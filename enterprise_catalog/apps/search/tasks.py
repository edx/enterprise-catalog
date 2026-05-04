"""
Per-content-type batch tasks for incremental Algolia indexing.

Each task takes a small list of content_keys (default batch size 10, controlled
by the dispatcher in Phase 4), generates Algolia objects via the legacy
``_get_algolia_products_for_batch`` helper, upserts new shards, deletes orphaned
shards, and updates the per-record ``ContentMetadataIndexingState``.

Design notes worth keeping in mind:

* **Algolia writes precede DB writes.** If the DB write fails after a successful
  Algolia write, the next stragglers run sees ``last_indexed_at`` not advanced
  and re-indexes — idempotent, just one wasted Algolia upsert.
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
    """
    return _index_content_batch(content_keys, COURSE, index_name=index_name, force=force)


@shared_task(base=_LoggedTaskWithRetry, bind=True, default_retry_delay=UNREADY_TASK_RETRY_COUNTDOWN_SECONDS)
def index_programs_batch_in_algolia(self, content_keys, index_name=None, force=False):  # pylint: disable=unused-argument
    """
    Index a small batch of program ContentMetadata records into Algolia.
    """
    return _index_content_batch(content_keys, PROGRAM, index_name=index_name, force=force)


@shared_task(base=_LoggedTaskWithRetry, bind=True, default_retry_delay=UNREADY_TASK_RETRY_COUNTDOWN_SECONDS)
def index_pathways_batch_in_algolia(self, content_keys, index_name=None, force=False):  # pylint: disable=unused-argument
    """
    Index a small batch of learner pathway ContentMetadata records into Algolia.
    """
    return _index_content_batch(content_keys, LEARNER_PATHWAY, index_name=index_name, force=force)


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

    Returns a results dict with counts and the list of content_keys that hit
    per-record failures.
    """
    results = {
        'content_type': content_type,
        'indexed': 0,
        'skipped': 0,
        'removed': 0,
        'failed': 0,
        'failed_keys': [],
    }
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
    # content's own batch to write.
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
    requested_keys = set(content_keys)
    for obj in legacy_objects:
        agg_key = obj.get('aggregation_key')
        if agg_key in requested_keys:
            objects_by_content_key[agg_key].append(obj)

    algolia_client = get_initialized_algolia_client()

    for content_key in content_keys:
        content = content_by_key.get(content_key)
        if content is None:
            logger.warning(
                'ContentMetadata not found for content_key=%s (content_type=%s); marking as failed.',
                content_key, content_type,
            )
            results['failed'] += 1
            results['failed_keys'].append(content_key)
            continue

        try:
            outcome = _process_content_key(
                content,
                new_objects=objects_by_content_key.get(content_key, []),
                indexable_keys=mappings.all_indexable_content_keys,
                algolia_client=algolia_client,
                index_name=index_name,
                force=force,
            )
            results[outcome] += 1
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception(
                'Per-record indexing failed for content_key=%s', content_key,
            )
            _safely_mark_failed(content, exc)
            results['failed'] += 1
            results['failed_keys'].append(content_key)

    logger.info(
        'index_%s_batch complete: indexed=%d skipped=%d removed=%d failed=%d',
        content_type,
        results['indexed'], results['skipped'], results['removed'], results['failed'],
    )
    return results


def _process_content_key(content, new_objects, indexable_keys, algolia_client, index_name, force):
    """
    Resolve a single content_key to one of REMOVE / SKIP / INDEX and return the
    name of the result counter to increment.
    """
    state, _ = ContentMetadataIndexingState.get_or_create_for_content(content)
    content_key = content.content_key

    if content_key not in indexable_keys:
        if state.algolia_object_ids:
            algolia_client.delete_objects_batch(state.algolia_object_ids, index_name=index_name)
        state.mark_as_removed()
        return 'removed'

    if not force and state.last_indexed_at and state.last_indexed_at >= content.modified:
        return 'skipped'

    new_object_ids = [obj['objectID'] for obj in new_objects]
    existing_object_ids = algolia_client.get_object_ids_for_content_key(
        content_key, index_name=index_name,
    )
    orphan_ids = set(existing_object_ids) - set(new_object_ids)

    if new_objects:
        algolia_client.save_objects_batch(new_objects, index_name=index_name)
    if orphan_ids:
        algolia_client.delete_objects_batch(list(orphan_ids), index_name=index_name)

    state.mark_as_indexed(algolia_object_ids=new_object_ids)
    return 'indexed'


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
