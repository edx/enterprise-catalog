# Phase 4a Plan: Algolia Incremental Indexing Dispatcher (stale/failed → batch → dispatch)

This document captures the implementation plan for **Phase 4a** of the incremental Algolia indexing project in `edx/enterprise-catalog`.

Scope: implement the **scheduled dispatcher task** that queries for **stale and/or failed** records and dispatches **batch indexing tasks**.

> Note: This plan is intentionally detailed so it can be used later to craft an ADR.

---

## Background / Context

The new incremental indexing architecture replaces the legacy monolithic Algolia reindex with a **dispatcher → parallel batch tasks** model.

Phase 3 introduced:

- Content-type batch indexing tasks (courses/programs/pathways)
- `IndexingMappings` caching via `enterprise_catalog/apps/search/indexing_mappings.py`
  - The cache computes **indexable content keys** via the legacy partition helpers.

Phase 4 is responsible for:

- Dispatcher task(s) that decide *what to index* and *when*, and fan out batch tasks.

This Phase 4a slice covers only the **primary/scheduled dispatcher**:

- `dispatch_algolia_indexing`

Not included in this slice:

- `dispatch_algolia_indexing_for_catalog_query` (catalog-query-specific dispatcher)

---

## Goals

- Identify **records needing indexing**:
  - never indexed
  - stale (content modified since last index)
  - failed previously (retry)
  - programs/pathways: stale when child content was indexed more recently
- Batch work into small parallel units (default 10 keys per task).
- Dispatch batch indexing tasks without blocking.
- Support `dry_run` for safe execution/testing.
- Avoid unnecessary recomputation by using the cached `IndexingMappings` and warming it once per dispatcher run.

---

## Non-goals

- No membership-removal detection for a specific catalog query (that’s the catalog-query-specific dispatcher).
- No integration into the daily cron chain / API endpoint in this slice (that’s later integration work).

---

## Key Decisions

### 1) Partitioning happens in `IndexingMappings`, not in the dispatcher

We will **not** call the partition helpers directly in the dispatcher. The dispatcher will rely on:

- `get_indexing_mappings()` from `enterprise_catalog/apps/search/indexing_mappings.py`

That module already calls:

- `partition_course_keys_for_indexing(...)`
- `partition_program_keys_for_indexing(...)`

…and computes:

- `IndexingMappings.all_indexable_content_keys`

This avoids duplicating “indexable content” logic in multiple layers.

### 2) Cache invalidation only for force runs

- If `force=True`, the dispatcher will call `invalidate_indexing_mappings_cache()` before warming mappings.
- If `force=False` (stragglers/retry style runs), we will *not* invalidate; TTL is the safety net.

### 3) Programs/pathways staleness depends on child indexing

- Programs are stale if a child course has been indexed more recently.
- Pathways are stale if a child program has been indexed more recently.

This is implemented immediately in Phase 4a.

### 4) UUID-based program key detection

`IndexingMappings.pathway_to_program_course_keys[pathway_key]` contains a **mixed set** of:

- course content keys (not UUIDs)
- program content keys (UUID strings)

We distinguish program keys by UUID parsing.

---

## Proposed Public API

### Celery Task: `dispatch_algolia_indexing`

**Location**: `enterprise_catalog/apps/search/tasks.py`

**Signature (proposed)**:

```python
dispatch_algolia_indexing(
    *,
    force: bool = False,
    dry_run: bool = False,
    include_failed: bool = True,
    index_name: str | None = None,
)
```

**Parameters**:

- `force`:
  - `True`: index all indexable records regardless of staleness.
  - `False`: index only stale/failed records.
- `dry_run`:
  - `True`: compute and log batches, but do not enqueue tasks.
- `include_failed`:
  - `True`: include previously failed records for retry.
  - Note: when `force=True`, this flag is effectively irrelevant because we index everything anyway.
- `index_name`:
  - optional override to target a non-default Algolia index (e.g., v2) by passing through to batch tasks.

---

## Dispatcher Algorithm

### Step 0: Cache management

1. If `force=True`, call `invalidate_indexing_mappings_cache()`.
2. Warm mappings with `get_indexing_mappings()` synchronously.

This provides:

- `program_to_course_keys`
- `pathway_to_program_course_keys`
- `all_indexable_content_keys`

### Step 1: Determine indexable keys per content type

We avoid guessing based on key prefixes.

- Use `mappings.all_indexable_content_keys` as the global set.
- Derive per-type indexable keys by querying `ContentMetadata` by `content_type` and `content_key__in` that set:

  - courses: `content_type=COURSE`
  - programs: `content_type=PROGRAM`
  - pathways: `content_type=LEARNER_PATHWAY`

