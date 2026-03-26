# Algolia Re-indexing Architecture: Technical Pitch

## The Problem

Our Algolia reindexing runs as a **single, monolithic operation** that takes **2+ hours** in production and is growing longer as our catalog expands. This creates three critical issues:

| Issue | Impact |
|-------|--------|
| **No partial updates** | A single course metadata or catalog change rebuilds the entire index |
| **Not parallelizable** | One Celery worker, one thread, one long-running API call |
| **Failure = full restart** | If it fails at hour 1:45, we start over from scratch |

The `replace_all_objects()` approach worked years ago at smaller scale but doesn't fit our current data volume or operational needs.

---

## Ask

Approve development of incremental Algolia indexing to replace the current monolithic approach. This unblocks catalog scalability and reduces operational risk from long-running, non-resumable jobs.

---

## Future State

| Capability | Benefit |
|------------|---------|
| **Per-record indexing** | Update only what changed |
| **Horizontally scalable** | Fan out across Celery workers in batches of 10 |
| **Incremental by default** | Skip records unchanged since last index |
| **Failure-resilient** | Failed batches retry independently; progress preserved |
| **Explicit chaining** | Force-reindex after `update_content_metadata` ensures consistent catalog membership |

**Result**: Full reindex parallelizes across workers; typical changes index in minutes, not hours.

---

## Key Design Decisions

1. **Force-reindex after `update_content_metadata`**: Catalog membership builds incrementally as each CatalogQuery is processed. Indexing after completion ensures consistent membership state in Algolia.

2. **Record-level deduplication**: Tasks check `last_indexed_at >= content.modified` before indexing each record, preventing duplicate work when cron and force-reindex overlap.

3. **Staleness via Discovery's `modified` field**: Courses use `json_metadata.modified` from course-discovery to detect actual content changes. Programs/pathways detect staleness when their child content has been indexed more recently.

---

## Development Scope

| Component | Complexity | Notes |
|-----------|------------|-------|
| Prerequisite fix | Low | Skip metadata updates when Discovery content unchanged ✅ |
| New tracking model | Low | `ContentMetadataIndexingState` for staleness + failure tracking |
| Algolia client methods | Low | Thin wrappers around existing SDK |
| Content-type Celery tasks | Medium | Reuses existing object generation logic |
| Dispatcher + explicit chaining | Low | Chain after `update_content_metadata`; hourly cron for stragglers |
| Testing + cutover | Low | Validate against v2 index; zero-downtime swap |

**Total scope**: Medium — mostly orchestration around existing business logic. Object schema, sharding, and UUID aggregation remain unchanged.

See `tech-spec.md` for detailed implementation phases.

---

## Migration Path

```
Build
  - Prerequisite fix: reliable staleness detection for courses
  - New tracking model, tasks, dispatcher, management command
  - Target new v2 index; existing index untouched
                    |
                    v
Validate
  - Run incremental indexing against v2 index
  - Compare record counts and content with production
  - Test search behavior in staging
                    |
                    v
Cutover
  - Zero-downtime transition via dual-index support
  - Chain force-reindex after update_content_metadata
  - Enable hourly cron for stragglers/retries
  - Gradually migrate frontends, then deprecate old code path
```

**Risk mitigation**: Old system remains fully operational until cutover is validated. Rollback = revert settings.

---

## Key Metrics (Before / After)

| Metric | Current | Target |
|--------|---------|--------|
| Trigger | Cron (implicit chain via separate schedules) | Cron (explicit chain after metadata sync) |
| Full reindex duration | 2+ hours (sequential) | Parallelized across workers |
| Failure recovery | Restart from zero | Retry failed batch only |
| Granularity | All records every time | Only changed records |
