"""
Per-content-type batch tasks for incremental Algolia indexing.

Each task takes a small list of content_keys (default batch size 10, controlled
by the dispatcher in Phase 4), generates Algolia objects via the legacy
``_get_algolia_products_for_batch`` helper, upserts new shards, deletes orphaned
shards, and updates the per-record ``ContentMetadataIndexingState``.

Design notes worth keeping in mind:

* **Algolia save operations precede DB writes.**
  If the DB write fails after a successful  Algolia save, the next stragglers run sees ``last_indexed_at`` not advanced
  and re-indexes — idempotent, just one wasted Algolia upsert.
  Note that an Algolia "save" is actually the successful queueing of an async task,
  which task will eventually write/publish the updated index record on the Algolia index.
  See ADR 0012.
* **Per-record failures don't fail the whole batch.** Each content_key is
  wrapped in its own try/except; failures are recorded via ``mark_as_failed``
  and the loop continues.
* **Orphaned shards** are detected by querying Algolia for the content_key's
  current shards and diffing against the new shard set. The legacy
  ``replace_all_objects`` flow relied on full-index replacement; we don't have
  that, so each batch task does its own cleanup.
* **No** ``transaction.atomic()`` wrapping. The state-update helpers each do a
  single ``save(update_fields=...)`` which is atomic at the DB level, and
  Algolia calls are external — they can't be rolled back by a DB rollback
  anyway.
"""
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from enum import StrEnum

from celery import shared_task
from celery_utils.logged_task import LoggedTask
from django.db import IntegrityError
from django.db.utils import OperationalError
from requests.exceptions import ConnectionError as RequestsConnectionError

from enterprise_catalog.apps.api.tasks import _get_algolia_products_for_batch
from enterprise_catalog.apps.catalog.algolia_utils import (
    get_initialized_algolia_client,
)
from enterprise_catalog.apps.catalog.constants import (
    COURSE,
    LEARNER_PATHWAY,
    PROGRAM,
)
from enterprise_catalog.apps.catalog.models import ContentMetadata
from enterprise_catalog.apps.search.indexing_mappings import (
    get_indexing_mappings,
)
from enterprise_catalog.apps.search.models import ContentMetadataIndexingState


logger = logging.getLogger(__name__)


UNREADY_TASK_RETRY_COUNTDOWN_SECONDS = 60 * 5


class BatchOutcome(StrEnum):
    """
    The resolution of a single content_key inside a batch indexing task.

    ``_process_content_key`` returns one of INDEXED / SKIPPED / REMOVED.
    FAILED is recorded by ``_index_content_batch`` when an exception bubbles
    out of the per-record block. The string values match the ``BatchResults``
    counter attribute names so we can dispatch increments via ``setattr``.
    """
    INDEXED = 'indexed'
    SKIPPED = 'skipped'
    REMOVED = 'removed'
    FAILED = 'failed'


@dataclass
class BatchResults:
    """
    Counts of per-record outcomes from a single batch indexing task, plus
    the ``content_type`` and the list of content_keys that hit per-record
    failures.

    ``_index_content_batch`` returns one of these. The Celery task wrappers
    convert it to a plain dict via ``dataclasses.asdict()`` so the on-the-wire
    shape (over Celery / JSON) stays stable for downstream consumers.

    Counter attribute names match ``BatchOutcome`` member values 1:1, which
    lets ``increment(outcome)`` use ``setattr``/``getattr`` without a switch.
    """
    content_type: str
    indexed: int = 0
    skipped: int = 0
    removed: int = 0
    failed: int = 0
    failed_keys: list = field(default_factory=list)

    def increment(self, outcome):
        """Bump the counter that matches ``outcome`` (a ``BatchOutcome``)."""
        setattr(self, outcome, getattr(self, outcome) + 1)

    def record_failure(self, content_key):
        """Bump the FAILED counter and remember which content_key failed."""
        self.failed += 1
        self.failed_keys.append(content_key)


class _LoggedTaskWithRetry(LoggedTask):  # pylint: disable=abstract-method
    """
    Local copy of the legacy ``LoggedTaskWithRetry`` semantics. Mirrors
    ``enterprise_catalog.apps.api.tasks.LoggedTaskWithRetry`` so the search
    app can decorate its tasks without importing across the catalog/search
    boundary.
    """
    autoretry_for = (
        RequestsConnectionError,
        IntegrityError,
        OperationalError,
    )
    retry_kwargs = {'max_retries': 5}
    retry_backoff = True
    retry_jitter = True