### Step 2: Stale/failed filtering

#### Courses (when `force=False`)
Include a course key when any is true:

- never indexed: `state.last_indexed_at is NULL`
- stale: `ContentMetadata.modified > state.last_indexed_at`
- failed and retry enabled: `include_failed=True` and `state.last_failure_at is not NULL`

When `force=True`, include all indexable course keys.

#### Programs (when `force=False`)
Include a program key when any is true:

- never indexed
- failed (if enabled)
- stale via child course indexing:
  - any child course `last_indexed_at > program.last_indexed_at`
  - child course keys come from `mappings.program_to_course_keys[program_key]`

When `force=True`, include all indexable program keys.

#### Pathways (when `force=False`)
Include a pathway key when any is true:

- never indexed
- failed (if enabled)
- stale via child program indexing:
  - any child program `last_indexed_at > pathway.last_indexed_at`
  - child keys come from `mappings.pathway_to_program_course_keys[pathway_key]`
  - extract program keys by UUID parsing (program `content_key` is always a UUID string)

When `force=True`, include all indexable pathway keys.

### Step 3: Batch and dispatch

- Batch size: `settings.ALGOLIA_INDEXING_BATCH_SIZE` (default: 10)
- Build per-type batches, then dispatch as a **serial chain of Celery groups**:

  ```
  chain(
      group(course_batches),     # all course tasks run in parallel
      group(program_batches),    # starts only after ALL courses finish
      group(pathway_batches),    # starts only after ALL programs finish
  ).apply_async()
  ```

  Tasks use `.si()` (immutable signatures) so each task's kwargs are fixed at
  dispatch time and Celery does not forward previous group results as positional
  arguments.

  Empty groups are omitted from the chain.

**Why ordering matters — child-staleness propagation:**

Programs and pathways detect staleness by comparing their own
`last_indexed_at` against the `last_indexed_at` of their child content.
That comparison is only meaningful if child timestamps reflect the current
run — i.e. if courses were indexed *before* programs are evaluated, and
programs were indexed *before* pathways are evaluated.  If all three types
were dispatched simultaneously (flat `.delay()` per batch), a program task
could execute before its child course task writes the updated
`last_indexed_at`, causing the program to be incorrectly skipped as
non-stale.  The chain-of-groups guarantee eliminates that race.

See ADR 0013 for the full rationale.

### Step 4: Logging and return value

The dispatcher returns a summary dict suitable for CLI/ops visibility, e.g.:

```python
{
  "force": False,
  "dry_run": False,
  "batch_size": 10,
  "index_name": None,
  "dispatched": {
    "course": {"records": 123, "batches": 13},
    "program": {"records": 45, "batches": 5},
    "learnerpathway": {"records": 6, "batches": 1},
  },
}
```

---

## Performance / Query Strategy

Avoid N+1 queries by bulk-loading required state.

For each content type, the dispatcher should:

- bulk fetch `ContentMetadata` for the type (only fields needed, e.g. `content_key`, `modified`).
- bulk fetch `ContentMetadataIndexingState` for the same keys.

For child-staleness checks:

- programs: bulk fetch course states for all child course keys referenced by programs under consideration.
- pathways: bulk fetch program states for all child program keys (UUIDs) referenced by pathways under consideration.

Compute staleness comparisons in Python using dict lookups keyed by `content_key`.

---

## Testing Plan

Add unit tests in `enterprise_catalog/apps/search/tests/test_tasks.py`:

1. **`dry_run=True`**: no `.delay()` calls; summary matches expected counts.
2. **force cache behavior**:
   - `force=True` calls `invalidate_indexing_mappings_cache()`.
   - `force=False` does not.
3. **courses stale detection**:
   - never indexed included
   - modified > last_indexed_at included
   - modified <= last_indexed_at excluded
4. **include_failed**:
   - failed record included when `include_failed=True`
5. **program child-staleness**:
   - program included when any child course last_indexed_at > program last_indexed_at
6. **pathway child-staleness (UUID program keys)**:
   - pathway included when any child program (UUID key) last_indexed_at > pathway last_indexed_at
7. **batching math**:
   - correct number of dispatched tasks based on batch size

---

## Follow-ups (out of Phase 4a scope)

- Implement `dispatch_algolia_indexing_for_catalog_query` for API-triggered catalog refreshes (membership removal detection via Algolia diff).
- Integrate dispatchers into:
  - daily cron chain after `update_content_metadata`
  - API refresh endpoint `EnterpriseCatalogRefreshDataFromDiscovery`
- Add management command wrapper for manual runs.
