# Content Sync and Algolia Indexing

How course content gets from `course-discovery` into this service's database, and from the
database into the Algolia search index that the learner and admin portals query.

Two halves, joined by one table:

```
course-discovery ──▶ ContentMetadata (MySQL) ──▶ Algolia index
      sync                          incremental indexing
```

The first half is a pull: management commands and Celery tasks fetch from Discovery and write
`ContentMetadata` rows plus catalog-membership associations. The second half is a push: a
dispatcher decides which records are stale, fans small batches out to Celery workers, and each
batch upserts Algolia objects and deletes the ones that no longer belong.

`ContentMetadata.modified` is the hinge between the two. Staleness detection is entirely
driven by comparing it against `ContentMetadataIndexingState.last_indexed_at`, so anything
that touches `modified` without a real content change causes needless reindexing, and anything
that changes content without bumping `modified` silently leaves the index stale.

Courses, programs, and learner pathways all follow that path. Videos do not: they arrive from
LMS through a separate set of commands and are indexed off their parent course's staleness.
Both halves of the video story are called out separately below.

## Part 1: Discovery to the database

### The daily sync

`./manage.py update_content_metadata` runs the whole sync. In order
(`enterprise_catalog/apps/catalog/management/commands/update_content_metadata.py`):

1. `fetch_missing_pathway_metadata_task` loads every `learnerpathway` from Discovery, then the
   programs and courses those pathways reference, and wires up the associations. Pathway
   membership isn't discoverable from course or program metadata, so it has to be loaded from
   the pathway side.
2. `fetch_missing_course_metadata_task` finds courses referenced by a program's
   `json_metadata` that have no `ContentMetadata` row and fetches them. Program-only courses
   are the usual gap.
3. A Celery `group` of `update_catalog_metadata_task`, one per `CatalogQuery` that at least one
   `EnterpriseCatalog` actually uses. Each calls Discovery `/api/v1/search/all/` with the
   query's `content_filter`, creates or updates `ContentMetadata` rows, and rewrites that
   query's content associations. Queries with `restricted_runs_allowed` also run
   `synchronize_restricted_content`, which fetches the restricted runs and populates
   `RestrictedCourseMetadata`.
4. `update_full_content_metadata_task`, run synchronously via `.apply()` rather than dispatched,
   because on production-sized data it outlives a worker's task timeout. It replaces the thin
   `/search/all/` payload with the full record from `/api/v1/courses/` and `/api/v1/programs/`,
   merges course reviews, generates normalized metadata, and updates the restricted overrides.
5. `dispatch_algolia_indexing`, which is where Part 2 begins.

A group is used in step 3 instead of a chord because a `TaskRecentlyRunError` in one member
shouldn't stop step 4 from running. Most of these tasks carry `@expiring_task_semaphore()` and
refuse to run twice within an hour; `--force` bypasses that.

### Which Discovery endpoint supplies what

| Endpoint | Called by | Gives us |
|---|---|---|
| `/api/v1/search/all/` | `update_catalog_metadata_task` | Thin records for every content item matching a `content_filter`, plus the membership set |
| `/api/v1/courses/` | `_update_full_content_metadata_course` | Full course metadata, restricted runs when `QUERY_FOR_RESTRICTED_RUNS` is passed |
| `/api/v1/programs/` | `_update_full_content_metadata_program` | Full program metadata |
| `/api/v1/course_review/` | `_update_full_content_metadata_course` | Ratings feeding `avg_course_rating` and `course_bayesian_average` |

`/search/all/` requests pass `include_modified` so Discovery returns each record's `modified`
timestamp. We compare it before writing, and skip the write when nothing changed. That skip is
what keeps `ContentMetadata.modified` meaningful.

### Restricted courses

A course with restricted runs gets a canonical `RestrictedCourseMetadata` row
(`catalog_query=NULL`) plus one row per catalog query that allows those runs. The canonical row
feeds the Algolia payload for every catalog the course belongs to, including catalogs whose
filter disallows restricted runs, so `_update_full_restricted_course_metadata` restores the
unrestricted parent's `advertised_course_run_uuid` after the restricted fetch overwrites it.
Without that, a restricted run leaks into records for catalogs that shouldn't see it.

### Videos take a separate route in

Videos never come through `/search/all/` and are not part of the daily chain. They start as a
`VideoShortlist` row, which is a `video_usage_key` curated by hand in Django admin. Three
commands in `video_catalog` turn that into an indexable record:

| Command | Does |
|---|---|
| `fetch_video_metadata` | Processes `VideoShortlist` rows where `is_processed=False`, resolves the usage key to its course run, pulls metadata from LMS, and creates the `Video` row with `parent_content_metadata` pointing at the course run's `ContentMetadata`. `--force` reprocesses rows already marked processed |
| `fetch_video_skills` | Fetches skills for each video from Discovery into `VideoSkill` |
| `generate_video_transcript_summary` | Produces the `transcript_summary` that ends up in the index |

A video has no catalog membership of its own. It inherits from its parent course, reached
through `parent_content_metadata.parent_content_key`, which is why video indexing is scheduled
off the parent course's staleness rather than its own.

## Part 2: The database to Algolia

### What one content record looks like in the index

A single course does not map to a single Algolia object. Each record is written as a set of
**shards** that share an `aggregation_key`. For courses, programs, and pathways that key is
`{content_type}:{content_key}` (for example `course:edX+DemoX`), passed through from Discovery.
Videos are the exception, covered below. The index sets `attributeForDistinct: aggregation_key`
with `distinct: True`, so a search returns one hit per content item no matter how many shards
back it.

Sharding exists because membership arrays are unbounded. A course in 4,000 catalogs would
carry 4,000 UUIDs in one object. Instead, membership is chunked at `ALGOLIA_UUID_BATCH_SIZE`
(100) into separate shards:

| Shard objectID | Carries |
|---|---|
| `{content_type}-{uuid}-catalog-uuids-{n}` | `enterprise_catalog_uuids` |
| `{content_type}-{uuid}-customer-uuids-{n}` | `enterprise_customer_uuids` |
| `{content_type}-{uuid}-catalog-query-uuids-{n}` | `enterprise_catalog_query_uuids`, `enterprise_catalog_query_titles` |
| any of the above plus `-es` | Spanish translation, for `ContentMetadata`-backed records only |
| `video-{edx_video_id}-…` | Videos, sharded on the same three axes but keyed differently (below) |

Frontends read through a secured API key, served by the `secured_algolia_api_key` action on the
enterprise-customer viewset (`apps/api/v1/views/enterprise_customer.py`) and built by
`generate_secured_api_key`. It pins `restrictIndices` and a filter on the caller's
`enterprise_catalog_query_uuids`. Membership attributes are in `unretrievableAttributes`, so a
client can filter on them but never read another customer's UUIDs back out.

Course runs are never indexed. They contribute their catalog and customer UUIDs upward:
run to course to program to pathway. Each indexed object carries the union of UUIDs from
everything beneath it in that tree.

### Videos are shaped differently, and it matters

`add_video_to_algolia_objects` sets `aggregation_key` to the bare `edx_video_id`, not
`video:{edx_video_id}`. Videos are the only content type whose aggregation key carries no
content-type prefix. Four consequences worth knowing before you touch this code:

`get_object_ids_for_aggregation_key` cannot find video shards. It is called with the prefixed
form that `_aggregation_key_for` builds, which never matches a video's key.

`index_videos_batch_in_algolia` issues no deletes at all. It writes no
`ContentMetadataIndexingState` rows, so there is no previous shard set to diff against and no
orphan cleanup. A video that loses catalog memberships keeps its stale shards in the index
until someone removes them by hand. Courses, programs, and pathways do not have this problem.

`dispatch_algolia_indexing_for_catalog_query` browses every shard carrying the catalog query's
facet, videos included. Their unprefixed keys fall through `_partition_aggregation_key` and hit
the `KeyError` branch of `_group_aggregation_keys_by_content_type`, which logs
`Ignoring unsupported content_type=… for aggregation key=…`. Expect one warning per video per
per-catalog dispatch. It is noise, not a fault.

`create_spanish_algolia_object` returns `None` for anything that is not a `ContentMetadata`
instance, so videos never get `-es` shards. Video transcripts carry their own language data
independently of that.

A video whose payload exceeds `ALGOLIA_JSON_METADATA_MAX_SIZE` (100KB) is dropped with a
`logger.warning` and nothing else. No state row records the drop, because videos have no state
rows, so the only evidence is that log line.

### What is indexable

`partition_course_keys_for_indexing` and `partition_program_keys_for_indexing` replicate the
B2C indexing rules from `course-discovery`. A course is indexable when it has an advertised
course run, at least one owner, a marketing URL slug, and a verified upgrade deadline in the
future. Pathways are always indexable. The union of the three sets is
`IndexingMappings.all_indexable_content_keys`, and anything outside it is removed from the
index rather than skipped.

