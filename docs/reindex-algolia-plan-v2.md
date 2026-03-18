# Algolia Incremental Indexing: Implementation Plan

## Overview

This plan migrates Algolia indexing from a monolithic `replace_all_objects()` approach to an incremental, parallelizable batch system.

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

**Problems**: 2+ hour duration, not parallelizable, failure = full restart, rebuilds entire index even for small changes.

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

**Benefits**: Automatic indexing, parallelized, failure-resilient, only indexes changed records.

### Summary

| Metric | Current | Future |
|--------|---------|--------|
| Trigger | Cron (separate schedules, implicit chain) | Cron (explicit chain after metadata sync) |
| Duration | 2+ hours (sequential) | Parallelized across workers |
| Failure impact | Full restart | Retry failed batch only |
| Granularity | All records every time | Only changed records |

---

## Design Decisions

### Why Force-Reindex After `update_content_metadata`?

**The consistency problem**: `update_content_metadata` processes catalog queries sequentially. A course in 50 catalogs has its membership built incrementally:

```
Query 1  → Course X in [Catalog A, B]
Query 25 → Course X in [Catalog A, B, C, D, E]
Query 50 → Course X in [Catalog A, B, C, D, E, F, G]  ← final
```

If we indexed immediately on each touch, Algolia would have incomplete membership until Query 50 finishes. By force-reindexing after completion, we ensure consistent catalog membership.

### Why Cron-Based Instead of Event-Driven?

| Concern | Event-Driven | Cron + Force-Reindex |
|---------|--------------|----------------------|
| Consistency | Indexes partial state mid-sync | Indexes after sync completes |
| Deduplication | Course touched 50x = 50 tasks | Batch after completion |
| Failure handling | Needs explicit retry queue | Cron naturally retries |

This maintains the current cron-based model while adding consistency guarantees through explicit chaining.

### How Do We Prevent Double-Indexing?

Record-level deduplication inside each task. Before indexing a record, check if it's already been indexed at the current version:

```python
if state.last_indexed_at and state.last_indexed_at >= content.modified:
    continue  # Already indexed this version, skip
```

This handles overlap between cron and force-reindex gracefully — tasks may dispatch with overlapping batches, but each record is only actually indexed if needed.

### Why Separate Tasks Per Content Type?

Programs depend on courses; pathways depend on programs. Sequential execution (courses → programs → pathways) ensures correct UUID inheritance without complex cascade logic.

### Why Batch Size of 10?

Balances parallelism vs overhead. Smaller batches = finer failure granularity, more tasks. Larger batches = fewer tasks, coarser recovery.

### Why a New Tracking Model?

A new `ContentMetadataIndexingState` model tracks per-record indexing state:
- `last_indexed_at`: When this record was last successfully indexed
- `algolia_object_ids`: Which Algolia shards exist for this record (for cleanup)
- `last_failure_at` / `failure_reason`: For retry logic

This enables efficient staleness queries and failure tracking without modifying `ContentMetadata` itself.

### Staleness Detection

We determine what needs reindexing differently per content type:

| Content Type | Stale When |
|--------------|------------|
| Courses | `modified > last_indexed_at` |
| Programs | `modified > last_indexed_at` OR `MAX(course.last_indexed_at) > last_indexed_at` |
| Pathways | `modified > last_indexed_at` OR `MAX(program.last_indexed_at) > last_indexed_at` |

**Key insight for courses**: We rely on `json_metadata.modified` from course-discovery (the upstream source of truth) to determine if content has actually changed. Phase 0 ensures `ContentMetadata.modified` only updates when this Discovery timestamp differs, making our staleness query reliable.

**Programs/Pathways**: Discovery doesn't provide a `modified` field for these types. Instead, we detect staleness when their child content (courses/programs) has been indexed more recently — indicating their inherited catalog membership may have changed.

---

## Implementation Phases

### TL;DR

| Phase | Summary | Risk | LOE |
|-------|---------|------|-----|
| 0 | Fix: Skip metadata updates when Discovery content unchanged | Low | Low |
| 1 | New `ContentMetadataIndexingState` model | Low | Low |
| 2 | Add Algolia client batch methods | Low | Low |
| 3 | Three content-type indexing tasks | Medium | Medium |
| 4 | Dispatcher task + chain after `update_content_metadata` | Low | Low |
| 5 | Management command for manual runs | Low | Low |
| 6 | Test against v2 index | Low | Low |
| 7 | Atomic cutover | Medium | Low |

---

### Phase 0: Prerequisite Fix

**Problem**: `ContentMetadata.modified` updates on every sync, even when content unchanged. Breaks staleness detection.

**Solution**: Compare Discovery's `json_metadata.modified` before updating:

```python
new_modified = defaults.get('_json_metadata', {}).get('modified')
old_modified = content_metadata._json_metadata.get('modified')

if new_modified and old_modified and new_modified == old_modified:
    continue  # Skip update, content unchanged
```

