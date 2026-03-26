# Algolia Incremental Indexing: Technical Specification

> **Executive Summary**: Replace our monolithic 2+ hour Algolia reindex with an incremental, parallelized batch system. Content changes will index in minutes instead of hours, failed batches retry independently, and the system scales with changes rather than total catalog size. Phased rollout with feature flag allows safe deployment; settings-based cutover enables instant rollback.

---

## Background

Our Algolia reindexing runs as a **single, monolithic operation** that takes **2+ hours** in production and is growing longer as our catalog expands. This creates three critical issues:

| Issue | Impact |
|-------|--------|
| **No partial updates** | A single course metadata or catalog change rebuilds the entire index |
| **Not parallelizable** | One Celery worker, one thread, one long-running API call |
| **Failure = full restart** | If it fails at hour 1:45, we start over from scratch |

The `replace_all_objects()` approach worked years ago at smaller scale but doesn't fit our current data volume or operational needs.

### Current State

```
┌─────────────────────────┐      ┌─────────────────────────┐      ┌─────────────┐
│ update_content_metadata │      │ reindex_algolia         │      │   Algolia   │
│ (cron, ~1hr)            │      │ (cron, scheduled after  │─────▶│   Index     │
│                         │      │  UCM completes, 2+ hrs) │      │             │
│                         │      │ replace_all_objects()   │      └─────────────┘
│ Separate cron jobs,     │      │ Single-threaded         │
│ implicitly chained      │      │ All-or-nothing          │
└─────────────────────────┘      └─────────────────────────┘
```

### Business Alignment

This work directly supports catalog scalability and operational reliability:
- **Scalability**: As we onboard more enterprise customers and content, indexing time grows linearly. Incremental indexing scales with *changes*, not total catalog size.
- **Reliability**: Failed batches retry independently rather than restarting a 2+ hour job.
- **Freshness**: Content changes appear in search results faster (minutes vs. hours).

### Cost & Infrastructure Implications

| Area | Impact |
|------|--------|
| **Algolia write operations** | Reduced — only changed records indexed vs. full replace every run |
| **Celery workers** | Increased task volume but smaller tasks; existing worker pool should suffice initially. |
| **Database** | New `ContentMetadataIndexingState` table (~1 row per ContentMetadata). Staleness queries use indexed fields; minimal impact expected. |
| **Redis/Celery broker** | More tasks dispatched but each is small; no significant increase in memory pressure |

**Net**: No new infrastructure required. Algolia costs likely decrease due to fewer write operations.

---

## Product Requirements

### Definition of Done

1. **Incremental indexing operational**: Content changes index within minutes of `update_content_metadata` completion
2. **Parallelized execution**: Full reindex fans out across multiple Celery workers
3. **Failure resilience**: Failed batches retry independently; progress is preserved
4. **Zero-downtime cutover**: Atomic swap from old index to new index
5. **Rollback capability**: Can revert to old indexing system if issues arise
6. **Observability**: Indexing state trackable per-record; failures visible in admin

### Success Metrics

| Metric | Current | Target |
|--------|---------|--------|
| Full reindex duration | 2+ hours (sequential) | < 30 minutes (parallelized across 10+ workers) |
| Typical incremental reindex | N/A (always full) | < 5 minutes for daily changes |
| Failure recovery | Restart from zero | Retry failed batch only; < 1 minute per batch |
| Granularity | All records every time | Only changed records |
| Index freshness after content sync | 2-3 hours | < 15 minutes |
| Batch success rate | N/A | > 99% (failures auto-retry on next cron) |

---

### Ownership & Parallelism

**Owner**: Alex Dusenbery (tech lead)
**Primary driver**: TBD

**Phase Dependencies**:
```
        Phase 0 (COMPLETE)
              │
       ┌──────┴──────┐
       ▼             ▼
    Phase 1       Phase 2
    (model)       (client)
       │             │
       ├──────┐      │
       ▼      │      │
    Phase 3   │      │
    (tasks)   │      │
       │      │      │
       └──────┴──────┘
              │
              ▼
          Phase 4 (dispatcher)
              │
              ▼
          Phase 5 (command)
              │
              ▼
          Phase 6 (integration)
              │
              ▼
          Phase 7 (validation)
              │
              ▼
          Phase 8 (cutover)
```

**Parallelism opportunities**:
- Because we're building the new index in isolation with our legacy index still in normal operation,
  it's ok to bite off a little at a time, sequentially.
- Phases 1 and 2 can be worked in parallel (no dependencies between them), but that's only a minimal savings
- At Phase 4 and beyond, work is sequential