### Per-record state

`ContentMetadataIndexingState` (`enterprise_catalog/apps/search/models.py`) holds one row per
`ContentMetadata`:

`last_indexed_at` when the record was last written to Algolia, `algolia_object_ids` for the
shard IDs that write produced, `last_failure_at` and `failure_reason` for the most recent
failure, `removed_from_index_at` for records that were deleted from the index. The row is the
authority for which shards a record owns; Algolia is only consulted when the row has no
recorded IDs. See ADR 0012.

Videos have no state rows. Their staleness is proxied through the parent course.

### The dispatcher

`dispatch_algolia_indexing` (`enterprise_catalog/apps/search/tasks.py`) decides what to
enqueue and in what order.

Staleness rules per type:

| Type | Dispatched when |
|---|---|
| `course` | Never indexed, `ContentMetadata.modified > last_indexed_at`, or `last_failure_at` set and `include_failed=True` |
| `program` | Same, plus when any child course was indexed more recently than the program |
| `learnerpathway` | Same, plus when any child program was indexed more recently than the pathway |
| `video` | Parent course is in the dispatched course set |

`force=True` skips all of it and dispatches every indexable record.

Child staleness is why ordering matters. A program's own `modified` doesn't advance when one
of its courses changes, so the only signal is the child's newer `last_indexed_at`, and that
timestamp is written only after the child's batch task finishes. Courses must therefore
complete before programs start, and programs before pathways.

`_build_sequential_canvas` gets that ordering with nested chords rather than
`chain(group, group)`. A chain attaches the remaining chain as a link on every task in the
group, so all N tasks call `apply_chord` for the next group concurrently and produce N-1
duplicate `ChordCounter` inserts. Chords route completion through `on_chord_part_return` with
`SELECT FOR UPDATE`, firing the callback exactly once. See ADR 0013.

Every task is enqueued with `.si()` so the previous group's return values aren't forwarded as
positional arguments.

### The batch tasks

`index_courses_batch_in_algolia`, `index_programs_batch_in_algolia`,
`index_pathways_batch_in_algolia` each take up to `ALGOLIA_INDEXING_BATCH_SIZE` (10) content
keys; `index_videos_batch_in_algolia` takes up to 20 `edx_video_id` values. All four run
through three passes:

1. Resolve. Every key becomes an `IndexingDecision` with an outcome of INDEXED, SKIPPED,
   REMOVED, or FAILED. No writes happen here. The only Algolia I/O is the per-record browse in
   `_existing_shard_ids`, and only for rows with no recorded shard IDs.
2. Execute. One `save_objects_batch` for the whole batch, then one `delete_objects_batch` for
   every orphaned and removed shard. If a bulk call raises `AlgoliaException`, the task retries
   record by record to isolate which keys actually failed, mutating those decisions to FAILED.
   Both operations are keyed on `objectID` and therefore idempotent.
3. Finalize. Each decision's actual outcome stamps its state row (`mark_as_indexed`,
   `mark_as_removed`, `mark_as_failed`) and bumps a counter in the returned `BatchSummary`.

Algolia writes happen before the DB write. If the DB write fails afterward, the next run sees
an unadvanced `last_indexed_at` and reindexes, costing one wasted upsert.

Orphan cleanup is per record: the new shard ID set is diffed against the previous one and the
difference is deleted. A course dropping from 300 catalogs to 150 sheds its now-empty
`catalog-uuids-2` shard this way.

A record that is indexable but produces zero Algolia objects is treated as REMOVED, not as an
error. That happens when catalog memberships drop without `modified` advancing, and the record
comes back cleanly on the next run once memberships return.

### The mappings cache

Each batch task needs the program-to-course and pathway-to-program mappings plus the full
indexable key set, all of which are O(catalog size) to compute. `get_indexing_mappings` caches
them under `algolia:indexing_mappings:v1` for `ALGOLIA_INDEXING_MAPPINGS_CACHE_TIMEOUT`
(30 minutes). The dispatcher invalidates the cache explicitly on `force=True` runs and on
every non-dry per-catalog run; the TTL is the safety net.

### Per-catalog indexing

`POST /api/v1/enterprise-catalogs/{uuid}/refresh_metadata/` runs a chain of
`update_catalog_metadata_task`, `update_full_content_metadata_task`, and
`dispatch_algolia_indexing_for_catalog_query`. `edx-enterprise` calls it when an admin creates
or edits a catalog in LMS Django Admin.