**Scope**: Courses only (programs/pathways don't have `modified` from Discovery; they use different staleness logic).

---

### Phase 1: Tracking Model

```python
class ContentMetadataIndexingState(TimeStampedModel):
    content_metadata = models.OneToOneField(ContentMetadata, on_delete=models.CASCADE)
    last_indexed_at = models.DateTimeField(null=True)
    removed_from_index_at = models.DateTimeField(null=True)
    algolia_object_ids = JSONField(default=list)  # Tracks shards
    last_failure_at = models.DateTimeField(null=True)
    failure_reason = models.TextField(null=True)
```

---

### Phase 2: Algolia Client Methods

```python
def save_objects_batch(self, objects: list[dict]) -> None
def delete_objects_batch(self, object_ids: list[str]) -> None
def get_object_ids_by_prefix(self, prefix: str) -> list[str]  # For shard discovery
```

---

### Phase 3: Content-Type Tasks

```python
@shared_task(base=LoggedTaskWithRetry, bind=True, max_retries=1)
def index_courses_batch_in_algolia(
    self,
    content_keys: list[str],
    index_name: str = None,
    force: bool = False,
):
    for content_key in content_keys:
        content = ContentMetadata.objects.get(content_key=content_key)
        state = content.indexing_state

        # Record-level deduplication: skip if already indexed at current version
        if not force and state.last_indexed_at and state.last_indexed_at >= content.modified:
            continue

        # 1. Query Algolia for existing shards
        # 2. Generate Algolia objects (reuses existing logic)
        # 3. save_objects_batch() for upserts
        # 4. delete_objects_batch() for orphaned shards
        # 5. Update ContentMetadataIndexingState
```

Similar tasks for programs and pathways.

---

### Phase 4: Dispatcher + Integration

```python
@shared_task(base=LoggedTaskWithRetry, bind=True)
def dispatch_algolia_indexing(self, content_type: str = None, force: bool = False):
    # 1. Query stale records (modified > last_indexed_at)
    # 2. Query failed records for retry
    # 3. Batch into groups of 10
    # 4. Dispatch content-type tasks with content_keys
```

**Integration**: Chain after `update_content_metadata`:

```python
chain(
    # ... existing metadata tasks ...
    update_full_content_metadata_task.si(),
    # NEW: Force reindex after metadata is consistent
    dispatch_algolia_indexing.si(force=True),
    dispatch_algolia_indexing.si(content_type='program', force=True),
    dispatch_algolia_indexing.si(content_type='learnerpathway', force=True),
)
```

**Cron**: Also run `dispatch_algolia_indexing` hourly to catch stragglers and retry failures.

---

### Phase 5: Management Command

```bash
./manage.py incremental_reindex_algolia \
    [--content-type course|program|learnerpathway] \
    [--index-name <name>] \
    [--force-all] \
    [--dry-run]
```

---

### Phase 6: Test Against v2 Index

1. Create `enterprise_catalog_v2` index in Algolia
2. Copy settings from v1
3. Run `./manage.py incremental_reindex_algolia --index-name v2 --force-all`
4. Compare record counts and spot-check content

---

### Phase 7: Cutover

1. Validate v2 parity with v1
2. Atomic swap via Algolia `move_index` API
3. Enable hourly cron
4. Monitor; deprecate old code after stability period

**Rollback**: Swap back to old index; `reindex_algolia` command still works.

---

## File Changes

| File | Changes |
|------|---------|
| `apps/catalog/models.py` | Add `ContentMetadataIndexingState`; fix `_update_existing_content_metadata` |
| `apps/api_client/algolia.py` | Add batch methods |
| `apps/api/tasks.py` | Add 3 content-type tasks + dispatcher |
| `apps/catalog/management/commands/` | Add `incremental_reindex_algolia.py` |

---

## Success Criteria

- [ ] v2 index matches v1 in record count and content
- [ ] Full reindex parallelizes across workers
- [ ] Failed batches retry independently
- [ ] Hourly cron keeps index fresh automatically

---

## Meta: How We Arrived at This Plan

This section summarizes the key decision points from the planning discussion.

### Staleness Detection
- **Initial idea**: Use `ContentMetadata.modified` to detect changes
- **Problem discovered**: `modified` updates on every sync even when content unchanged
- **Insight**: Discovery's `json_metadata.modified` field can detect actual content changes
- **Validation**: SQL queries confirmed courses have this field (programs/pathways don't)
- **Result**: Phase 0 prerequisite fix — only update `modified` when Discovery content actually changed

### Cascade vs Independent Triggers
- **Question**: Should course changes cascade to program/pathway reindexing?
- **Decision**: No cascading. Separate tasks with independent triggers. Programs/pathways reindex when `MAX(child.last_indexed_at) > last_indexed_at`

### Cron vs Event-Driven
- **Preference**: Batch/cron-based to simplify mental model
- **Problem raised**: What if cron runs mid-way through `update_content_metadata`? Courses could be indexed with partial catalog membership
- **Decision**: Force-reindex after `update_content_metadata` completes, consistent with current cron behavior

### Task Deduplication
- **Initial approach**: Use `expiring_task_semaphore` with `(content_keys, modified_timestamps)` in task args
- **Problem caught**: Task args are *lists* (batches), so different batches with overlapping records wouldn't dedupe
- **Decision**: Record-level deduplication inside the task (`last_indexed_at >= content.modified`)

### Current State Clarification
- **Correction**: Current state is cron-based with implicit chaining (separate cron schedules), not manual triggers

### Document Evolution
1. **v1** (566 lines): Comprehensive but verbose, design decisions buried at bottom
2. **v2** (~290 lines): Condensed, design decisions at top, ASCII diagrams, removed redundant tables