**Staffing**: 
* Single engineer can likely implement code for most phases. 
* We **must** pair on release, validation, and cutover to reduce risk.

**Rough Level of effort**
Assumptions:
- Pure implementation: 14-21 days
- Code review cycles: 4-5 days
- Buffer for bugs/iteration: 3-5 days

Realistic range: 21-31 focused engineering days.

| Focus Level                | Calendar Time |
|----------------------------|---------------|
| 80%+ dedicated             | 6-8 weeks     |
| 50% dedicated              | 8-12 weeks    |
| Interrupt-driven           | 1-2 quarters  |

---

## Design Overview

### Future State

```
┌─────────────────────────┐      ┌─────────────────────────┐      ┌─────────────┐
│ update_content_metadata │─────▶│ dispatch_algolia_       │─────▶│   Algolia   │
│ (cron, ~1hr)            │      │ indexing (force=True)   │      │   Index     │
│                         │      │                         │      │             │
│ Chains force-reindex    │      │ Batches of 10 records   │      └─────────────┘
│ after completion        │      │ Parallel across workers │
└─────────────────────────┘      └─────────────────────────┘
          │                                 ▲
          │                                 │
          │      ┌─────────────────────────┐│
          │      │ dispatch_algolia_       ││
          └─────▶│ indexing (hourly cron)  │┘
                 │                         │
                 │ Catches stragglers,     │
                 │ retries failures        │
                 └─────────────────────────┘
```

### Key Components

| Component | Purpose |
|-----------|---------|
| `ContentMetadataIndexingState` model | Tracks per-record indexing state, failure history |
| `AlgoliaSearchClient` batch methods | `save_objects_batch()`, `delete_objects_batch()`, `get_object_ids_by_prefix()` |
| Content-type indexing tasks | `index_courses_batch_in_algolia`, `index_programs_batch_in_algolia`, `index_pathways_batch_in_algolia` |
| Dispatcher task | `dispatch_algolia_indexing` — queries stale records, batches, dispatches |
| Management command | `incremental_reindex_algolia` — manual runs, testing against v2 index |
| Feature flag | `ENABLE_INCREMENTAL_ALGOLIA_INDEXING` — controls when to start syncing to new index |
| `ALGOLIA.INDEX_NAME` setting | Controls which Algolia index is active; changing this setting is the cutover mechanism |

---

## Proposed Design & Related Work

### Design Decisions

#### 1. Why Force-Reindex After `update_content_metadata`?

**The consistency problem**: `update_content_metadata` processes catalog queries sequentially. A course in 50 catalogs has its membership built incrementally:

```
Query 1  → Course X in [Catalog A, B]
Query 25 → Course X in [Catalog A, B, C, D, E]
Query 50 → Course X in [Catalog A, B, C, D, E, F, G]  ← final
```

If we indexed immediately on each touch, Algolia would have incomplete membership until Query 50 finishes. By force-reindexing after completion, we ensure consistent catalog membership.

#### 2. Why Cron-Based Instead of Event-Driven?

| Concern | Event-Driven | Cron + Force-Reindex |
|---------|--------------|----------------------|
| Consistency | Indexes partial state mid-sync | Indexes after sync completes |
| Deduplication | Course touched 50x = 50 tasks | Batch after completion |
| Failure handling | Needs explicit retry queue | Cron naturally retries |

This maintains the current cron-based model while adding consistency guarantees through explicit chaining.

#### 3. How Do We Prevent Double-Indexing?

**Record-level deduplication in batch tasks**

Each batch task checks if a record needs indexing before doing work:

```python
if state.last_indexed_at and state.last_indexed_at >= content.modified:
    continue  # Already indexed this version, skip
```

This is the primary deduplication mechanism. Even if the hourly cron dispatcher runs concurrently with a force-reindex (chained after `update_content_metadata`), the batch tasks will skip records that have already been indexed at the current version.

**Why not use `expiring_task_semaphore` on the dispatcher?**

The existing `expiring_task_semaphore` decorator dedupes by `(task_name, args, kwargs)` and bypasses the check when `force=True`. This means:
- Cron (`force=False`) and force-reindex (`force=True`) have different semaphore keys
- `force=True` bypasses the semaphore check entirely

We could extend the semaphore to support a custom key, but it's simpler to accept that concurrent dispatcher runs are harmless — the record-level deduplication ensures no wasted Algolia writes.

#### 4. Why Separate Tasks Per Content Type?

Programs depend on courses; pathways depend on programs. Sequential execution (courses → programs → pathways) ensures correct UUID inheritance without complex cascade logic.

#### 5. Why Batch Size of 10?

