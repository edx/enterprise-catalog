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

**Two triggers exist today for Algolia updates:**

1. **Daily cron chain** (~3+ hours total): Syncs all content from Discovery, then rebuilds entire Algolia index
2. **API-triggered refresh** (on-demand): Called by edx-enterprise when a catalog/query is created or updated via LMS Django Admin

Problems: 2+ hour duration, not parallelizable, failure = full restart, rebuilds entire index even for small changes.

```
TRIGGER 1: Daily Cron Chain
┌─────────────────────────┐      ┌─────────────────────────┐      ┌─────────────┐
│ update_content_metadata │      │ reindex_algolia         │      │   Algolia   │
│ (cron, ~1hr)            │      │ (cron, scheduled after  │─────▶│   Index     │
│                         │      │  UCM completes, 2+ hrs) │      │             │
│ Syncs ALL content from  │      │ replace_all_objects()   │      │ Full        │
│ Discovery; rebuilds all │      │ Single-threaded         │      │ replacement │
│ catalog memberships     │      │ All-or-nothing          │      └─────────────┘
└─────────────────────────┘      └─────────────────────────┘

TRIGGER 2: API Refresh (EnterpriseCatalogRefreshDataFromDiscovery)
┌─────────────────────────┐      ┌─────────────────────────┐      ┌─────────────┐
│ update_catalog_metadata │─────▶│ update_full_content_    │─────▶│   Algolia   │
│ (single catalog query)  │      │ metadata                │      │   Index     │
│                         │      │                         │      │             │
│ Triggered by POST from  │      │ index_enterprise_       │      │ Full        │
│ edx-enterprise when     │      │ catalog_in_algolia      │      │ replacement │
│ catalog/query changes   │      │ (still replace_all)     │      └─────────────┘
└─────────────────────────┘      └─────────────────────────┘
```

### Future State

**Same two triggers, but incremental and parallelized:**

We'll make an asynchronous task that operates on small batches of courses the main "unit of work" - we can
fan this out over multiple celery workers to parallelize it. We'll trigger that task as follows:

1. **Daily cron chain**: Syncs content, then dispatches incremental indexing for changed records
2. **API-triggered refresh**: Updates single catalog's content, then indexes only that catalog's content
3. **Stragglers cron** (new): Catches any missed records or retries failures

Benefits: Parallelized, failure-resilient, only indexes changed records.

```
TRIGGER 1: Daily Cron Chain
┌─────────────────────────┐      ┌─────────────────────────┐      ┌─────────────┐
│ update_content_metadata │─────▶│ dispatch_algolia_       │─────▶│   Algolia   │
│ (cron, ~1hr)            │      │ indexing (force=True)   │      │   Index     │
│                         │      │                         │      │             │
│ Syncs ALL content;      │      │ Batches of 10 records   │      │ Incremental │
│ chains incremental      │      │ Parallel across workers │      │ upserts     │
│ indexing on completion  │      │ All records (force)     │      └─────────────┘
└─────────────────────────┘      └─────────────────────────┘

TRIGGER 2: API Refresh (EnterpriseCatalogRefreshDataFromDiscovery)
┌─────────────────────────┐      ┌─────────────────────────┐      ┌─────────────┐
│ update_catalog_metadata │─────▶│ dispatch_algolia_       │─────▶│   Algolia   │
│ (single catalog query)  │      │ indexing_for_catalog_   │      │   Index     │
│                         │      │ query(catalog_query_id) │      │             │
│ Triggered by POST from  │      │                         │      │ Incremental │
│ edx-enterprise          │      │ Only content in this    │      │ upserts     │
│                         │      │ catalog's membership    │      └─────────────┘
└─────────────────────────┘      └─────────────────────────┘

TRIGGER 3: Stragglers Cron (every 30 min)
┌─────────────────────────┐      ┌─────────────────────────┐
│ dispatch_algolia_       │─────▶│ Batches stale/failed    │
│ indexing (force=False)  │      │ records only            │
│                         │      │                         │
│ Catches records missed  │      │ Record-level dedup      │
│ by other triggers;      │      │ prevents double-work    │
│ retries past failures   │      │                         │
└─────────────────────────┘      └─────────────────────────┘
```

### Scheduled Tasks Summary