That dispatcher does something the daily one doesn't: it diffs the catalog query's current
database membership against what Algolia reports for that query's facet, via
`get_aggregation_keys_for_catalog_query`. Keys in Algolia but not in the database are content
that left the catalog and needs reindexing to shed the stale facet. Keys in the database but
not in Algolia are new members that need the facet added. Both sets bypass the staleness check,
since neither shows up as a `modified` bump.

Videos in this path are always scoped to the catalog query's courses. Passing `force=True`
through to `_get_video_pks_for_dispatch` would return every video in the service and escape the
catalog boundary.

### Index configuration

`ALGOLIA_INDEX_SETTINGS` and `ALGOLIA_REPLICA_INDEX_SETTINGS` in
`enterprise_catalog/apps/catalog/algolia_utils.py` are the source of truth for searchable
attributes, facets, and custom ranking. `incremental_reindex_algolia` pushes them on every
non-dry run, which overwrites anything edited by hand in the Algolia dashboard. The replica is
registered as a virtual replica and differs only in leading with `desc(duration)`.

`ALGOLIA_FIELDS` is the allowlist of attributes that survive into an Algolia object.
`create_algolia_objects` filters everything else out. A field added to the payload builders but
not to `ALGOLIA_FIELDS` never reaches the index.

## Triggers and schedules

| Trigger | Cadence | What it does |
|---|---|---|
| `update_content_metadata` | Daily | Full Discovery sync, then `dispatch_algolia_indexing` for everything changed |
| `incremental_reindex_algolia` | More frequent than daily | Stragglers pass. Indexes records that are stale or previously failed. No Discovery sync |
| `refresh_metadata` API | On demand | Syncs one catalog query and indexes only its membership, including additions and removals |

Neither schedule is defined in this repository. Both commands run as jobs under
`prod-enterprise-catalog` in our internal argocd instance - check internal config to find real numbers.

The stragglers pass is the retry mechanism. A batch that fails permanently after Celery's five
retries leaves `last_failure_at` set, and the next stragglers run picks it up because
`include_failed` defaults to `True`.

## Runbook

### Commands

```bash
# Full sync from Discovery, then index everything that changed
./manage.py update_content_metadata --force

# Stragglers pass: stale and previously-failed records only
./manage.py incremental_reindex_algolia

# Rebuild every indexable record regardless of staleness
./manage.py incremental_reindex_algolia --force-all

# See what would be dispatched, without touching Algolia or index settings
./manage.py incremental_reindex_algolia --dry-run

# Narrow to one or more content types
./manage.py incremental_reindex_algolia --content-type course program

# Run in-process instead of dispatching to workers (debugging)
./manage.py incremental_reindex_algolia --no-async
```

`--force` on `update_content_metadata` means two different things at once: it bypasses the
one-hour task semaphore on the sync tasks, and it passes `force=True` to the dispatcher, which
reindexes every record rather than only stale ones. Use it when you want a full pass; leave it
off for a routine sync.

`--dry-run` skips the index-settings push as well as the writes, so it is safe against a
production index.

### Changing cron behavior without a deploy

`IncrementalReindexAlgoliaConfig` is a `ConfigurationModel` in Django admin. Add a row with
`enabled=True` and its values override the command-line arguments the cron passes, which is how
you schedule a one-off `--force-all` run. Add a new row with `enabled=False` to return to
CLI-driven defaults. Old rows are history, not state; only the most recent one is read.

### Observability

Every stage logs a structured summary. These are the lines to grep first:

| Log line | Emitted by | Tells you |
|---|---|---|
| `dispatch_algolia_indexing summary=…` | Daily and stragglers dispatchers | Per-type record and batch counts, plus `force`, `dry_run`, and the target index |
| `dispatch_algolia_indexing_for_catalog_query summary=…` | Per-catalog dispatcher | The same, plus `db_membership_count`, `algolia_membership_count`, `added_count`, `removed_count`. A large `removed_count` means content left the catalog |
| `index_{type}_batch complete: indexed=… skipped=… removed=… failed=…` | Every batch task | Per-batch outcomes. `failed` above zero means state rows were stamped with a reason |
| `Bulk save_objects_batch raised for N records; falling back to per-record saves.` | `_execute_saves` | Algolia rejected a bulk write. The per-record retry that follows names the specific keys |
| `Indexable {type} … produced zero Algolia objects; treating as REMOVED.` | Decision resolution | A record dropped out of the index without its `modified` advancing, usually a membership change |
| `Ignoring unsupported content_type=… for aggregation key=…` | Per-catalog dispatcher | Videos. Expected noise, see above |