Balances parallelism vs overhead. Smaller batches = finer failure granularity, more tasks. Larger batches = fewer tasks, coarser recovery. This is configurable via `ALGOLIA_INDEXING_BATCH_SIZE` setting.

#### 6. Why a New `search` App?

Better isolation and code organization. Separates Algolia indexing concerns from core catalog models and API endpoints. Establishes a clear sub-domain boundary.

### Staleness Detection

We determine what needs reindexing differently per content type:

| Content Type | Stale When |
|--------------|------------|
| Courses | `modified > last_indexed_at` |
| Programs | `modified > last_indexed_at` OR `MAX(course.last_indexed_at) > last_indexed_at` |
| Pathways | `modified > last_indexed_at` OR `MAX(program.last_indexed_at) > last_indexed_at` |

**Key insight for courses**: We rely on `json_metadata.modified` from course-discovery (the upstream source of truth) to determine if content has actually changed. Phase 0 (complete) ensures `ContentMetadata.modified` only updates when this Discovery timestamp differs, making our staleness query reliable.

**Programs/Pathways**: Discovery doesn't provide a `modified` field for these types. Instead, we detect staleness when their child content (courses/programs) has been indexed more recently — indicating their inherited catalog membership may have changed.

### Model Design

```python
class ContentMetadataIndexingState(TimeStampedModel):
    """
    Tracks per-record Algolia indexing state for ContentMetadata.
    """
    content_metadata = models.OneToOneField(
        ContentMetadata,
        on_delete=models.CASCADE,
        related_name='indexing_state',
    )
    last_indexed_at = models.DateTimeField(null=True)
    removed_from_index_at = models.DateTimeField(null=True)
    algolia_object_ids = models.JSONField(default=list)  # Tracks shards
    last_failure_at = models.DateTimeField(null=True)
    failure_reason = models.TextField(null=True)

    class Meta:
        indexes = [
            models.Index(fields=['last_indexed_at']),
            models.Index(fields=['last_failure_at']),
        ]
```

### Task Design

Each content-type batch task (`index_courses_batch_in_algolia`, etc.) follows this pattern:

1. For each content_key in batch:
   - Fetch `ContentMetadata` and `ContentMetadataIndexingState`
   - Apply record-level deduplication (see Design Decision #3)
   - Query Algolia for existing shards
   - Generate Algolia objects (reuses existing `_get_algolia_products_for_batch`)
   - `save_objects_batch()` for upserts
   - `delete_objects_batch()` for orphaned shards
   - Update `ContentMetadataIndexingState`
2. On per-record failure: call `mark_as_failed()`, continue to next record
3. Return results dict with indexed/skipped/failed counts

### Integration with `update_content_metadata`

```python
chain(
    # ... existing metadata tasks ...
    update_full_content_metadata_task.si(),
    # NEW: Force reindex after metadata is consistent (controlled by feature flag)
    dispatch_algolia_indexing.si(force=True),
)
```

**Feature Flag**: `ENABLE_INCREMENTAL_ALGOLIA_INDEXING` controls whether the chain includes the new dispatcher. This allows us to build and deploy all phases before activating.

---

## No-gos

The following are explicitly out of scope for this project:

1. **Schema changes**: Algolia object schema, sharding strategy, and UUID aggregation remain unchanged
2. **Real-time event-driven indexing**: We are not implementing webhooks or signals that index immediately on content change
3. **Replica index management**: The replica index will continue to be managed by Algolia's replica sync
4. **Search API changes**: No changes to how consumers query Algolia
5. **Historical backfill of indexing state**: We will not attempt to retroactively populate `last_indexed_at` for existing records; the first run will index everything

---

## Open Questions & Considerations

| Question | Current Thinking | Resolution Needed |
|----------|------------------|-------------------|
| Should we add Datadog metrics for indexing latency? | Yes, useful for monitoring | Decide specific metrics during implementation |
| How long to retain failure_reason history? | Single most recent failure | May want to log to external system for trends |
| Should batch size be configurable per content type? | Start with uniform 10 | Tune based on observed performance |
| Celery queue for indexing tasks? | Use default queue initially | May need dedicated queue if volume is high |

---

## Build Phases

### How We Get There

**Development Strategy**: Build incrementally in a new `search` app, completely isolated from the existing indexing flow. The legacy `reindex_algolia` command and cron continue operating unchanged throughout development.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           DEVELOPMENT (Phases 1-5)                          │
│                                                                             │
│   Legacy Flow (unchanged)          New Flow (being built)                   │
│   ┌─────────────────────┐          ┌─────────────────────┐                  │
│   │ reindex_algolia     │          │ search app          │                  │
│   │ (cron, 2+ hrs)      │───────▶  │ - new model         │                  │
│   │ replace_all_objects │  v1      │ - new tasks         │  (no writes yet) │
│   └─────────────────────┘ index    │ - new dispatcher    │                  │
│                                    └─────────────────────┘                  │
└─────────────────────────────────────────────────────────────────────────────┘
                                         │
                                         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           VALIDATION (Phases 6-7)                           │
│                                                                             │
│   Legacy Flow (unchanged)          New Flow (writing to v2)                 │
│   ┌─────────────────────┐          ┌─────────────────────┐                  │
│   │ reindex_algolia     │          │ dispatch_algolia    │                  │
│   │ (cron, 2+ hrs)      │───────▶  │ (manual runs)       │───────▶          │
│   │ replace_all_objects │  v1      │ incremental batches │  v2              │
│   └─────────────────────┘ index    └─────────────────────┘ index            │
│                                                                             │
│   Both indices exist; frontends still read v1; v2 validated against v1      │
└─────────────────────────────────────────────────────────────────────────────┘
                                         │
                                         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           CUTOVER (Phase 8)                                 │
│                                                                             │
│   8a: Enable dual-index     Secured API keys allow both v1 and v2           │
│          │                                                                  │
│          ▼                                                                  │
│   8b: Switch writes         enterprise-catalog writes to v2                 │
│          │                  (frontends still read v1, which is allowed)     │
│          ▼                                                                  │
│   8c: Migrate frontends     Each frontend switches to v2 independently      │
│          │                  (no coordination required)                      │
│          ▼                                                                  │
│   8d: Cleanup               Remove v1 from allowed list; disable legacy     │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Why No Downtime**: The secured API key's `restrictIndices` parameter controls which indices frontends can access. By allowing both v1 and v2 during transition (Phase 8a), frontends continue working regardless of which index they're configured to read.

**Why No Staleness**: During Phase 8b, writes switch to v2 while some frontends still read v1. This creates brief staleness on v1 (new content appears on v2 first), but v1 already had hours of staleness from the legacy 2+ hour reindex cycle. Once each frontend migrates to v2 (Phase 8c), it gets the freshest data.

**Rollback at Any Point**: Because both indices exist and the secured key allows both, rollback is instant: revert the setting change. No data migration or index manipulation required.

---

### Phase 0: Prerequisite Fix (COMPLETE)

**Status**: Merged to master and running in production

**Summary**: Ensure `ContentMetadata.modified` only updates when Discovery content actually changes, enabling reliable staleness detection.

**Commits**:
- `d5ed823` feat: skip metadata updates when Discovery content unchanged
- `deb60ba` fix: include modified in course fields plucked from Discovery
- `138fe69` fix: preserve catalog associations for skipped courses

**Tech Debt (Non-blocking)**: The current implementation uses Discovery's `detail_fields` query param which adds overhead by expanding course run sub-serializers. A future course-discovery change to support `include_modified` param will improve performance. This is blocked on course-discovery build pipeline fixes.

---

### Phase 1: Create `search` App and Tracking Model

**Summary**: Create the new `search` Django app and `ContentMetadataIndexingState` model to track per-record indexing state.

**Scope**:
- Create `enterprise_catalog/apps/search/` app structure
- Add `ContentMetadataIndexingState` model (see Model Design section for field definitions)
- Add helper methods: `mark_as_indexed()`, `mark_as_failed()`, `mark_as_removed()`, `is_stale` property, `get_or_create_for_content()`
- Register model in Django admin
- Add app to `INSTALLED_APPS`

**File Changes**:
| File | Change |
|------|--------|
| `enterprise_catalog/apps/search/__init__.py` | Create |
| `enterprise_catalog/apps/search/apps.py` | Create app config |
| `enterprise_catalog/apps/search/models.py` | Create `ContentMetadataIndexingState` |
| `enterprise_catalog/apps/search/admin.py` | Register model in admin |
| `enterprise_catalog/apps/search/migrations/0001_initial.py` | Generated migration |
| `enterprise_catalog/settings/base.py` | Add to `INSTALLED_APPS` |

**Acceptance Criteria**:
- [ ] `ContentMetadataIndexingState` model exists with fields per Model Design section
- [ ] `is_stale` property correctly identifies records needing reindex
- [ ] Model visible in Django admin
- [ ] Migration applies cleanly

**Verification**:
- Unit tests for model methods (`mark_as_indexed`, `mark_as_failed`, `is_stale`)
- Unit tests for `get_or_create_for_content` class method
- Manual: Run migration in devstack, verify table created
- Manual: Create records via Django admin, verify fields work

---

### Phase 2: Algolia Client Batch Methods

**Summary**: Add batch upsert and delete methods to `AlgoliaSearchClient` to support incremental indexing.

**Scope**:
- Add `save_objects_batch(objects, index_name=None)` method
- Add `delete_objects_batch(object_ids, index_name=None)` method
- Add `get_object_ids_by_prefix(prefix, index_name=None)` method for shard discovery
- Add `_get_index(index_name=None)` helper to support custom index names

**File Changes**:
| File | Change |
|------|--------|
| `enterprise_catalog/apps/api_client/algolia.py` | Add batch methods |
| `enterprise_catalog/apps/api_client/tests/test_algolia.py` | Add tests |

**Acceptance Criteria**:
- [ ] `save_objects_batch()` upserts objects without affecting other records
- [ ] `delete_objects_batch()` removes specified objects by ID
- [ ] `get_object_ids_by_prefix()` returns all object IDs matching a content key prefix
- [ ] All methods support optional `index_name` param for targeting v2 index
- [ ] All methods have appropriate error handling and logging

**Verification**:
- Unit tests with mocked Algolia client
- Integration test (manual): Save and retrieve objects from a test index
- Integration test (manual): Delete objects, verify removal

---

### Phase 3: Content-Type Indexing Tasks

**Summary**: Create Celery tasks for indexing batches of each content type (courses, programs, pathways).

**Scope**:
- Create `index_courses_batch_in_algolia` task
- Create `index_programs_batch_in_algolia` task
- Create `index_pathways_batch_in_algolia` task
- Implement shared `_index_content_batch()` logic:
  - Filter content keys by staleness (unless `force=True`)
  - Generate Algolia objects (reuse existing `_get_algolia_products_for_batch`)
  - Query existing shards from Algolia
  - Save new objects, delete orphaned shards
  - Update `ContentMetadataIndexingState`
  - Handle failures per-record with `mark_as_failed()`

**File Changes**:
| File | Change |
|------|--------|
| `enterprise_catalog/apps/search/tasks.py` | Create indexing tasks |
| `enterprise_catalog/apps/search/tests/test_tasks.py` | Add tests |

**Acceptance Criteria**:
- [ ] Tasks accept `content_keys`, `index_name`, and `force` parameters
- [ ] Tasks skip already-indexed records unless `force=True`
- [ ] Tasks update `ContentMetadataIndexingState` on success
- [ ] Tasks mark records as failed on error (without failing entire batch)
- [ ] Tasks log indexed/skipped/failed counts
- [ ] Tasks return results dict with counts

**Verification**:
- Unit tests with mocked Algolia client and ContentMetadata
- Unit tests for staleness filtering logic
- Unit tests for failure handling (one record fails, others succeed)
- Manual: Index a small batch to test index, verify objects appear

---

### Phase 4: Dispatcher Task

**Summary**: Create the dispatcher task that queries for stale/failed records and dispatches batch indexing tasks.

**Scope**:
- Create `dispatch_algolia_indexing` task
- Implement `_get_stale_content_keys(content_type, force, include_failed)` helper
- Implement batching logic (default batch size 10, configurable)
- Dispatch tasks in dependency order: courses → programs → pathways
- Support `dry_run` mode for testing
- Add `ALGOLIA_INDEXING_BATCH_SIZE` setting

**File Changes**:
| File | Change |
|------|--------|
| `enterprise_catalog/apps/search/tasks.py` | Add dispatcher task |
| `enterprise_catalog/apps/search/tests/test_tasks.py` | Add tests |
| `enterprise_catalog/settings/base.py` | Add `ALGOLIA_INDEXING_BATCH_SIZE` |

**Acceptance Criteria**:
- [ ] Dispatcher queries stale courses (never indexed OR modified > last_indexed_at)
- [ ] Dispatcher queries stale programs/pathways (per Staleness Detection: child content indexed more recently)
- [ ] Dispatcher queries failed records for retry
- [ ] Dispatcher batches content keys into groups of 10 (configurable)
- [ ] Dispatcher dispatches tasks in correct order (courses first)
- [ ] `dry_run=True` logs what would be dispatched without dispatching
- [ ] Dispatcher returns summary of dispatched tasks

**Verification**:
- Unit tests for stale content key queries
- Unit tests for batching logic
- Unit tests for dry_run mode
- Manual: Run dispatcher with `dry_run=True`, verify logged batches
- Manual: Run dispatcher against test index, verify tasks execute

---

### Phase 5: Management Command

**Summary**: Create management command for manual incremental reindexing runs.

**Scope**:
- Create `incremental_reindex_algolia` command
- Support arguments:
  - `--content-type` (course/program/learnerpathway)
  - `--index-name` (for targeting v2 index)
  - `--force-all` (reindex regardless of staleness)
  - `--dry-run` (log without executing)
  - `--no-async` (run synchronously for debugging)

**File Changes**:
| File | Change |
|------|--------|
| `enterprise_catalog/apps/search/management/__init__.py` | Create |
| `enterprise_catalog/apps/search/management/commands/__init__.py` | Create |
| `enterprise_catalog/apps/search/management/commands/incremental_reindex_algolia.py` | Create command |
| `enterprise_catalog/apps/search/management/commands/tests/test_incremental_reindex_algolia.py` | Add tests |

**Acceptance Criteria**:
- [ ] Command invokes `dispatch_algolia_indexing` with correct parameters
- [ ] Command prints summary of dispatched tasks
- [ ] `--dry-run` shows what would be indexed without indexing
- [ ] `--force-all` reindexes all content regardless of staleness
- [ ] `--index-name` allows targeting alternate index (for v2 testing)
- [ ] `--no-async` runs synchronously for debugging

**Verification**:
- Unit tests for argument parsing
- Manual: Run `--dry-run` in devstack, verify output
- Manual: Run `--force-all --index-name enterprise_catalog_v2` against v2 index

---

### Phase 6: Feature Flag and Integration

**Summary**: Add feature flag and integrate dispatcher into `update_content_metadata` chain.

**Scope**:
- Add `ENABLE_INCREMENTAL_ALGOLIA_INDEXING` feature flag (default: False)
- Modify `update_content_metadata` task chain to conditionally include dispatcher
- Document flag in settings

**File Changes**:
| File | Change |
|------|--------|
| `enterprise_catalog/apps/api/tasks.py` | Add conditional chaining |
| `enterprise_catalog/settings/base.py` | Add feature flag |

**Acceptance Criteria**:
- [ ] Feature flag `ENABLE_INCREMENTAL_ALGOLIA_INDEXING` exists (default False)
- [ ] When flag is False, existing behavior unchanged
- [ ] When flag is True, `dispatch_algolia_indexing(force=True)` chains after metadata sync
- [ ] Flag is documented in settings

**Verification**:
- Unit tests for conditional chaining logic
- Manual: With flag False, verify old reindex behavior
- Manual: With flag True, verify dispatcher runs after metadata sync

---

### Phase 7: Validation Against v2 Index

**Summary**: Create v2 index and validate incremental indexing produces identical results to monolithic reindex.

**Scope**:
- Create `enterprise_catalog_v2` index in Algolia (copy settings from v1)
- Run full incremental reindex to v2: `./manage.py incremental_reindex_algolia --force-all --index-name enterprise_catalog_v2`
- Run comparison script to validate parity
- Document any discrepancies and resolve

**Artifacts**:
- `scripts/compare_algolia_indices.py` — comparison script (exists in prototype branch)

**Acceptance Criteria**:
- [ ] v2 index created with identical settings to v1
- [ ] Full incremental reindex completes successfully
- [ ] Comparison script reports record count match
- [ ] Comparison script reports no content discrepancies (or discrepancies are understood/acceptable)
- [ ] Search behavior validated in staging environment

**Verification**:
- Run comparison script, document results
- Manual QA: Perform sample searches against v2 index
- Manual QA: Verify catalog membership for sample courses

---

### Phase 8: Zero-Downtime Cutover

**Summary**: Multi-step cutover enabling gradual frontend migration with no service interruption.

**Background**: Multiple frontends read from Algolia using index names from their own env vars. The secured API key's `restrictIndices` controls which indices each frontend can access. See `docs/algolia-reindexing/algolia-frontend-architecture.md` for full architecture diagram.

#### Phase 8a: Enable Dual-Index Support

**Scope**:
- Add new setting: `ALGOLIA['ALLOWED_INDEX_NAMES']` containing both v1 and v2 index names
- Update `generate_secured_api_key()` to use `ALLOWED_INDEX_NAMES` for `restrictIndices`
- Deploy enterprise-catalog

**File Changes**:
| File | Change |
|------|--------|
| `enterprise_catalog/apps/api_client/algolia.py` | Update `generate_secured_api_key()` to use `ALLOWED_INDEX_NAMES` |
| `enterprise_catalog/settings/base.py` | Add `ALGOLIA['ALLOWED_INDEX_NAMES']` |

**Acceptance Criteria**:
- [ ] `ALLOWED_INDEX_NAMES` setting exists with both v1 and v2 index names
- [ ] Secured API keys include both indices in `restrictIndices`
- [ ] Existing frontend behavior unchanged (still reading v1)

#### Phase 8b: Switch Writes to v2

**Scope**:
- Update `ALGOLIA['INDEX_NAME']` to v2 index name
- Enable `ENABLE_INCREMENTAL_ALGOLIA_INDEXING` flag
- Configure hourly cron for `dispatch_algolia_indexing`
- Deploy enterprise-catalog

**Acceptance Criteria**:
- [ ] Incremental indexing writes to v2
- [ ] Hourly cron configured and running
- [ ] Frontends still work (reading v1, secured key allows both)

#### Phase 8c: Migrate Frontends

**Scope**: Update each frontend's `ALGOLIA_INDEX_NAME` env var to v2. Can be done gradually, any order.

| Frontend | Env Var | Deploy Independently |
|----------|---------|---------------------|
| frontend-app-learner-portal-enterprise | `ALGOLIA_INDEX_NAME` | Yes |
| frontend-app-admin-portal | `ALGOLIA_INDEX_NAME` | Yes |
| frontend-app-enterprise-public-catalog | `ALGOLIA_INDEX_NAME` | Yes |

**Acceptance Criteria**:
- [ ] All frontends updated to v2 index name
- [ ] Search functionality verified on each frontend
- [ ] No increase in error rates

#### Phase 8d: Cleanup

**Scope**:
- Remove v1 from `ALLOWED_INDEX_NAMES`
- Disable old `reindex_algolia` cron job
- Deprecate old `reindex_algolia` command and `replace_all_objects` code path
- (Optional) Delete v1 index from Algolia after stability period

**Acceptance Criteria**:
- [ ] v1 removed from `ALLOWED_INDEX_NAMES`
- [ ] Old cron disabled
- [ ] Monitoring confirms stable operation for 1+ week

---

**Verification** (across all Phase 8 sub-phases):
- Monitor Datadog for indexing task success/failure rates
- Monitor Algolia dashboard for index health
- Manual QA: Verify search results on each frontend after migration

**Rollback Plan** (at any sub-phase):

| Current Phase | Rollback Steps |
|---------------|----------------|
| 8a | Revert `generate_secured_api_key()` change; remove `ALLOWED_INDEX_NAMES` |
| 8b | Revert `INDEX_NAME` to v1; disable feature flag; frontends unaffected |
| 8c | Revert individual frontend's `ALGOLIA_INDEX_NAME` to v1 |
| 8d | Re-add v1 to `ALLOWED_INDEX_NAMES`; re-enable old cron if needed |

**Why This Is Zero-Downtime**:

| During Phase | Writes | Reads | Status |
|--------------|--------|-------|--------|
| 8a | v1 | v1 | No change, enabling future |
| 8b | v2 | v1 (both valid) | Brief staleness on v1 |
| 8c | v2 | v2 (gradual) | Each frontend migrates independently |
| 8d | v2 | v2 | Final state |

---

## Consequences / Outcomes

### Positive Outcomes

1. **Operational resilience**: Individual batch failures don't require full restart
2. **Scalability**: Indexing time scales with changes, not total catalog size
3. **Faster feedback**: Content changes visible in search within minutes
4. **Better observability**: Per-record indexing state visible in admin
5. **Reduced Algolia costs**: Fewer write operations when content unchanged

### Potential Negative Outcomes

1. **Increased complexity**: More moving parts than monolithic approach
2. **Migration risk**: Cutover requires careful validation
3. **New failure modes**: Batch task failures need monitoring

### Unexpected Outcomes to Watch

1. **Celery queue depth**: Many small tasks may queue up; monitor for contention
2. **Database load**: Staleness queries hit ContentMetadata table; indexes should help
3. **Algolia rate limits**: Many parallel requests; may need throttling

---

## Risks

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| v2 index doesn't match v1 | Medium | High | Thorough comparison script; resolve discrepancies before cutover |
| Cutover causes search outage | Very Low | High | Zero-downtime approach; dual-index support during transition; gradual frontend migration |
| Batch failures accumulate | Low | Medium | Hourly cron retries failures; monitoring alerts |
| Celery worker overload | Low | Medium | Configurable batch size; dedicated queue if needed |
| Frontend migration coordination | Low | Low | Frontends can migrate independently; no coordination required |
| Course-discovery optimization never ships | Medium | Low | Current implementation works, just slower; non-blocking |

---

## Cross-functional Requirements

### Security

- No new authentication/authorization requirements
- Algolia API keys already managed securely in settings
- No PII changes; same data indexed as before

### Scalability

- Parallelized across Celery workers (horizontal scaling)
- Batch size configurable per environment
- Staleness queries use indexed fields

### Observability

- Celery task success/failure tracked in existing monitoring
- `ContentMetadataIndexingState` queryable in Django admin
- Failure reasons stored per-record
- Recommend adding Datadog metrics for indexing latency and batch sizes

### Alerting (Required Before Cutover)

| Alert | Condition | Severity | Action |
|-------|-----------|----------|--------|
| Dispatcher task failure | `dispatch_algolia_indexing` fails | P2 | Check logs; will auto-retry on next cron |
| High batch failure rate | > 5% of batch tasks fail in 1 hour | P2 | Investigate failure reasons in admin |
| Stale index | No successful indexing in 4+ hours | P1 | Check Celery workers; verify cron running |
| Algolia API errors | Algolia SDK errors spike | P2 | Check Algolia status; may need throttling |

### Operational Runbook

**Investigating indexing failures**:
1. Check `ContentMetadataIndexingState` in Django admin — filter by `last_failure_at` not null
2. Review `failure_reason` field for patterns
3. Check Celery task logs for stack traces
4. Common causes: Algolia rate limits, ContentMetadata deleted mid-task, malformed JSON metadata

**Manual reindex of failed records**:
```bash
# Force reindex of all failed records (including any previously failed ones)
./manage.py incremental_reindex_algolia --force-all

# Force reindex of all records for a specific content type
./manage.py incremental_reindex_algolia --content-type course --force-all

# Dry run to see what would be indexed
./manage.py incremental_reindex_algolia --dry-run
```

**Emergency: Revert to old indexing system**:
See Phase 8 Rollback Plan.

**Checking index health**:
- Algolia dashboard: record count, last update time
- Django admin: `ContentMetadataIndexingState` — count of records with `last_indexed_at` in last 24h
- Datadog: task success/failure rates

### Testing

**Unit Testing**:
- Unit tests for all new code (models, tasks, management command)
- Mock Algolia client for isolated testing
- Test failure scenarios (Algolia API errors, missing ContentMetadata)

**Test Data Requirements**:
| Scenario | Data Needed |
|----------|-------------|
| Basic indexing | Courses, programs, pathways with valid metadata |
| Multi-catalog membership | Course belonging to 5+ catalogs |
| Staleness detection | ContentMetadata with varying `modified` timestamps |
| Program/pathway dependencies | Program with 10+ courses; pathway with programs |
| Failure handling | ContentMetadata with malformed JSON (for error path testing) |

**Performance Testing** (Phase 7):
- Benchmark full reindex: time with 1 worker vs. 5 workers vs. 10 workers
- Measure incremental reindex of 100 changed records
- Monitor Celery queue depth during parallel indexing
- Verify Algolia rate limits not exceeded

**QA Test Cases** (Phase 7):

| Test Case | Steps | Expected Result |
|-----------|-------|-----------------|
| Search by course title | Search "Introduction to Python" | Course appears in results |
| Filter by catalog | Filter to specific enterprise catalog | Only catalog's content shown |
| Course in multiple catalogs | Search course that's in 3 catalogs | Course appears; all catalog memberships correct |
| Program search | Search for program by title | Program appears with correct course count |
| Pathway search | Search for pathway | Pathway appears with correct structure |
| Recently updated course | Modify course, wait for reindex, search | Updated content appears |
| Empty search | Search with no filters | All content types appear |

**Regression Testing**:
- Run existing search integration tests (if any) against v2 index
- Compare search result counts between v1 and v2 for sample queries
- Verify facet counts match between indices

**Rollback Testing** (Phase 7, before cutover):
- Practice rollback procedure in staging
- Verify search works after rollback to v1
- Document rollback time (target: < 5 minutes)

---

## Dependencies

| Dependency | Type | Status | Notes |
|------------|------|--------|-------|
| Phase 0 (staleness fix) | Internal | Complete | Merged to master |
| Algolia SDK | External | Stable | Already in use |
| Celery/Redis | External | Stable | Already in use |
| course-discovery `include_modified` | External | Blocked | Non-blocking tech debt; improves performance |

---

## Artifacts

| Artifact | Location | Purpose |
|----------|----------|---------|
| Pitch document | `docs/algolia-reindexing/pitch.md` | Business justification |
| Tech spec | `docs/algolia-reindexing/tech-spec.md` | Shovel-ready implementation guide |
| Prototype branch | `aed/reindex-algolia-plan` | Reference implementation (not for direct use) |
| Comparison script | `scripts/compare_algolia_indices.py` (prototype branch) | v1/v2 validation |
