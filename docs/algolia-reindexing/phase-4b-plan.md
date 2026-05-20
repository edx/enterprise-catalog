# Phase 4b Plan: Catalog-Query Dispatcher (membership + removals → batch → dispatch)

This document captures the implementation plan for **Phase 4b** of the incremental Algolia indexing project in `edx/enterprise-catalog`.

Scope: implement the **catalog-query-specific dispatcher task** that indexes all content associated with a given `CatalogQuery`, and also handles **membership removals** by diffing Algolia vs. the database.

> Note: This plan is intentionally detailed so it can be used later to craft an ADR.

---

## Background / Context

The incremental indexing system uses a **dispatcher → parallel batch tasks** architecture.

Phase 4 includes two dispatcher tasks:

1. **Phase 4a**: scheduled dispatcher for stale/failed records (`dispatch_algolia_indexing`)
2. **Phase 4b**: **catalog-query-specific dispatcher** (`dispatch_algolia_indexing_for_catalog_query`)

Phase 4b is used by the API refresh flow (POST to `EnterpriseCatalogRefreshDataFromDiscovery`) when incremental indexing is enabled.

The tricky part: **membership removals**. When content is removed from a catalog query, its `ContentMetadata.modified` may not change, so staleness detection alone won’t pick it up. We must reindex removed content so the Algolia facet membership is updated (old facets removed).

---

## Goals

- Index all content that is currently a member of a given `CatalogQuery`.
- Detect membership removals by querying Algolia for content currently facet-tagged with this catalog query and diffing against DB membership.
- Dispatch the existing Phase 3 batch indexing tasks in small batches (default 10).
- Support `dry_run` for safe testing.
- Support `force` to bypass per-record staleness checks inside batch tasks (useful for correctness after large membership changes).

---

## Non-goals

- No attempt to "stream" changes; this is dispatcher-based, invoked per API refresh.
- No attempt to track pre-update membership state in the DB; we use Algolia as "what was indexed" and the DB as "what should be indexed".

---

## Key Decisions

### 1) Removals are detected via Algolia diff, not DB history

We will *not* try to snapshot membership before `update_catalog_metadata` runs. Instead:

- Query Algolia for currently indexed aggregation keys tagged with the catalog query facet.
- Query DB for current membership aggregation keys for that catalog query.
- Compute removed keys as a set difference.

This keeps the API refresh flow simpler and uses Algolia as the “what is currently indexed” reference.

### 2) Use `aggregation_key` format for set diff

We do set math using the canonical form:

- `"{content_type}:{content_key}"`

This matches the legacy generator output and the structures used elsewhere in the incremental system.

### 3) Partitioning is not done here

Like Phase 4a, we will not re-run partition helpers directly. Indexability constraints are handled by:

- the batch task’s existing logic (it checks against `mappings.all_indexable_content_keys` and can route to REMOVED)
- and by scoping the DB membership query to `ContentMetadata` rows actually present

---

## Proposed Public API

### Celery Task: `dispatch_algolia_indexing_for_catalog_query`

**Location**: `enterprise_catalog/apps/search/tasks.py`

**Signature**:

```python
# Celery shared_task — invoke via .delay() or .apply_async()
dispatch_algolia_indexing_for_catalog_query(
    catalog_query_id,   # positional; CatalogQuery primary key (int)
    dry_run=False,
    force=False,
    include_failed=True,
    index_name=None,
)
```

**Parameters**:

- `catalog_query_id`: required; the `CatalogQuery` primary key
- `dry_run`: compute & log work, but do not enqueue batch tasks
- `force`: passed through to batch tasks; when True, batch tasks bypass their per-record skip (`last_indexed_at >= modified`)
- `include_failed`: include records with `last_failure_at` (useful if API refresh is also expected to “unstick” failures)
- `index_name`: optional index override (e.g., v2) passed through to batch tasks and Algolia client methods used by the dispatcher

---

## Dispatcher Algorithm

### Step 0: Load inputs

1. Load `CatalogQuery` by id.
2. Obtain an initialized Algolia client (same initialization pattern as existing tasks/client helpers).

### Step 1: Compute DB membership aggregation keys

Build:

- `db_aggregation_keys = {f"{cm.content_type}:{cm.content_key}" for cm in catalog_query.content_metadata.all()}`

