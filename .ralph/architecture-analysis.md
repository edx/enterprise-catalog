# Enterprise Catalog: Architecture Analysis & Rearchitecture Planning

## 1. Current Architecture Overview

### Three Tightly-Coupled Domains

The enterprise-catalog service currently bundles three distinct responsibilities into one monolithic workflow:

1. **Content Replication** — Pulling content metadata from course-discovery and persisting enterprise-specific views of it in the local MySQL DB.
2. **Catalog-Content Inclusion** — Maintaining the many-to-many mapping between `CatalogQuery` records and `ContentMetadata` records. This drives `contains_content_items`, used heavily in learner credit `can-redeem` and LMS cron workflows.
3. **Search Indexing** — Transforming content metadata (enriched with catalog/customer/query facets) into Algolia objects and rebuilding the search index.

### Current Workflow

Both `update_content_metadata` (steps 1+2) and `reindex_algolia` (step 3) run on daily cron schedules. When a catalog/query record is saved, all three steps run synchronously in sequence. This creates a dependency chain where a failure in step 1 blocks steps 2 and 3.

### Key Architectural Facts

- ~33,500 `ContentMetadata` records in production (April 2024)
- ~1,439 distinct `CatalogQuery` filters in production
- ~2 `CatalogQuery` writes/day; ~2,300 `Course` updates/day upstream in discovery
- API throughput: up to 1,000 req/min, ~4 nines availability
- `contains_content_items` called at ~30 RPM by downstream services

---

## 2. Current Architecture Pain Points

### Resilience & Stability
- No explicit input/output contracts on content metadata schema. Upstream schema changes in discovery silently break replication and (transitively) catalog-content inclusion and search indexing.
- A failure in content replication blocks catalog-content inclusion; a failure there blocks Algolia reindexing. One bug can cascade to block new customer onboarding entirely.

### Data Consistency & Timeliness
- Content metadata is **not real-time**. A scheduled run must succeed for all catalog records to reflect current upstream data.
- No event-driven mechanism exists — openedx-events has no content metadata change events at this time.

### Testability & Deployment
- The three-domain workflow cannot be deployed or tested in isolation. A change to Algolia indexing logic requires deploying the full service.
- Local devstack setup is complex and requires coordinating multiple management commands.
- Production/staging testing of big workflow changes is risky because there is no safe staging path.

### Tight Coupling
- Content replication depends on course-discovery's Elasticsearch backend being available.
- The inclusion logic and the indexing logic are interleaved — they share code, tasks, and scheduling.

### Observability
- No easy way to inspect which content records are currently associated with a catalog from the DB alone.
- No canonical service-level dependency diagram.
- Task health/status visibility is limited; must use Postman or Django admin to inspect state.

---

## 3. Proposed Future State: Three Separate Applications

The spec proposes decomposing the monolith into three loosely-coupled Django applications within the same service (same DB, same deployment, but clearly separated concerns):

### Application 1: Content Replication

**Responsibilities:**
- Ingest upstream content metadata changes (from discovery REST API or, ideally, an event bus)
- Apply enterprise-specific transformations
- Persist both raw and transformed metadata

**Decision Points:**
1. **Async event consumption vs. sync polling**: The spec strongly recommends async event consumption (via an event broker). This requires upstream (discovery) to publish `content_metadata_changed` events — which do not yet exist in openedx-events. Short-term: sync polling via celery task remains necessary.
2. **Input/output schema contracts**: Using `jsonschema` to define and validate the shape of content metadata coming in and going out.
3. **Child-parent relationship inference**: How to encapsulate the logic that determines course-run → course parent relationships strictly within this app.

**Output:** A REST endpoint like `/api/v1/content-metadata/{content-key}` providing enterprise-normalized metadata, usable outside catalog context (e.g. subsidy reversals, Learner Portal course page).

### Application 2: Catalog-Content Inclusion

**Responsibilities:**
- Define and validate the schema of `CatalogQuery` filters
- Execute business logic to determine which content belongs to which catalog
- Persist catalog ↔ content relationships in the DB