| Task | Schedule | Purpose | Trigger |
|------|----------|---------|---------|
| **Daily metadata sync chain** | Once daily | Sync all content from Discovery, rebuild all catalog memberships, then index all changed content | Cron |
| **Stragglers/retry dispatcher** | Every 30 min | Index any stale records missed by other triggers; retry past failures | Cron |
| **API refresh** | On-demand | Index content for a specific catalog after it's created/updated | POST to `EnterpriseCatalogRefreshDataFromDiscovery` |

**Key distinction**: The daily sync chain runs `update_content_metadata` which rebuilds *all* catalog memberships from scratch by re-evaluating every CatalogQuery against Discovery. The stragglers cron does *not* sync metadata — it only dispatches indexing for records that are already stale in our database.

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

**Owner**: Alex Dusenbery (tech lead/architect)
**Implementer(s)**: Titans

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

---

## Architecture

### Central Thesis

We're replacing a single 2+ hour monolithic `replace_all_objects()` call with a **dispatcher → parallel batch tasks** pattern:

1. A **dispatcher task** queries for content that needs indexing
2. It groups content into **batches of 10** records
3. It **fans out** one Celery task per batch — these run in parallel across workers
4. Each batch task **independently updates Algolia** and records its state
5. A new **`ContentMetadataIndexingState` model** tracks per-record indexing state, enabling staleness detection and failure retry

Content types are processed **sequentially** (courses → programs → pathways) because programs inherit catalog membership from courses, and pathways from programs. But **within each content type, batches execute in parallel**.

```
                                    ┌─────────────────┐
                                    │ Batch 1 (10)    │──▶ Algolia
                                    ├─────────────────┤
┌──────────────┐    ┌───────────┐   │ Batch 2 (10)    │──▶ Algolia
│  Dispatcher  │───▶│  Courses  │──▶├─────────────────┤
│              │    │  (query)  │   │ Batch 3 (10)    │──▶ Algolia
│ Query stale  │    └───────────┘   ├─────────────────┤
│ content,     │                    │ ...             │
│ batch by 10, │    ┌───────────┐   └─────────────────┘
│ fan out      │───▶│ Programs  │──▶ (same pattern, after courses complete)
│              │    └───────────┘
│              │    ┌───────────┐
│              │───▶│ Pathways  │──▶ (same pattern, after programs complete)
└──────────────┘    └───────────┘
```

### How It Works: End-to-End Flow

**Step 1: Trigger**

One of three triggers initiates indexing:
- **Daily cron chain**: After `update_content_metadata` syncs all content from Discovery
- **API refresh**: After `EnterpriseCatalogRefreshDataFromDiscovery` updates a single catalog
- **Stragglers cron**: Every 30 minutes, catches missed records and retries failures

**Step 2: Dispatcher queries for stale content**

The dispatcher task queries `ContentMetadataIndexingState` to find records needing indexing:
- Records where `modified > last_indexed_at` (content changed)
- Records where `last_indexed_at IS NULL` (never indexed)
- Records where `last_failure_at IS NOT NULL` (retry failures)