@shared_task(base=_LoggedTaskWithRetry, bind=True, default_retry_delay=UNREADY_TASK_RETRY_COUNTDOWN_SECONDS)
def index_courses_batch_in_algolia(self, content_keys, index_name=None, force=False):  # pylint: disable=unused-argument
    """
    Index a small batch of course ContentMetadata records into Algolia.

    Returns a plain dict (via ``dataclasses.asdict``) so the on-the-wire
    Celery payload stays JSON-serializable.
    """
    return asdict(_index_content_batch(content_keys, COURSE, index_name=index_name, force=force))


@shared_task(base=_LoggedTaskWithRetry, bind=True, default_retry_delay=UNREADY_TASK_RETRY_COUNTDOWN_SECONDS)
def index_programs_batch_in_algolia(self, content_keys, index_name=None, force=False):  # pylint: disable=unused-argument
    """
    Index a small batch of program ContentMetadata records into Algolia.

    Returns a plain dict (via ``dataclasses.asdict``) so the on-the-wire
    Celery payload stays JSON-serializable.
    """
    return asdict(_index_content_batch(content_keys, PROGRAM, index_name=index_name, force=force))


@shared_task(base=_LoggedTaskWithRetry, bind=True, default_retry_delay=UNREADY_TASK_RETRY_COUNTDOWN_SECONDS)
def index_pathways_batch_in_algolia(self, content_keys, index_name=None, force=False):  # pylint: disable=unused-argument
    """
    Index a small batch of learner pathway ContentMetadata records into Algolia.

    Returns a plain dict (via ``dataclasses.asdict``) so the on-the-wire
    Celery payload stays JSON-serializable.
    """
    return asdict(_index_content_batch(content_keys, LEARNER_PATHWAY, index_name=index_name, force=force))


def _index_content_batch(content_keys, content_type, index_name=None, force=False):
    """
    Drive the per-record indexing loop for a batch of content_keys.

    Each content_key resolves to one of three outcomes:

    * **REMOVE** — content is no longer indexable; existing Algolia shards are
      deleted and the state row is marked ``removed_from_index_at``.
    * **SKIP** — already indexed at the current ContentMetadata version
      (``state.last_indexed_at >= content.modified``) and ``force=False``.
    * **INDEX** — new objects are upserted, orphaned shards deleted, and the
      state row is marked ``last_indexed_at``.

    Returns a ``BatchResults`` with counts and the list of content_keys that
    hit per-record failures. Task wrappers convert it to a dict via
    ``asdict()`` for Celery transport.
    """
    results = BatchResults(content_type=content_type)
    if not content_keys:
        logger.info('No content_keys passed to index_%s_batch; returning empty result.', content_type)
        return results

    mappings = get_indexing_mappings()

    content_by_key = {
        content.content_key: content
        for content in ContentMetadata.objects.filter(
            content_key__in=content_keys, content_type=content_type,
        )
    }

    # Generate all Algolia objects for the batch in one call. Filter the
    # output by aggregation_key so we only act on shards belonging to keys
    # in this batch — the legacy generator may "pull in" related content
    # (e.g. courses inside a requested program) which we leave for that
    # content's own batch to write. The legacy generator emits
    # ``aggregation_key`` as ``"{content_type}:{content_key}"`` (e.g.
    # ``"course:edX+DemoX"``), so we build the prefixed form here.
    objects_by_content_key = defaultdict(list)
    legacy_objects = _get_algolia_products_for_batch(
        batch_num=0,
        content_keys_batch=content_keys,
        all_indexable_content_keys=mappings.all_indexable_content_keys,
        program_to_courses_mapping=mappings.program_to_course_keys,
        pathway_to_programs_courses_mapping=mappings.pathway_to_program_course_keys,
        context_accumulator={
            'total_algolia_products_count': 0,
            'discarded_algolia_object_ids': defaultdict(int),
        },
        dry_run=False,
    )
    aggregation_key_to_content_key = {
        _aggregation_key_for(content_type, ck): ck for ck in content_keys
    }
    for obj in legacy_objects:
        agg_key = obj.get('aggregation_key')
        if agg_key in aggregation_key_to_content_key:
            objects_by_content_key[aggregation_key_to_content_key[agg_key]].append(obj)

    algolia_client = get_initialized_algolia_client()

    for content_key in content_keys:
        content = content_by_key.get(content_key)
        if content is None:
            logger.warning(
                'ContentMetadata not found for content_key=%s (content_type=%s); marking as failed.',
                content_key, content_type,
            )
            results.record_failure(content_key)
            continue

        try:
            outcome = _process_content_key(
                content,
                content_type=content_type,
                new_objects=objects_by_content_key.get(content_key, []),
                indexable_keys=mappings.all_indexable_content_keys,
                algolia_client=algolia_client,
                index_name=index_name,
                force=force,
            )
            results.increment(outcome)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception(
                'Per-record indexing failed for content_key=%s', content_key,
            )
            _safely_mark_failed(content, exc)
            results.record_failure(content_key)

    logger.info(
        'index_%s_batch complete: indexed=%d skipped=%d removed=%d failed=%d',
        content_type,
        results.indexed,
        results.skipped,
        results.removed,
        results.failed,
    )
    return results