**Decision Points (biggest of the entire project):**
- **Which backend executes the inclusion logic?** Three options considered:
  1. Current naive Python filtering against local DB
  2. Algolia as the logic engine (see ideas below)
  3. An internally-managed search index (e.g. Elasticsearch/OpenSearch)

**Transactional Pattern:** Must be synchronous regardless of backend choice; triggered by catalog/query record changes and by schedule. Eventual consistency is acceptable.

### Application 3: Search Indexing

**Responsibilities:**
- Transform enterprise-normalized content metadata + catalog-content inclusion state into Algolia objects
- Rebuild/update the Algolia search index

**Constraints:**
- Must *not* do any data transformation beyond what's needed to produce valid Algolia objects. Business logic (e.g. price computation) must live in the Content Replication app.

**Decision Points:**
- **Continue faceting Algolia records with (customer, catalog, query) identifiers?** If yes: must remain synchronous/batch. If no: could move toward async, per-content-record updates. Dropping facets would radically simplify this application but requires frontend changes.
- **`is_discoverable` facet proposal**: Instead of excluding non-indexable content, index everything and use a boolean facet to control discoverability. Improves observability of index state.

---

## 4. Alternative Approaches to Catalog-Content Inclusion

### Idea 1: Algolia as the Only Source of Truth (ELT Pattern)

**Concept:** Replace the ETL pipeline (Extract from discovery → Transform → Load to Algolia) with ELT:
1. Extract/replicate from discovery into enterprise-catalog (unchanged)
2. Load raw data into Algolia without membership logic
3. Transform by querying Algolia — catalog queries *become* Algolia queries

Frontends fetch the catalog query from enterprise-catalog and pass it directly to Algolia.

**Strengths:**
- Dramatically simplifies the service — no need to facet records by catalog/query/customer
- No batch job needed for catalog-content inclusion; membership is computed on-demand via Algolia
- Real-time updates become possible: when content changes, update the Algolia record; catalog membership is immediately reflected

**Weaknesses:**
- `contains_content_items` (~30 RPM) would become Algolia API calls — expensive and subject to rate limits (caching helps)
- Algolia's query language may not support all the filter types used in `CatalogQuery.content_filter` — needs verification
- Significant frontend changes required to construct and execute Algolia queries from catalog query definitions
- Algolia as the system-of-record creates a hard external dependency for a core business operation (catalog membership)
- Cost: Algolia pricing scales with operations

### Idea 2: Algolia as the Logic Engine (ETL + Algolia for inclusion, DB as SOR)

**Concept:** Similar to Idea 1 but enterprise-catalog DB remains the system of record. Catalog queries are run against Algolia periodically; results are stored in the DB. Downstream clients continue querying enterprise-catalog as today.

**Strengths:**
- DB remains authoritative for catalog-content inclusion (no downstream client changes)
- Algolia is optimized for the exact filter operations catalog queries need
- Reduces expensive Python-level filtering loops
- Incremental updates become more feasible: on content change → run affected catalog queries against Algolia → update DB

**Weaknesses:**
- Still requires periodic full re-sync to keep DB and Algolia in sync
- Algolia rate limits and cost for batch inclusion queries
- Complexity: must keep two systems in sync (Algolia index and DB)
- Algolia outage degrades catalog-content inclusion freshness

### Idea 3: Internally-Managed Search Index as Logic Engine

**Concept:** Run an internal search index (Elasticsearch/OpenSearch) owned by enterprise-catalog for catalog-content inclusion logic. Use it to answer:
- "Given this catalog query, which content records match?" (1 query)
- "Given this modified content record, which catalog queries now match it?" (percolation queries)

**Strengths:**
- No external dependency on Algolia for core business logic
- Search engines are purpose-built for exactly these query patterns — far more scalable than naive Python filtering
- Percolation queries (document-matches-query) directly solve the "which queries does this changed content match?" problem
- If this search index is good enough, could replace Algolia for frontends too (eliminating Algolia cost/dependency)

**Weaknesses:**
- Significant operational overhead: owning and maintaining an Elasticsearch/OpenSearch cluster
- Adds a new infrastructure dependency
- Data must be kept in sync between MySQL DB and the search index
- Frontend integration with Algolia would need to be migrated if Algolia is eliminated