Batch tasks return a `BatchSummary` and results are persisted by `django-celery-results`, so
outcomes for a given run can be queried out of the task result table rather than reconstructed
from logs.

The object-building path is instrumented via `function_trace`. The span
names are in `AlgoliaTraceNames` (`apps/catalog/constants.py`) and cover content loading,
indexability partitioning, object construction, and Spanish object creation. Start there when a
batch is slow rather than failing.

### When a record looks stale in search

1. Find its `ContentMetadataIndexingState` in Django admin. `last_failure_at` and
   `failure_reason` name the problem when there is one.
2. `last_indexed_at` older than `ContentMetadata.modified` means it is queued for the next
   stragglers run and hasn't been picked up yet.
3. `removed_from_index_at` set means the record was deliberately deleted, either because it
   fell out of `all_indexable_content_keys` or because it generated zero objects. Check
   `_should_index_course` against the record's advertised run, owners, and marketing URL.
4. No state row at all means it has never been indexed. The next dispatcher run will treat it
   as stale.
5. If the state row looks correct and Algolia still disagrees, clear `algolia_object_ids` on
   the row. The next run falls back to browsing Algolia for the record's real shards and
   reconciles from there.

### Verifying against the live index

`./manage.py run_algolia_integration_tests` exercises the indexing path against a real Algolia
index. It is not part of the unit test suite and needs credentials.

## Things worth knowing before changing this code

An Algolia "save" is the successful queueing of an async job on Algolia's side, not a durable
write. `save_objects_batch` deliberately does not wait for the job unless
`ALGOLIA_WAIT_FOR_TASKS` is set. Reads immediately after a write can lag. See ADR 0012.

`ContentMetadata.modified` is load-bearing. Any code path that saves a `ContentMetadata`
without a real content change schedules an unnecessary reindex of that record and, through
child staleness, of every program and pathway containing it.

`ALGOLIA_INDEXING_CHUNK_SIZE` (100) is smaller than the SDK's automatic 1,000 on purpose. Each
chunk is an independently failing unit, so a partial failure loses one chunk rather than the
whole batch.

Batch tasks import `_get_algolia_products_for_batch` and `add_video_to_algolia_objects` from
`enterprise_catalog.apps.api.tasks`. The object-building logic lives there, not in the `search`
app. A batch may pull in related content (a course inside a requested program), which is
filtered back out by `aggregation_key` so each content item is written by its own batch.

Videos take a different code path from every other type, described under "Videos are shaped
differently" above. The short version: they live in the `Video` model rather than
`ContentMetadata`, so `index_videos_batch_in_algolia` builds objects directly and must call
`create_algolia_objects` itself for DB enrichment. Skipping that step is what caused the
missing `org`, `partners`, and `duration` fields found during v2 validation. They also have no
state rows, no orphan deletion, and an unprefixed `aggregation_key`.

## Where the code lives

| Path | Contents |
|---|---|
| `apps/catalog/management/commands/update_content_metadata.py` | The daily sync entry point |
| `apps/api/tasks.py` | Discovery sync tasks and all Algolia object building |
| `apps/catalog/models.py` | `update_contentmetadata_from_discovery`, `synchronize_restricted_content` |
| `apps/catalog/algolia_utils.py` | `ALGOLIA_FIELDS`, index settings, indexability rules |
| `apps/search/models.py` | `ContentMetadataIndexingState`, `IncrementalReindexAlgoliaConfig` |
| `apps/search/tasks.py` | Dispatchers and batch indexing tasks |
| `apps/search/indexing_mappings.py` | Cached program/pathway mappings and indexable key set |
| `apps/search/management/commands/incremental_reindex_algolia.py` | Operator entry point |
| `apps/api_client/algolia.py` | Algolia client: batch save/delete, browse, secured keys |
| `apps/video_catalog/models.py` | `Video`, `VideoShortlist`, `VideoSkill` |
| `apps/video_catalog/management/commands/` | `fetch_video_metadata`, `fetch_video_skills`, `generate_video_transcript_summary` |
| `apps/catalog/constants.py` | `AlgoliaTraceNames` span names |

## Related documents

- `docs/decisions/0011-search-app-for-incremental-algolia-indexing.rst`
- `docs/decisions/0012-incremental-indexing-state-authority-and-outcome-contract.rst`
- `docs/decisions/0013-dispatcher-serial-chain-ordering.rst`
- `docs/algolia-reindexing/` for the design and migration record
- `docs/architecture_overview.rst` for normalized metadata and the wider service architecture
