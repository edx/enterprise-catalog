"""
Per-content-type batch tasks for incremental Algolia indexing.

Each task takes a small list of content_keys (default batch size 10, controlled
by the dispatcher in Phase 4), generates Algolia objects via the legacy
``_get_algolia_products_for_batch`` helper, upserts new shards, deletes orphaned
shards, and updates the per-record ``ContentMetadataIndexingState``.

Design notes worth keeping in mind:

* **Three-pass + bulk-with-fallback.** ``_index_content_batch`` first resolves
  every record into an ``IndexingDecision`` (no Algolia writes; the only
  Algolia I/O is the per-record browse fallback in ``_existing_shard_ids``),
  then issues one bulk ``save_objects_batch`` and one bulk
  ``delete_objects_batch`` for the entire task batch. If a bulk call raises
  ``AlgoliaException``, we fall back to per-record save/delete to isolate
  which content_keys actually failed; per-record fallback failures mutate
  ``decision.outcome`` to FAILED in place. Algolia upserts and deletes by
  ``objectID``, so the per-record retries are idempotent against any chunks
  the SDK already flushed before the bulk call raised. The final pass
  applies state-row updates by dispatching on ``decision.outcome``.
* **Algolia save operations precede DB writes.**
  If the DB write fails after a successful  Algolia save, the next stragglers run sees ``last_indexed_at`` not advanced
  and re-indexes — idempotent, just one wasted Algolia upsert.
  Note that an Algolia "save" is actually the successful queueing of an async task,
  which task will eventually write/publish the updated index record on the Algolia index.
  See ADR 0012.
* **Per-record failures don't fail the whole batch.** Each content_key is
  isolated either at resolution time (try/except inside
  ``_resolve_indexing_decision``) or at fallback time (try/except per record
  inside ``_per_record_save_fallback`` / ``_per_record_delete_fallback``);
  failures are recorded via ``mark_as_failed`` and the loop continues.
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

from algoliasearch.exceptions import AlgoliaException
from celery import shared_task
from celery_utils.logged_task import LoggedTask
from django.db import IntegrityError
from django.db.utils import OperationalError
from requests.exceptions import ConnectionError as RequestsConnectionError

from enterprise_catalog.apps.api.tasks import _get_algolia_products_for_batch
from enterprise_catalog.apps.api_client.algolia import AlgoliaSearchClient
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
    IndexingMappings,
    get_indexing_mappings,
)
from enterprise_catalog.apps.search.models import ContentMetadataIndexingState


logger = logging.getLogger(__name__)


UNREADY_TASK_RETRY_COUNTDOWN_SECONDS = 60 * 5


class RecordOutcome(StrEnum):
    """
    The outcome of indexing a single content record inside a batch indexing
    task.

    ``_resolve_indexing_decision`` produces one of INDEXED / SKIPPED / REMOVED
    / FAILED in pass 1; FAILED can also arrive in pass 2 if a per-record
    fallback save/delete itself raises (the fallback mutates the decision's
    outcome). The string values match the ``BatchSummary`` counter attribute
    names so ``increment(outcome)`` can dispatch via ``setattr``.
    """
    INDEXED = 'indexed'
    SKIPPED = 'skipped'
    REMOVED = 'removed'
    FAILED = 'failed'


@dataclass
class BatchSummary:
    """
    Counts of per-record outcomes from a single batch indexing task, plus
    the ``content_type`` and the list of content_keys that hit per-record
    failures.

    ``_index_content_batch`` returns one of these. The Celery task wrappers
    convert it to a plain dict via ``dataclasses.asdict()`` so the on-the-wire
    shape (over Celery / JSON) stays stable for downstream consumers.

    Counter attribute names match ``RecordOutcome`` member values 1:1, which
    lets ``increment(outcome)`` use ``setattr``/``getattr`` without a switch.
    """
    content_type: str
    indexed: int = 0
    skipped: int = 0
    removed: int = 0
    failed: int = 0
    failed_keys: list = field(default_factory=list)

    def increment(self, outcome):
        """Bump the counter that matches ``outcome`` (a ``RecordOutcome``)."""
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


@dataclass
class IndexingDecision:
    """
    The resolved indexing decision for one content record in a batch indexing
    task.

    Built by ``_resolve_indexing_decision`` in pass 1 (with at most one
    per-record Algolia browse — see ``_existing_shard_ids``), consumed by
    ``_execute_saves`` / ``_execute_deletes`` in pass 2, then applied to the
    state row + counters in pass 3 by ``_finalize_decision``.

    Each decision carries two outcome fields:

    * ``desired_outcome`` (immutable) — what pass 1's resolution determined
      *should* happen for this record (INDEXED / SKIPPED / REMOVED / FAILED).
    * ``outcome`` (mutable, mirrors ``desired_outcome`` until pass 2 runs) —
      what *actually* happened after the Algolia writes. The per-record
      fallback paths set ``outcome = FAILED`` and populate ``failure_reason``
      when a save or delete retry raises. Pass 3 dispatches on ``outcome``.

    The split keeps pass 1's plan auditable after pass 2 has rewritten the
    fate of some records.

    Always construct via the ``skipped`` / ``removed`` / ``indexed`` /
    ``failed`` classmethods rather than the raw constructor; each enforces
    the field invariants for its desired outcome.

    Invariants by desired_outcome:

    * INDEXED — ``new_objects`` non-empty, ``new_object_ids`` matches their
      ``objectID`` (in order), ``ids_to_delete`` is the set of orphans (may
      be empty).
    * REMOVED — ``new_objects`` and ``new_object_ids`` empty; ``ids_to_delete``
      is the set of shards Algolia is hosting for this content (may be
      empty).
    * SKIPPED — nothing to do; all lists empty.
    * FAILED — planning failed (e.g. missing ``ContentMetadata``).
      ``content`` and ``state`` may be ``None``; ``failure_reason`` carries
      the exception.
    """
    content_key: str
    desired_outcome: RecordOutcome
    outcome: RecordOutcome = None
    content: ContentMetadata = None
    state: ContentMetadataIndexingState = None
    new_objects: list = field(default_factory=list)
    new_object_ids: list = field(default_factory=list)
    ids_to_delete: list = field(default_factory=list)
    failure_reason: Exception = None

    def __post_init__(self):
        # ``outcome`` mirrors ``desired_outcome`` until pass 2 overrides it.
        if self.outcome is None:
            self.outcome = self.desired_outcome

    @classmethod
    def skipped(cls, *, content_key, content, state):
        """``state.last_indexed_at`` is current and ``force=False``."""
        return cls(
            content_key=content_key, desired_outcome=RecordOutcome.SKIPPED,
            content=content, state=state,
        )

    @classmethod
    def removed(cls, *, content_key, content, state, ids_to_delete):
        """
        Either the content is no longer indexable, or it's indexable but the
        legacy generator emitted zero shards (memberships dropped).
        ``ids_to_delete`` is whatever Algolia is currently hosting for it.
        """
        return cls(
            content_key=content_key, desired_outcome=RecordOutcome.REMOVED,
            content=content, state=state, ids_to_delete=list(ids_to_delete),
        )

    @classmethod
    def indexed(cls, *, content_key, content, state, new_objects, new_object_ids, ids_to_delete):
        """
        New objects to upsert (non-empty) plus any orphan shard IDs that the
        previous run wrote but this run no longer needs.
        """
        return cls(
            content_key=content_key, desired_outcome=RecordOutcome.INDEXED,
            content=content, state=state,
            new_objects=new_objects, new_object_ids=new_object_ids,
            ids_to_delete=list(ids_to_delete),
        )

    @classmethod
    def failed(cls, *, content_key, content=None, state=None, failure_reason):
        """
        Pass 1 couldn't produce a real plan (e.g. missing ``ContentMetadata``).
        Pass 3 will best-effort stamp the state row via ``mark_as_failed`` if
        ``content`` is available.
        """
        return cls(
            content_key=content_key, desired_outcome=RecordOutcome.FAILED,
            content=content, state=state, failure_reason=failure_reason,
        )


def _index_content_batch(content_keys, content_type, index_name=None, force=False):
    """
    Drive the per-record indexing loop for a batch of content_keys via three
    coordinated passes:

    1. **Resolve** (no Algolia writes; one per-record read fallback): each
       content_key resolves to an ``IndexingDecision`` carrying one of four
       outcomes — INDEXED / SKIPPED / REMOVED / FAILED — plus the new objects
       and shard IDs needed for the writes. The only Algolia I/O here is
       ``_existing_shard_ids``'s per-record browse fallback for records the
       state row hasn't seen yet.
    2. **Execute** (bulk-with-fallback): one ``save_objects_batch`` call across
       every decision with ``desired_outcome=INDEXED``, then one
       ``delete_objects_batch`` for every orphan + REMOVED shard. If either
       bulk call raises, we fall back to per-record retries; per-record
       failures mutate the decision's ``outcome`` to FAILED in place.
    3. **Finalize** (state row updates): each decision's actual outcome drives
       the state-row stamp (``mark_as_indexed``, ``mark_as_removed``,
       ``mark_as_failed``) and the counter increment.

    Returns a ``BatchSummary`` with counts and the list of content_keys that
    hit per-record failures. Task wrappers convert it to a dict via
    ``asdict()`` for Celery transport.
    """
    results = BatchSummary(content_type=content_type)
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

    objects_by_content_key = _build_objects_by_content_key(
        content_keys=content_keys, content_type=content_type, mappings=mappings,
    )

    algolia_client = get_initialized_algolia_client()

    # --- Pass 1: resolve each content_key into an IndexingDecision ---------
    decisions: list[IndexingDecision] = [
        _resolve_indexing_decision(
            content_key=content_key,
            content=content_by_key.get(content_key),
            content_type=content_type,
            new_objects=objects_by_content_key.get(content_key, []),
            indexable_keys=mappings.all_indexable_content_keys,
            algolia_client=algolia_client,
            index_name=index_name,
            force=force,
        )
        for content_key in content_keys
    ]

    # --- Pass 2: bulk Algolia ops with per-record fallback ------------------
    # Per-record fallbacks mutate decision.outcome to FAILED on retry failure,
    # so pass 3 only needs to look at decision.outcome.
    _execute_saves(decisions, algolia_client, index_name)
    _execute_deletes(decisions, algolia_client, index_name)

    # --- Pass 3: finalize state rows + counters -----------------------------
    # Wrap per-decision so a DB hiccup in one record's ``mark_as_*`` doesn't
    # abort the rest of the batch. The failure is recorded in the summary; we
    # don't attempt a recovery ``mark_as_failed`` write here since that path
    # could itself raise — the next run sees ``last_indexed_at`` unchanged and
    # re-indexes idempotently.
    for decision in decisions:
        try:
            _finalize_decision(decision, results)
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                'Finalize step raised for content_key=%s', decision.content_key,
            )
            results.record_failure(decision.content_key)

    logger.info(
        'index_%s_batch complete: indexed=%d skipped=%d removed=%d failed=%d',
        content_type,
        results.indexed,
        results.skipped,
        results.removed,
        results.failed,
    )
    return results


def _build_objects_by_content_key(
    content_keys: list[str],
    content_type: str,
    mappings: IndexingMappings,
) -> dict[str, list[dict]]:
    """
    Generate Algolia objects for the batch in one call to the legacy generator
    and bucket them by content_key.

    Filter the generator output by aggregation_key so we only act on shards
    belonging to keys in this batch — the generator may "pull in" related
    content (e.g. courses inside a requested program) which we leave for that
    content's own batch to write. The generator emits ``aggregation_key`` as
    ``"{content_type}:{content_key}"``, so we build the same prefixed form
    here.
    """
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
    return objects_by_content_key


def _aggregation_key_for(content_type, content_key):
    """
    The legacy Algolia object generator emits ``aggregation_key`` as
    ``"{content_type}:{content_key}"`` (e.g. ``"course:edX+DemoX"``). The
    incremental indexing tasks need the same prefixed form to filter
    generator output and to query Algolia for existing shards.
    """
    return f'{content_type}:{content_key}'


def _existing_shard_ids(state, aggregation_key, algolia_client, index_name):
    """
    Return the Algolia shard objectIDs Algolia is hosting for a content
    record.

    Prefer the state row's tracked IDs — it's the system of record for what
    the previous run wrote, and trusting it eliminates one Algolia search op
    per non-SKIP record. Fall back to a per-record Algolia browse only when
    the row has no recorded IDs (first-time index, row reset, or shards
    inherited from a legacy reindex). Until the legacy reindexer is retired,
    this fallback is the only way to detect orphans on first contact.
    """
    if state.algolia_object_ids:
        return state.algolia_object_ids
    return algolia_client.get_object_ids_for_aggregation_key(
        aggregation_key, index_name=index_name,
    )


def _resolve_indexing_decision(
    content_key: str,
    content: ContentMetadata | None,
    content_type: str,
    new_objects: list[dict],
    indexable_keys: set[str],
    algolia_client: AlgoliaSearchClient,
    index_name: str | None,
    force: bool,
) -> IndexingDecision:
    """
    Decide whether this content should be indexed, skipped, removed, or
    marked failed, and return that plan. No Algolia writes happen here;
    later passes act on it. A single Algolia read may occur the first
    time we see a record, to discover existing shards that need cleanup.

    ``content=None`` means the upstream DB lookup turned up no
    ``ContentMetadata`` row for this key — that's resolved here as FAILED,
    not in the caller.
    """
    if content is None:
        logger.warning(
            'ContentMetadata not found for content_key=%s (content_type=%s); marking as failed.',
            content_key, content_type,
        )
        return IndexingDecision.failed(
            content_key=content_key,
            failure_reason=ValueError(
                f'ContentMetadata not found for content_key={content_key}',
            ),
        )

    try:
        state, _ = ContentMetadataIndexingState.get_or_create_for_content(content)
        aggregation_key = _aggregation_key_for(content_type, content_key)

        if content_key not in indexable_keys:
            return IndexingDecision.removed(
                content_key=content_key, content=content, state=state,
                ids_to_delete=_existing_shard_ids(
                    state, aggregation_key, algolia_client, index_name,
                ),
            )

        if not force and state.last_indexed_at and state.last_indexed_at >= content.modified:
            return IndexingDecision.skipped(
                content_key=content_key, content=content, state=state,
            )

        if not new_objects:
            # Indexable per the partition fn, but the legacy generator emitted
            # zero shards (e.g. catalog memberships dropped without
            # ``ContentMetadata.modified`` advancing). Treat as REMOVED so
            # the row reflects reality and the next run can resurrect it
            # cleanly when memberships return.
            logger.warning(
                'Indexable %s %r produced zero Algolia objects; treating as REMOVED.',
                content_type, content_key,
            )
            return IndexingDecision.removed(
                content_key=content_key, content=content, state=state,
                ids_to_delete=_existing_shard_ids(
                    state, aggregation_key, algolia_client, index_name,
                ),
            )

        new_object_ids = [obj['objectID'] for obj in new_objects]
        orphan_ids = list(
            set(_existing_shard_ids(state, aggregation_key, algolia_client, index_name))
            - set(new_object_ids)
        )
        return IndexingDecision.indexed(
            content_key=content_key, content=content, state=state,
            new_objects=new_objects,
            new_object_ids=new_object_ids,
            ids_to_delete=orphan_ids,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception(
            'Resolving indexing decision failed for content_key=%s', content_key,
        )
        return IndexingDecision.failed(
            content_key=content_key, content=content, failure_reason=exc,
        )


def _execute_saves(decisions, algolia_client, index_name):
    """
    Issue one bulk ``save_objects_batch`` for every decision whose
    desired_outcome is INDEXED. On ``AlgoliaException``, fall back to per-record
    save — per-record failures mutate the decision's actual outcome to FAILED.
    """
    indexed_decisions = [
        d for d in decisions
        if d.desired_outcome == RecordOutcome.INDEXED
    ]
    if not indexed_decisions:
        return

    all_objects = [obj for decision in indexed_decisions for obj in decision.new_objects]
    try:
        algolia_client.save_objects_batch(all_objects, index_name=index_name)
    except AlgoliaException:
        logger.exception(
            'Bulk save_objects_batch raised for %d records; falling back to per-record saves.',
            len(indexed_decisions),
        )
        _per_record_save_fallback(indexed_decisions, algolia_client, index_name)


def _per_record_save_fallback(decisions, algolia_client, index_name):
    """
    Retry each decision's save individually. On per-record failure, mutate
    the decision's outcome to FAILED so pass 3 dispatches via the FAILED
    branch.
    """
    for decision in decisions:
        try:
            algolia_client.save_objects_batch(decision.new_objects, index_name=index_name)
        except AlgoliaException as exc:
            logger.exception(
                'Per-record save fallback failed for content_key=%s', decision.content_key,
            )
            decision.outcome = RecordOutcome.FAILED
            decision.failure_reason = exc


def _execute_deletes(decisions, algolia_client, index_name):
    """
    Issue one bulk ``delete_objects_batch`` for every orphan + REMOVED shard
    across the batch. Decisions whose save fallback failed have already been
    mutated to FAILED, so the outcome filter excludes them — their old shards
    stay in place as the partial-failure fallback.

    On ``AlgoliaException``, fall back to per-record deletes; per-record
    failures mutate the decision's outcome to FAILED in place.
    """
    delete_decisions = [
        decision for decision in decisions
        if decision.outcome in (RecordOutcome.INDEXED, RecordOutcome.REMOVED)
        and decision.ids_to_delete
    ]
    if not delete_decisions:
        return

    all_ids = [obj_id for decision in delete_decisions for obj_id in decision.ids_to_delete]
    try:
        algolia_client.delete_objects_batch(all_ids, index_name=index_name)
    except AlgoliaException:
        logger.exception(
            'Bulk delete_objects_batch raised for %d records; falling back to per-record deletes.',
            len(delete_decisions),
        )
        _per_record_delete_fallback(delete_decisions, algolia_client, index_name)


def _per_record_delete_fallback(decisions, algolia_client, index_name):
    """
    Retry each decision's delete individually. On per-record failure, mutate
    the decision's outcome to FAILED.
    """
    for decision in decisions:
        try:
            algolia_client.delete_objects_batch(decision.ids_to_delete, index_name=index_name)
        except AlgoliaException as exc:
            logger.exception(
                'Per-record delete fallback failed for content_key=%s', decision.content_key,
            )
            decision.outcome = RecordOutcome.FAILED
            decision.failure_reason = exc


def _finalize_decision(decision, results):
    """
    Apply the decision's final outcome to the state row and bump the matching
    counter. ``decision.outcome`` reflects what actually happened after pass 2
    (a save or delete fallback may have moved a desired-INDEXED record to
    FAILED), so this is a pure dispatch on the actual outcome.

    Any exception raised here (e.g. a DB error from ``mark_as_*``) propagates
    to the caller, which catches it per-record so siblings still finalize.
    """
    if decision.outcome == RecordOutcome.FAILED:
        if decision.content is not None:
            state = decision.state or ContentMetadataIndexingState.get_or_create_for_content(
                decision.content,
            )[0]
            state.mark_as_failed(reason=decision.failure_reason)
        results.record_failure(decision.content_key)
    elif decision.outcome == RecordOutcome.SKIPPED:
        results.increment(RecordOutcome.SKIPPED)
    elif decision.outcome == RecordOutcome.REMOVED:
        decision.state.mark_as_removed()
        results.increment(RecordOutcome.REMOVED)
    elif decision.outcome == RecordOutcome.INDEXED:
        decision.state.mark_as_indexed(algolia_object_ids=decision.new_object_ids)
        results.increment(RecordOutcome.INDEXED)
