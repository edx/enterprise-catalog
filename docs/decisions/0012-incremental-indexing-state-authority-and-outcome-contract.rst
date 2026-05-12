Incremental Indexing: State Row Authority and Outcome Contract
==============================================================

Status
------

Accepted

Context
-------

The Phase 3 batch tasks (``index_courses_batch_in_algolia`` /
``index_programs_batch_in_algolia`` / ``index_pathways_batch_in_algolia``,
all driven by ``_index_content_batch``) need a clear answer to two questions
that the higher-level tech-spec leaves implicit:

1. **Which is the source of truth for what shards a content owns in Algolia
   — the state row or Algolia itself?** Both have copies. The state row's
   ``algolia_object_ids`` is what the previous task wrote; Algolia's index is
   what's actually queryable. They can diverge after a partial failure or
   when shards from the legacy ``reindex_algolia`` job pre-date the state
   row.
2. **What outcomes can a single ``content_key`` resolve to in a batch run,
   and what does each do to the state row?** The dispatcher (Phase 4) and
   any ops/observability tooling will key off these outcomes, so they need
   to be a stable contract — not an implementation detail.

Decision
--------

**The state row is the system of record for shard ownership; Algolia browse
is a fallback.** ``_resolve_indexing_decision`` reads
``state.algolia_object_ids`` first to compute orphan diffs and to drive the
REMOVE path; it falls back to
``AlgoliaSearchClient.get_object_ids_for_aggregation_key`` only when the
state row has no recorded IDs (first index for this content, row reset, or
shards inherited from a legacy reindex). This trade trusts our own record
of writes over a per-record search round-trip; the fallback preserves
correctness during the transition window when legacy shards may exist
without a corresponding state row.

**``_index_content_batch`` resolves each ``content_key`` to exactly one of
four outcomes**, formalized as ``RecordOutcome`` (a ``StrEnum``) with these
semantics. Each ``IndexingDecision`` carries both a ``desired_outcome`` (set
during pass 1, immutable) and an ``outcome`` (mutable, mirrors
``desired_outcome`` until pass 2's per-record fallback flips it to FAILED on
write failure). Pass 3 dispatches on ``outcome``:

* **INDEXED** — content is indexable per the partition functions AND the
  legacy generator emits ≥1 shard. New objects are upserted, orphans
  deleted. State row: ``last_indexed_at`` stamped, ``algolia_object_ids``
  replaced, ``removed_from_index_at`` cleared.
* **SKIPPED** — ``state.last_indexed_at >= content.modified`` and
  ``force=False``. State row untouched.
* **REMOVED** — content is non-indexable per the partition functions, OR
  is indexable but the generator emits zero shards (e.g. catalog
  memberships dropped without ``ContentMetadata.modified`` advancing). Any
  existing shards are deleted from Algolia. State row:
  ``removed_from_index_at`` stamped.
* **FAILED** — a per-record exception was raised and caught.
  ``mark_as_failed(reason)`` is called best-effort. The batch continues
  processing the remaining records.

Counters and the per-key failure list roll up into a ``BatchSummary``
dataclass; task wrappers convert it via ``dataclasses.asdict()`` so the
on-the-wire Celery payload stays JSON-serializable.

Consequences
------------

* The state row's ``algolia_object_ids`` is load-bearing — anything that
  bypasses ``mark_as_indexed`` / ``mark_as_removed`` (manual SQL,
  hand-rolled migrations, etc.) can leave the index and the row out of
  sync. Future work that writes to Algolia should go through the same
  state-update boundary.
* The fallback browse on the INDEXED path can be retired once the legacy
  reindex job is decommissioned and we're confident no shards exist
  without a corresponding state row. Until then, the cost is one search
  op per first-time-indexed content.
* The ``RecordOutcome`` set and its state-row effects are a public contract
  for the Phase 4 dispatcher and any downstream tooling. Adding a fifth
  outcome or changing what an existing one does to the state row is an
  ADR-worthy change.
* The "indexable but zero shards → REMOVED" routing means a record can
  cycle REMOVED → INDEXED on the next run if memberships return —
  ``mark_as_indexed`` clears ``removed_from_index_at`` so the row reflects
  the current state instead of carrying both timestamps.
* The Redis-cached ``IndexingMappings`` layer (see Phase 3 in the tech
  spec) is not protected by a distributed lock. The Phase 4 dispatcher is
  responsible for calling ``invalidate_indexing_mappings_cache()`` and
  then warming the cache synchronously before fanning out batch tasks. A
  thundering herd on a cold cache is a correctness no-op (every worker
  computes the same mappings) but wastes DB scans; locking would address
  that performance concern but not the harder mid-batch-consistency
  question, which would need either snapshotting mappings into task args
  or a generation counter on the cache key. We deferred both until we
  see real concurrency patterns in production.
* The production tasks discard the ``IndexingResponse`` from
  ``save_objects`` / ``delete_objects`` and never call ``.wait()``. The
  state row is stamped on API acceptance, not on Algolia-side publish,
  so the ``last_indexed_at`` timestamp is earlier than actual searchability by a few
  seconds during the eventual-consistency window. This is intentional, see below.

Alternatives Considered
-----------------------
Algolia processes indexing jobs as async tasks, returning task ids along with a 200 status response code. The semantics
of that 200 status code is that Algolia has *accepted* the task and will *eventually* publish the updated record in the
search index. I looked into calling ``wait()`` on the index ``save_object()`` response to actually wait for
each indexing task to complete, but decided against it for the following reasons:

1. This reindexing system's source of truth for reads is the ``ContentMetadataIndexingState`` model/table,
   not Algolia itself (except as a fallback for finding shard ids).
2. Our system shouldn't be coupled to Algolia internals. Algolia returning 200 means the write
   is in their queue and will publish. Our state model stamps successful rows on acceptance of the indexing task,
   not on actual publish of the updated content. If publish fails post-acceptance that's an Algolia-side incident,
   not something we should have to defensively code against.
3. ``wait()`` polls the Algolia task endpoint until the async indexing task is complete,
   which would hurt the throughtput performance of our setup quite a bit.

See: https://github.com/algolia/algoliasearch-client-python/blob/3bb9108d9dff627f12c921ad23dab02984f70a44/algoliasearch/responses.py#L40