Notes:
- We’ll ensure this query is efficient (values_list + iterator if needed).
- This yields the “should be indexed” membership set.

### Step 2: Compute Algolia membership aggregation keys (currently indexed)

Call the Algolia helper from Phase 2:

- `algolia_aggregation_keys = algolia_client.get_aggregation_keys_for_catalog_query(catalog_query.uuid, index_name=index_name)`

This yields the “is currently indexed under this facet” set.

### Step 3: Diff to find removed content

Compute:

- `removed_aggregation_keys = algolia_aggregation_keys - db_aggregation_keys`
- `all_aggregation_keys_to_index = db_aggregation_keys | removed_aggregation_keys`

Rationale:
- DB members must be indexed to reflect new membership.
- Removed content must be indexed to remove the stale facet(s).

### Step 4: Convert aggregation keys → content keys grouped by type

Parse each aggregation key into `(content_type, content_key)`.

Group by content type into lists:
- courses
- programs
- pathways

(Other content types may exist in Algolia / ContentMetadata; for Phase 4b we will either ignore unknown types or log them explicitly and skip.)

### Step 5: Optional stale/failed filtering (when force=False)

Even though API refresh is generally “index everything in this catalog”, we still may want to avoid unnecessary work:

- If `force=True`: skip filtering, dispatch all keys found.
- If `force=False`:
  - Apply the same per-type staleness logic as Phase 4a: courses use `ContentMetadata.modified > last_indexed_at`; programs and pathways additionally check child-staleness (re-dispatch when a child was indexed more recently than the parent).
  - Additionally, if `include_failed=True`, include keys with `last_failure_at` regardless of staleness.

Important: we should be careful not to filter out “removed” content due to staleness rules, because removed content’s `modified` likely won’t change. For removed keys, we should treat them as “must index” regardless of staleness when the key appears in `removed_aggregation_keys`.

**Proposed rule**:
- Always include removed keys.
- For DB-member keys, apply stale/failed filtering only when `force=False`.

### Step 6: Batch and dispatch

- Batch size: `settings.ALGOLIA_INDEXING_BATCH_SIZE` (default 10)
- Enqueue the existing batch tasks per content type:
  - `index_courses_batch_in_algolia.delay(content_keys=batch, index_name=index_name, force=force)`
  - `index_programs_batch_in_algolia.delay(...)`
  - `index_pathways_batch_in_algolia.delay(...)`

If `dry_run=True`, do not enqueue; return/log “would dispatch” summary.

### Step 7: Return summary

Return a dict such as:

```python
{
  "catalog_query_id": 123,
  "force": False,
  "dry_run": True,
  "batch_size": 10,
  "db_membership_count": 456,
  "algolia_membership_count": 470,
  "removed_count": 14,
  "dispatched": {
    "course": {"records": 300, "batches": 30},
    "program": {"records": 120, "batches": 12},
    "learnerpathway": {"records": 50, "batches": 5},
  },
}
```

---

## Performance / Query Strategy

- DB membership query should be `values_list("content_type", "content_key")` to avoid loading full models.
- Indexing-state lookups (for optional filtering) should be bulk queries keyed by content_key and content_type, not per-record queries.
- Algolia query should use the batch-friendly facet/browse API via the existing `get_aggregation_keys_for_catalog_query` helper.

---

## Testing Plan

Add unit tests in `enterprise_catalog/apps/search/tests/test_tasks.py` (or a new dispatcher-focused test module if preferred):

1. **Diff math**: removed keys are computed as `algolia - db`
2. **Removed keys are always included** even when `force=False` and even if not stale
3. **Grouping by content type** from aggregation keys is correct
4. **Batch sizing** matches `ALGOLIA_INDEXING_BATCH_SIZE`
5. **dry_run**: no `.si()` calls and `dispatched` counts reflect what would run
6. **index_name** is passed through to:
   - Algolia diff query helper
   - batch tasks
7. **include_failed**: failed keys included even if not stale (for DB-member keys)

---

## Follow-ups (out of Phase 4b scope)

- Integration into the API refresh endpoint (Phase 6), guarded by the feature flag.
- Observability improvements (metrics around removed count / dispatcher run time).