For the daily cron, `force=True` skips staleness checks and indexes all content (to catch membership changes that don't update `modified`).

**Step 3: Batch and fan out**

The dispatcher groups content keys into batches of 10 and dispatches a Celery task for each batch. Tasks are dispatched in dependency order:
1. All course batches (parallel)
2. Wait for courses to complete
3. All program batches (parallel)
4. Wait for programs to complete
5. All pathway batches (parallel)

**Step 4: Batch task indexes records**

Each batch task (`index_courses_batch_in_algolia`, etc.):
1. For each content_key in the batch:
   - Fetch `ContentMetadata` and its `ContentMetadataIndexingState`
   - Skip if already indexed at current version (deduplication)
   - Generate Algolia objects (reuses existing `_get_algolia_products_for_batch`)
   - Call `save_objects_batch()` to upsert records
   - Call `delete_objects_batch()` to remove orphaned shards
   - Update `ContentMetadataIndexingState.last_indexed_at`
2. On per-record failure: call `mark_as_failed()`, continue to next record
3. Return results dict with indexed/skipped/failed counts

**Step 5: State is persisted**

`ContentMetadataIndexingState` now reflects the new state. The next dispatcher run will only pick up records that are stale or failed.

### Mental Model

Think of the enterprise-catalog database tables as **invariants**:
- The `CatalogQuery ← M2M → ContentMetadata` relationship describes catalog membership
- The `ContentMetadata` records describe content state, including `modified` timestamp

Our goal is to create Algolia records that reflect this state, with **facets** describing which catalogs/queries/customers include each content record. The incremental system keeps Algolia in sync by tracking what's changed since last index.

### Staleness Detection

We determine what needs reindexing differently per content type:

| Content Type | Stale When |
|--------------|------------|
| Courses | `modified > last_indexed_at` |
| Programs | `modified > last_indexed_at` OR any child course indexed more recently |
| Pathways | `modified > last_indexed_at` OR any child program indexed more recently |

**Why programs/pathways depend on children**: Discovery doesn't provide a `modified` field for these types. When a course's catalog membership changes, programs containing that course need reindexing to update their facets — even though the program itself didn't change.

### Key Components

| Component | Purpose |
|-----------|---------|
| `ContentMetadataIndexingState` model | Tracks per-record indexing state, failure history |
| `AlgoliaSearchClient` batch methods | `save_objects_batch()`, `delete_objects_batch()`, `get_object_ids_by_prefix()`, `get_content_keys_for_catalog_query()` |
| Content-type batch tasks | `index_courses_batch_in_algolia`, `index_programs_batch_in_algolia`, `index_pathways_batch_in_algolia` |
| Dispatcher tasks | `dispatch_algolia_indexing` (all stale), `dispatch_algolia_indexing_for_catalog_query` (single catalog) |
| Management command | `incremental_reindex_algolia` — manual runs, testing against v2 index |
| Feature flag | `ENABLE_INCREMENTAL_ALGOLIA_INDEXING` — controls activation |

### ContentMetadataIndexingState Model

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

### Integration Points

**Daily cron chain** (after `update_content_metadata`):
```python
chain(
    # ... existing metadata tasks ...
    update_full_content_metadata_task.si(),
    # NEW: Force reindex after metadata is consistent
    dispatch_algolia_indexing.si(force=True),
)
```

**API refresh endpoint** (`EnterpriseCatalogRefreshDataFromDiscovery`):
```python
chain(
    update_catalog_metadata_task.si(catalog_query_id),
    update_full_content_metadata_task.si(),
    # NEW: Dispatcher queries Algolia to detect removals internally
    dispatch_algolia_indexing_for_catalog_query.si(catalog_query_id),
)
```

**Feature Flag**: `ENABLE_INCREMENTAL_ALGOLIA_INDEXING` controls whether these chains use the new dispatcher. This allows us to build and deploy all phases before activating.

---

## No-gos

The following are explicitly out of scope for this project:

1. **Schema changes**: Algolia object schema, sharding strategy, and UUID aggregation remain unchanged
2. **Real-time event-driven indexing**: We are not implementing webhooks or signals that index immediately on content change
3. **Replica index management**: The replica index will continue to be managed by Algolia's replica sync
4. **Search API changes**: No changes to how consumers query Algolia
5. **Historical backfill of indexing state**: We will not attempt to retroactively populate `last_indexed_at` for existing records. `ContentMetadataIndexingState` records are created lazily on first index; missing records are treated as "never indexed" and will be indexed on the first run.

---

## Design Decisions

This section explains the **"why"** behind key architectural choices. For the **"what"** and **"how"**, see the Architecture section above.

### 1. Why Force-Reindex After `update_content_metadata`?

**The consistency problem**: `update_content_metadata` processes catalog queries sequentially. A course in 50 catalogs has its membership built incrementally:

```
Query 1  → Course X in [Catalog A, B]
Query 25 → Course X in [Catalog A, B, C, D, E]
Query 50 → Course X in [Catalog A, B, C, D, E, F, G]  ← final
```

If we indexed immediately on each touch, Algolia would have incomplete membership until Query 50 finishes. By force-reindexing after completion, we ensure consistent catalog membership.

### 2. Why Cron-Based Instead of Fully Event-Driven?

| Concern | Fully Event-Driven | Cron + Force-Reindex |
|---------|--------------|----------------------|
| Consistency | Indexes partial state mid-sync | Indexes after sync completes |
| Deduplication | Course touched 50x = 50 tasks | Batch after completion |
| Failure handling | Needs explicit retry queue | Cron naturally retries |

This maintains the current cron-based model while adding consistency guarantees through explicit chaining.

**What would true event-driven indexing require?**

The core challenge is that catalog membership is *computed*, not stored as discrete events (see consistency problem in Design Decision #1). To eliminate crons entirely, we would need:

1. **Membership change tracking**: Store membership as explicit M2M relationships (not just query results) and emit events when relationships change. This would require schema changes to track `(content_key, catalog_uuid)` pairs with timestamps.
2. **Atomic membership updates**: Change `update_content_metadata` to compute final membership for each content item before writing, rather than building incrementally across queries.

**Why we're not doing this now**: The incremental batch approach gives us 90% of the benefits (parallelism, failure resilience, faster updates) with 10% of the complexity. True event-driven indexing is a larger architectural change that can be pursued later if needed.

**The API refresh endpoint (`EnterpriseCatalogRefreshDataFromDiscovery`) is already event-driven**: It triggers when a catalog/query is created or updated in the LMS. Our incremental indexing naturally supports this use case — we just need to scope the indexing to that catalog's content.

### 3. What Does `force=True` Mean?

The `force` parameter controls whether staleness checks are bypassed:

| `force` value | Behavior |
|---------------|----------|
| `force=False` | Only index records where `modified > last_indexed_at` (stale records) |
| `force=True` | Index all records regardless of staleness |

**Why the daily cron uses `force=True`**: Staleness detection (`modified > last_indexed_at`) catches *content* changes but not *membership* changes. When a CatalogQuery is updated, content may be added or removed from catalogs without its `modified` timestamp changing. By using `force=True` after the daily metadata sync, we ensure membership changes are reflected in Algolia.

**Why the stragglers cron uses `force=False`**: It only needs to catch records that were missed or failed — not re-process everything.

### 4. How Do We Prevent Double-Indexing?

**Record-level deduplication in batch tasks**

When `force=False`, each batch task checks if a record needs indexing before doing work:

```python
if not force and state.last_indexed_at and state.last_indexed_at >= content.modified:
    continue  # Already indexed this version, skip
```

This prevents the stragglers cron from re-indexing records that were just indexed by the daily cron.
We should make careful use of `transaction.atomic()` and/or `transaction.on_commit()` to ensure
the database atomicity/isolation behaviors we want.

**Why not use `expiring_task_semaphore` on the dispatcher?**

The existing `expiring_task_semaphore` decorator dedupes by `(task_name, args, kwargs)` and bypasses the check when `force=True`. This means:
- Cron (`force=False`) and force-reindex (`force=True`) have different semaphore keys
- `force=True` bypasses the semaphore check entirely

We could extend the semaphore to support a custom key, but it's simpler to accept that concurrent dispatcher runs are harmless — the record-level deduplication ensures no wasted Algolia writes.

### 5. Why Separate Tasks Per Content Type?

Programs depend on courses; pathways depend on programs. Sequential execution (courses → programs → pathways) ensures correct UUID inheritance without complex cascade logic.

### 6. Why Batch Size of 10?

* Balances parallelism vs overhead. Smaller batches = finer failure granularity, more tasks.
* Larger batches = fewer tasks, coarser recovery.
* Configurable via `ALGOLIA_INDEXING_BATCH_SIZE` setting.

### 7. Why a New `search` App?

* Better isolation and code organization.
* Separates Algolia indexing concerns from core catalog models and API endpoints.
* Establishes a clear sub-domain boundary.

### 8. How Do We Handle Membership Removals?

When content is *removed* from a catalog's membership, that content's Algolia record still has the old catalog in its facets. We need to reindex it to remove the stale membership.

**The problem**: Staleness detection (`modified > last_indexed_at`) catches content *changes*, not membership *changes*. If a CatalogQuery is updated to exclude a course, the course's `modified` timestamp doesn't change — but its membership did.

**Solution by trigger type**:

| Trigger | How Removals Are Handled |
|---------|--------------------------|
| **Daily cron chain** | Uses `force=True` which reindexes all content regardless of staleness. Membership is rebuilt from scratch, so removals are naturally reflected. |
| **API refresh** | Must explicitly track and reindex removed content (see below). |
| **Stragglers cron** | Only catches stale/failed records; doesn't help with removals. Relies on daily cron or API refresh to have already flagged them. |

**For API refresh (`EnterpriseCatalogRefreshDataFromDiscovery`)**, the dispatcher handles removal detection by querying Algolia:

1. **Query Algolia**: Find all content_keys currently indexed with this catalog query's facets
2. **Query database**: Get current membership from `catalog_query.content_metadata`
3. **Compute removed**: `algolia_keys - db_keys` = content removed from membership
4. **Dispatch indexing**: Include both current members AND removed content in batches

```python
# Pseudocode — this logic lives in the dispatcher, not the API endpoint
def dispatch_algolia_indexing_for_catalog_query(catalog_query_id):
    catalog_query = CatalogQuery.objects.get(id=catalog_query_id)

    # Query Algolia for records with this catalog_query's facets
    algolia_content_keys = algolia_client.get_content_keys_for_catalog_query(catalog_query_id)

    # Query database for current membership
    db_content_keys = set(catalog_query.content_metadata.values_list('content_key', flat=True))

    # Content removed from membership needs reindexing to update facets
    removed_keys = algolia_content_keys - db_content_keys
    all_keys_to_index = db_content_keys | removed_keys

    # Batch and dispatch...
```

**Why this works**: When we reindex a removed course, we generate fresh Algolia objects based on its *current* membership (which no longer includes the old catalog). The Algolia `save_objects_batch()` replaces the record, removing the stale catalog facet.

**Why query Algolia instead of tracking pre-update state**: This keeps the `update_content_metadata` flow unchanged and uses Algolia as the source of truth for "what's currently indexed." The dispatcher already needs Algolia client access, so this is a natural fit.

### 9. Why Use a Frontend Feature Flag for Cutover?

Instead of updating each frontend's `ALGOLIA_INDEX_NAME` env var and deploying separately, we use a feature flag that controls which index frontends read from.

| Approach | Cutover Speed | Rollback Speed | Coordination Required |
|----------|---------------|----------------|----------------------|
| Env var per frontend | Hours (3 deploys) | Hours (3 deploys) | High — must track each frontend |
| Frontend feature flag | Instant (flag flip) | Instant (flag flip) | None — single change |

**Benefits**:
- **Instant rollback**: If issues arise after cutover, disable the flag — no deploys needed
- **Gradual rollout**: Can enable for specific users/enterprises first to validate
- **No frontend deploy coordination**: All frontends switch simultaneously with one flag change
- **Reduced risk**: The actual cutover moment is decoupled from code deployment

**Trade-off**: Requires deploying feature-flag-aware code to all frontends before cutover. But this deployment carries no risk since the flag defaults to the current index.

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
│   8b: Deploy frontend flag  Frontends read flag to choose index (default v1)│
│          │                                                                  │
│          ▼                                                                  │
│   8c: Parallel writes       Legacy writes v1; incremental writes v2         │
│          │                  (both indices stay fresh)                       │
│          ▼                                                                  │
│   8d: Flip frontend flag    Instant switch to v2 — no deploys needed        │
│          │                  (rollback = disable flag; v1 still fresh)       │
│          ▼                                                                  │
│   8e: Disable legacy        Stop v1 writes; v1 becomes stale                │
│          │                                                                  │
│          ▼                                                                  │
│   8f: Cleanup               Remove v1 from allowed list; deprecate legacy   │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Why No Downtime**: The secured API key's `restrictIndices` parameter controls which indices frontends can access. By allowing both v1 and v2 during transition (Phase 8a), frontends continue working regardless of which index they're reading.

**Why No Staleness**: During Phase 8c, writes switch to v2 while frontends still read v1. This creates brief staleness on v1 (new content appears on v2 first), but v1 already had hours of staleness from the legacy 2+ hour reindex cycle. Once the frontend flag is flipped (Phase 8d), all frontends get the freshest data instantly.

**Rollback at Any Point**: Rollback is instant at any phase. Before 8d, nothing has changed for users. At 8d, disable the feature flag to revert all frontends to v1 without any deploys.

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

**State record initialization**: `ContentMetadataIndexingState` records are created lazily via `get_or_create_for_content()` when a content record is first indexed. On the first full reindex (Phase 7), state records will be created for all existing `ContentMetadata`. No data migration is needed — missing state records are treated as "never indexed" (always stale).

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
- Add `get_content_keys_for_catalog_query(catalog_query_id, index_name=None)` method for membership removal detection
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
- [ ] `get_content_keys_for_catalog_query()` returns content keys currently indexed with the given catalog query's facets (uses Algolia's browse/search API with facet filter)
- [ ] All methods support optional `index_name` param for targeting v2 index
- [ ] All methods have appropriate error handling and logging

**Verification**:
- Unit tests with mocked Algolia client
- Integration test (manual): Save and retrieve objects from a test index
- Integration test (manual): Delete objects, verify removal
- Integration test (manual): Query by catalog query facet, verify correct content keys returned

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
  - Update `ContentMetadataIndexingState` (be intentional and descriptive about use of `transaction.atomic()`.
  - Handle failures per-record with `mark_as_failed()`
- Add a Redis-cached `IndexingMappings` layer wrapping the legacy
  `_precalculate_content_mappings` helper, plus the indexable-content-key set
  from `partition_*_keys_for_indexing`. The cache amortizes the
  O(catalog-size) precompute across tasks in a dispatcher fan-out: first
  task warms it, the rest reuse it. Default TTL 30 minutes via
  `ALGOLIA_INDEXING_MAPPINGS_CACHE_TIMEOUT`. Expose
  `invalidate_indexing_mappings_cache()` for the Phase 4 dispatcher to call
  after `update_content_metadata`.
- Tasks fire-and-forget the Algolia writes: the `IndexingResponse` from
  `save_objects` / `delete_objects` is discarded and `.wait()` is not
  called. See ADR 0012 in this repo.

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

### Phase 4: Dispatcher Tasks

**Summary**: Create two dispatcher tasks

* one that queries for stale/failed records and dispatches batch indexing tasks. We'll run this one on a schedule.
* one that queries for all content that's a member of a given catalog/catalog query. This will be run as the last
  task in POSTs to `EnterpriseCatalogRefreshDataFromDiscovery` when incremental reindexing is enabled.

**Scope**:
- Create `dispatch_algolia_indexing` task
- Implement `_get_stale_content_keys(content_type, force, include_failed)` helper
- Create `dispatch_algolia_indexing_for_catalog_query` task
- Implement batching logic (default batch size 10, configurable)
- Dispatch tasks in dependency order: courses → programs → pathways
- Support `dry_run` mode for testing
- Add `ALGOLIA_INDEXING_BATCH_SIZE` setting
- Pre-warm `IndexingMappings` before fan-out: call
  `invalidate_indexing_mappings_cache()` (after `update_content_metadata`
  has settled), then `get_indexing_mappings()` synchronously so the cache
  is warm before workers pick up batch tasks. Without this, N concurrent
  workers would each compute the mappings on cache miss (a thundering
  herd). Correctness is unaffected, but the dispatcher is the natural
  serialization point.

**File Changes**:
| File | Change |
|------|--------|
| `enterprise_catalog/apps/search/tasks.py` | Add dispatcher tasks |
| `enterprise_catalog/apps/search/tests/test_tasks.py` | Add tests |
| `enterprise_catalog/settings/base.py` | Add `ALGOLIA_INDEXING_BATCH_SIZE` |

**Acceptance Criteria**:
The primary/scheduled task:
- [ ] Dispatcher queries stale courses (never indexed OR modified > last_indexed_at)
- [ ] Dispatcher queries stale programs/pathways (per Staleness Detection: child content indexed more recently)
- [ ] Dispatcher queries failed records for retry
- [ ] Dispatcher batches content keys into groups of 10 (configurable)
- [ ] Dispatcher dispatches tasks in correct order (courses first)
- [ ] `dry_run=True` logs what would be dispatched without dispatching
- [ ] Dispatcher returns summary of dispatched tasks

The catalog-query-specific dispatcher task must handle membership removals (see Design Decision #8):
- [ ] Task accepts `catalog_query_id` parameter
- [ ] Dispatcher queries Algolia for content currently indexed with this catalog query's facets
- [ ] Dispatcher queries database for current membership
- [ ] Dispatcher computes removed content: `algolia_keys - db_keys`
- [ ] Dispatcher indexes both current members AND removed content (to update facets)
- [ ] Dispatcher batches content keys into groups of 10 (configurable)
- [ ] Dispatcher dispatches tasks in correct order (courses first)
- [ ] Dispatcher returns summary of dispatched tasks

**Verification**:
- Unit tests for stale content key queries
- Unit tests for catalog query membership
- Unit tests for batching logic
- Unit tests for ``dry_run`` mode
- Manual: Run dispatcher with `dry_run=True`, verify logged batches
- Manual: Run dispatcher against test index, verify tasks execute

---

### Phase 5: Management Command

**Summary**: Create management command for manual/scheduled incremental reindexing runs.

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

**Summary**: Add feature flag and integrate dispatchers into both the daily cron chain and the API refresh endpoint.

**Scope**:
- Add `ENABLE_INCREMENTAL_ALGOLIA_INDEXING` feature flag (default: False)
- Modify `update_content_metadata` task chain to conditionally include dispatcher
- Modify `EnterpriseCatalogRefreshDataFromDiscovery` to use new dispatcher
- Document flag in settings

**File Changes**:
| File | Change |
|------|--------|
| `enterprise_catalog/apps/api/tasks.py` | Add conditional chaining for daily cron |
| `enterprise_catalog/apps/api/v1/views/enterprise_catalog_refresh_data_from_discovery.py` | Integrate `dispatch_algolia_indexing_for_catalog_query` |
| `enterprise_catalog/settings/base.py` | Add feature flag |

**Acceptance Criteria**:

*Daily cron chain:*
- [ ] Feature flag `ENABLE_INCREMENTAL_ALGOLIA_INDEXING` exists (default False)
- [ ] When flag is False, existing behavior unchanged
- [ ] When flag is True, `dispatch_algolia_indexing(force=True)` chains after metadata sync

*API refresh endpoint:*
- [ ] When flag is False, existing `index_enterprise_catalog_in_algolia_task` behavior unchanged
- [ ] When flag is True, `dispatch_algolia_indexing_for_catalog_query` replaces old indexing task
- [ ] Dispatcher handles removal detection internally (queries Algolia, no endpoint changes needed)

**Verification**:
- Unit tests for conditional chaining logic (daily cron)
- Unit tests for dispatcher integration (API refresh)
- Manual: With flag False, verify old reindex behavior for both triggers
- Manual: With flag True, verify dispatcher runs correctly for both triggers
- Manual: Verify removed content gets reindexed (loses catalog facet)

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

**Summary**: Multi-step cutover using a frontend feature flag to control which index is read, enabling instant rollback without frontend deploys.

**Background**: Multiple frontends read from Algolia using index names. The secured API key's `restrictIndices` controls which indices each frontend can access. By using a feature flag to control the index name (rather than env vars), we can switch all frontends instantly without deploying each one.

#### Phase 8a: Enable Dual-Index Support in Backend

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

#### Phase 8b: Add Frontend Feature Flag Support

**Scope**:
- Add Waffle flag `enterprise.use_algolia_index_v2` in edx-enterprise
- Expose flag via the `enterprise_features()` API (see `edx-enterprise/enterprise/toggles.py`)
- Frontends read this flag from the enterprise API to determine which index name to use
- Flag defaults to False (use v1)
- Deploy frontend changes that respect the flag

**How it works**: Enterprise frontends already fetch feature flags from the edx-enterprise API via the `enterprise_features()` endpoint. We add a new Waffle flag that gets included in this response. Frontends check this flag and use the appropriate Algolia index name.

**File Changes**:
| Repo | Change |
|------|--------|
| edx-enterprise | Add `USE_ALGOLIA_INDEX_V2` WaffleFlag in `enterprise/toggles.py`; include in `enterprise_features()` |
| frontend-app-learner-portal-enterprise | Read `use_algolia_index_v2` from enterprise features; use v2 index name when True |
| frontend-app-admin-portal | Read `use_algolia_index_v2` from enterprise features; use v2 index name when True |
| frontend-app-enterprise-public-catalog | Read `use_algolia_index_v2` from enterprise features; use v2 index name when True |

**Acceptance Criteria**:
- [ ] Waffle flag `enterprise.use_algolia_index_v2` exists in edx-enterprise (default False)
- [ ] Flag is included in `enterprise_features()` API response
- [ ] All frontends deployed with flag-reading logic
- [ ] With flag False, frontends continue using v1
- [ ] Flag can be enabled per-enterprise or globally via Django admin

#### Phase 8c: Enable Parallel Writes (v1 + v2)

**Scope**:
- Keep legacy `reindex_algolia` cron running (writes to v1)
- Enable `ENABLE_INCREMENTAL_ALGOLIA_INDEXING` flag with `INDEX_NAME` set to v2
- Configure stragglers cron for `dispatch_algolia_indexing` (writes to v2)
- Both indices are now being updated in parallel
- Deploy enterprise-catalog

**Why parallel writes**: Running both indexing systems simultaneously ensures:
- v1 stays fresh (frontends still reading it)
- v2 is continuously validated against v1
- Rollback requires no data recovery — v1 is always current

**Acceptance Criteria**:
- [ ] Legacy `reindex_algolia` cron still running (v1 stays fresh)
- [ ] Incremental indexing writes to v2
- [ ] Stragglers cron configured and running
- [ ] Both indices have matching content (validate with comparison script)
- [ ] Frontends still work (reading v1 via feature flag)

#### Phase 8d: Flip Frontend Feature Flag

**Scope**:
- Enable `use_algolia_index_v2` feature flag for all users
- Monitor for issues
- No frontend deploys required — flag change takes effect immediately

**Acceptance Criteria**:
- [ ] Feature flag enabled globally
- [ ] Search functionality verified on each frontend
- [ ] No increase in error rates

**Rollback**: Disable feature flag — instant rollback to v1 without any deploys.

#### Phase 8e: Disable Legacy v1 Writes

**Scope**:
- Disable old `reindex_algolia` cron job (v1 stops being updated)
- v1 index remains readable but becomes stale
- Monitor for any issues

**Acceptance Criteria**:
- [ ] Old `reindex_algolia` cron disabled
- [ ] v2 continues to be updated normally
- [ ] Monitoring confirms stable operation for 1+ week

#### Phase 8f: Final Cleanup

**Scope**:
- Remove v1 from `ALLOWED_INDEX_NAMES`
- Remove frontend feature flag and hardcode v2 index name (optional, can leave flag indefinitely)
- Deprecate old `reindex_algolia` command and `replace_all_objects` code path
- (Optional) Delete v1 index from Algolia after stability period

**Acceptance Criteria**:
- [ ] v1 removed from `ALLOWED_INDEX_NAMES`
- [ ] Legacy code deprecated or removed
- [ ] Documentation updated

---

**Verification** (across all Phase 8 sub-phases):
- Monitor Datadog for indexing task success/failure rates
- Monitor Algolia dashboard for index health
- Manual QA: Verify search results on each frontend after flag flip

**Rollback Plan** (at any sub-phase):

| Current Phase | Rollback Steps |
|---------------|----------------|
| 8a | Revert `generate_secured_api_key()` change; remove `ALLOWED_INDEX_NAMES` |
| 8b | No rollback needed — flag defaults to False |
| 8c | Disable incremental indexing flag; legacy v1 writes continue |
| 8d | Disable `use_algolia_index_v2` feature flag — instant rollback, no deploys |
| 8e | Re-enable old `reindex_algolia` cron |
| 8f | Re-add v1 to `ALLOWED_INDEX_NAMES`; re-enable old cron if needed |

**Why This Is Zero-Downtime**:

| During Phase | Writes | Reads | Status |
|--------------|--------|-------|--------|
| 8a | v1 | v1 | No change, enabling future |
| 8b | v1 | v1 | Frontend flag deployed, defaults to v1 |
| 8c | v1 + v2 | v1 | Both indices fresh; parallel validation period |
| 8d | v1 + v2 | v2 | Flag flipped — instant switch; v1 still fresh for rollback |
| 8e | v2 | v2 | Legacy disabled; v1 becomes stale |
| 8f | v2 | v2 | Final state, cleanup complete |

**Key benefits**:
- **Phase 8c runs both systems in parallel**: v1 stays fresh throughout validation
- **Phase 8d instant rollback**: If issues arise, disable the flag — frontends return to fresh v1 data
- **No staleness window**: Unlike the previous plan, v1 is never stale while frontends might read it

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
| Batch failures accumulate | Low | Medium | Stragglers cron retries failures; monitoring alerts |
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