def _aggregation_key_for(content_type, content_key):
    """
    The legacy Algolia object generator emits ``aggregation_key`` as
    ``"{content_type}:{content_key}"`` (e.g. ``"course:edX+DemoX"``). The
    incremental indexing tasks need the same prefixed form to filter
    generator output and to query Algolia for existing shards.
    """
    return f'{content_type}:{content_key}'


def _process_content_key(
    content, content_type, new_objects, indexable_keys, algolia_client, index_name, force,
):
    """
    Resolve a single content_key to one of ``BatchOutcome.REMOVED``,
    ``BatchOutcome.SKIPPED``, or ``BatchOutcome.INDEXED``. The caller increments
    the matching counter in the batch results dict.

    ``BatchOutcome.FAILED`` is never returned from here — exceptions raised
    inside this function bubble up to ``_index_content_batch``, which records
    the failure separately.
    """
    state, _ = ContentMetadataIndexingState.get_or_create_for_content(content)
    content_key = content.content_key
    aggregation_key = _aggregation_key_for(content_type, content_key)

    if content_key not in indexable_keys:
        # If the state row tracks shards, delete those. Otherwise (first time
        # we've seen this record, or state row missing the IDs from a legacy
        # reindex), fall back to querying Algolia by aggregation_key so we
        # don't leave orphans behind.
        ids_to_remove = state.algolia_object_ids or algolia_client.get_object_ids_for_aggregation_key(
            aggregation_key, index_name=index_name,
        )
        if ids_to_remove:
            algolia_client.delete_objects_batch(ids_to_remove, index_name=index_name)
        state.mark_as_removed()
        return BatchOutcome.REMOVED

    if not force and state.last_indexed_at and state.last_indexed_at >= content.modified:
        return BatchOutcome.SKIPPED

    new_object_ids = [obj['objectID'] for obj in new_objects]
    # Prefer the state row's tracked shard IDs over a browse round-trip — the
    # state row is the system of record for what we wrote on the previous run,
    # and trusting it eliminates one Algolia search op per non-SKIP record.
    # Fall back to a browse only when state is empty (first index for this
    # content, row was reset, etc.) so we still detect orphans in that case.
    existing_object_ids = state.algolia_object_ids or algolia_client.get_object_ids_for_aggregation_key(
        aggregation_key, index_name=index_name,
    )

    if not new_objects:
        # Indexable per the partition fn, but the legacy generator emitted
        # zero shards (e.g. catalog memberships were dropped without
        # ``ContentMetadata.modified`` advancing). Treat as REMOVED so the
        # row reflects reality and the next run can resurrect it cleanly
        # when memberships return.
        logger.warning(
            'Indexable %s %r produced zero Algolia objects; treating as REMOVED.',
            content_type, content_key,
        )
        if existing_object_ids:
            algolia_client.delete_objects_batch(existing_object_ids, index_name=index_name)
        state.mark_as_removed()
        return BatchOutcome.REMOVED

    orphan_ids = set(existing_object_ids) - set(new_object_ids)
    algolia_client.save_objects_batch(new_objects, index_name=index_name)
    if orphan_ids:
        algolia_client.delete_objects_batch(list(orphan_ids), index_name=index_name)

    state.mark_as_indexed(algolia_object_ids=new_object_ids)
    return BatchOutcome.INDEXED


def _safely_mark_failed(content, exc):
    """
    Record a per-record failure on the state row, swallowing any exception
    raised by ``mark_as_failed`` itself — we never want the failure-recording
    path to take down the whole batch.
    """
    try:
        state, _ = ContentMetadataIndexingState.get_or_create_for_content(content)
        state.mark_as_failed(reason=exc)
    except Exception:  # pylint: disable=broad-except
        logger.exception(
            'mark_as_failed itself raised for content_key=%s; original error was %r',
            content.content_key, exc,
        )