---

## 5. Feasibility of Incremental Syncing

### Catalog Query Changes (feasible)
- ~2 writes/day to `CatalogQuery`
- For each write: run the filter against all ~33,500 content records
- ~67,000 filter ops/day = ~47/minute = ~1/second (bursty)
- **Conclusion: Incremental sync of catalog query changes is feasible with naive approach**

### Content Metadata Changes (not feasible naively)
- ~1,439 distinct catalog queries
- ~2,300 upstream course updates/day = ~1.5 updates/minute
- Naive approach: 1,439 × 1.5 = **2,100 filter operations/minute = 35/second continuously**
- **Conclusion: Naive incremental sync of content changes is NOT feasible**

### Non-Naive Approaches for Content Changes:
1. **Diff-based filtering**: Only re-run catalog queries that filter on attributes that actually changed
2. **Attribute-indexed queries**: Index catalog queries by their filterable attributes so we can quickly find which queries care about a changed attribute
3. **Batching**: Accumulate N changed records before running all filters; amortizes DB read cost and handles rapid repeated changes to the same record

---

## 6. Important Decision Points (Requiring Team Input)

### Decision 1: Event Bus Commitment (Content Replication)
**Question:** Do we commit to building an async event consumer for content metadata changes? This is blocked on upstream (course-discovery) publishing those events via openedx-events.
- **If yes**: Must coordinate with discovery team to publish `ContentMetadataChanged` events; short-term polling fallback required
- **If no**: Continue with scheduled polling; miss the opportunity for real-time consistency

### Decision 2: Catalog-Content Inclusion Backend (Most Critical)
**Question:** Which backend drives catalog-content inclusion logic?
- **Option A: Continue naive Python filtering** — No new infrastructure, but doesn't scale for real-time syncing
- **Option B: Algolia as logic engine** — Leverages existing Algolia investment, but couples core business logic to a paid 3rd party; cost concerns at scale
- **Option C: Internal search index** — Most scalable and independent, but highest operational cost; could eventually replace Algolia for frontends

### Decision 3: Algolia Facets on Customer/Catalog/Query (Search Indexing)
**Question:** Do we continue building `(customer, catalog, catalog_query)` facets on Algolia records?
- **If yes**: Must keep synchronous batch processing; no simplification possible in indexing
- **If no**: Frontend must construct and execute catalog queries against Algolia directly; massive simplification to indexing but significant frontend migration cost

### Decision 4: Input/Output Schema Contracts (Content Replication)
**Question:** Do we adopt `jsonschema` (or similar) to formalize contracts for content metadata?
- This is a prerequisite for meaningful isolation between the three applications
- Requires agreement on what the "enterprise-normalized" output schema looks like

### Decision 5: `is_discoverable` Facet
**Question:** Should we index all content and use a facet to control discoverability, rather than filtering at index-build time?
- Relatively low-risk, high observability gain
- Could be implemented independently of other decisions

---

## 7. Implementation Plan

### Phase 0: Preparation (Low Risk, High Value)
These can be done now without architectural commitment:

1. **Establish input/output schema contracts** for content metadata using `jsonschema`. Define what `ContentMetadata._json_metadata` must contain and what the enterprise-normalized form must provide. This is foundational to all three future apps.

2. **Add `is_discoverable` facet to Algolia objects**. Decouple the "should this be indexed" decision from the "is this in the index" state. Improves observability immediately.

3. **Improve observability**: Add Django admin views / management commands that make it easy to see which content is currently associated with a given catalog, and the health of the last sync job.

4. **Decompose the `update_content_metadata` task** into clearly separated phases with distinct logging/monitoring for (a) content replication and (b) catalog-content inclusion. Even within the same code, making the boundary explicit reduces cognitive overhead.

### Phase 1: App Separation (Structural Refactor, No Behavior Change)
Restructure the existing `catalog` app into three well-bounded Django apps without changing business logic:

- `content_replication/` — Models: `ContentMetadata`, `RestrictedCourseMetadata`, `ContentTranslation`. Tasks: discovery API fetching, metadata transform. API: `/api/v1/content-metadata/`.
- `catalog_inclusion/` — Models: `EnterpriseCatalog`, `CatalogQuery`, M2M associations. Tasks: catalog-content inclusion logic. API: existing catalog endpoints.
- `search_indexing/` — No models. Tasks: Algolia object construction and indexing. API: reindex trigger endpoint.

This is a large refactor but preserves behavior while establishing the domain boundaries called for in the spec.

### Phase 2: Content Replication Hardening
- Implement `jsonschema` validation on ingested content metadata (schema contract enforcement)
- Add explicit error isolation: a content metadata schema violation should NOT block catalog-content inclusion
- Instrument the replication pipeline with detailed monitoring traces
- Expose `/api/v1/content-metadata/{content-key}` endpoint for direct content lookup outside catalog context

### Phase 3: Catalog-Content Inclusion Backend (Requires Decision 2)
**If Decision 2 = Option B (Algolia as logic engine):**
- Implement batch catalog-query-to-Algolia-query translation
- Run translated queries against Algolia to determine content membership
- Store results in DB as today; DB remains SOR

**If Decision 2 = Option C (Internal search index):**
- Stand up OpenSearch/Elasticsearch within enterprise-catalog's infrastructure
- Index content metadata records into this internal index
- Implement percolation queries for "which catalog queries match this changed content?"
- Implement batch query execution for "which content matches this catalog query?"
- Replace naive Python filtering with search engine queries

### Phase 4: Async Event Integration (Requires Decision 1)
- Coordinate with discovery team on publishing `ContentMetadataChanged` openedx-events
- Implement an async event consumer in the content replication app
- Replace (or supplement) the scheduled polling job with event-driven replication
- Maintain polling as a reconciliation fallback

### Phase 5: Search Indexing Simplification (Requires Decision 3)
**If Decision 3 = drop customer/catalog/query facets:**
- Remove facet-building logic from Algolia indexing
- Update frontends (Learner Portal, Admin Portal, Public Catalog MFE) to construct Algolia queries from catalog query definitions
- Move toward async, per-record Algolia updates triggered by content replication events

**If Decision 3 = keep facets:**
- Keep synchronous batch processing; optimize where possible
- Ensure indexing app pulls pre-computed transformation data from content replication app rather than recomputing it

---

## 8. Recommended Approach

Given the constraints, the following approach balances risk, value, and feasibility:

1. **Phase 0 now** — schema contracts + `is_discoverable` facet + observability improvements. Low risk, immediate value, unblocks later phases.

2. **Phase 1 (app separation) next** — Structural refactor establishes domain boundaries and enables independent testing/deployment of each domain in the future.

3. **Decision 2 = Option B (Algolia as logic engine)** — This is the pragmatic choice: it leverages existing Algolia investment, doesn't require new infrastructure, and substantially reduces Python-level filtering overhead. The cost concern is real but manageable with caching. It keeps the DB as the system of record for catalog-content inclusion, so no downstream client changes are needed.

4. **Decision 3 = keep facets for now, with `is_discoverable`** — Dropping facets requires significant frontend migration. This should be deferred until the inclusion logic is stabilized. The `is_discoverable` facet is a low-cost improvement that can be done in Phase 0.

5. **Decision 1 = build async consumer when events are available** — Don't block on this. Continue polling in the near term. Monitor openedx-events for content metadata change events and implement the consumer when they're published.

---

## 9. Open Questions for Team Discussion

1. **Can Algolia's filter/query syntax fully express all current `CatalogQuery.content_filter` patterns?** Need to audit the full set of filter operators used in production queries against Algolia's capabilities.

2. **What is the cost model for significantly increased Algolia operations** (e.g. running ~1,439 catalog queries against Algolia per content change batch)?

3. **Is there appetite for the frontend migration** required to drop customer/catalog/query facets from Algolia records? This is likely the highest-leverage simplification but also the highest coordination cost.

4. **Who owns the upstream event publishing?** The async event-driven approach depends on course-discovery publishing `ContentMetadataChanged` events. Is that team aligned on this?

5. **What is the acceptable staleness window** for catalog-content inclusion? This determines whether near-real-time incremental syncing is actually required, or if the current daily batch is acceptable with better error isolation.
