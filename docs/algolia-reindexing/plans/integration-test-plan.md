# Integration Test Plan: Phase 4a & 4b Algolia Incremental Indexing

## Status: Draft

---

## 1. Problem

Phase 4a (`dispatch_algolia_indexing`) and Phase 4b
(`dispatch_algolia_indexing_for_catalog_query`) are implemented in
`enterprise_catalog/apps/search/tasks.py`.  We need an integration test
harness that:

- Runs against a **real Algolia sandbox** (local devstack app or staging app)
  without depending on pre-existing Django records.
- Is **idempotent**: loads a known fixture set before the run and cleans it up
  after, whether the run succeeds or fails.
- Is **environment-agnostic**: which Algolia application to target is
  determined entirely by environment variables, so the same command works for
  both local and staging.
- Produces **verifiable outcomes**: after each scenario the command queries
  Algolia directly to assert that the right records were indexed with the
  right enterprise-catalog facets.

---

## 2. Approach Summary

| Concern | Decision |
|---|---|
| Entrypoint | Django management command `run_algolia_integration_tests` |
| Celery | `CELERY_TASK_ALWAYS_EAGER = True` — batch tasks run inline/synchronously |
| Algolia credentials | Env vars (see §3) |
| Fixture format | Single JSON file loaded/cleaned by Python code (not Django dumpdata) |
| Algolia write latency | Poll loop with configurable timeout (default 10 s) after each scenario |
| Cleanup | `try / finally` in the command — always runs unless `--no-cleanup` |

---

## 3. Environment Variables

The command fails fast if these are missing:

```
ALGOLIA_APP_ID             Algolia Application ID
ALGOLIA_API_KEY            Algolia Admin API Key (write access required)
ALGOLIA_INDEX_NAME         Primary index name
ALGOLIA_REPLICA_INDEX_NAME  Replica index name (required)
```

The command overrides `settings.ALGOLIA` from these values at runtime, so
no settings file changes are needed between environments.

---

## 4. File Structure

```
tests/
└── integration/
    ├── __init__.py
    ├── fixtures/
    │   └── algolia_reindexing.json       # all fixture data
    └── algolia_reindexing/
        ├── __init__.py
        ├── loader.py                     # fixture load / teardown
        ├── assertions.py                 # Algolia poll + assert helpers
        └── scenarios.py                  # one function per test scenario

enterprise_catalog/apps/search/management/commands/
└── run_algolia_integration_tests.py      # management command entrypoint

docs/algolia-reindexing/
└── integration-test-plan.md             # this document
```

---

## 5. Fixture Data

### 5.1 Content Records

Four `ContentMetadata` records — three courses and one program.

| `content_key` | `content_type` | Title | In Program? |
|---|---|---|---|
| `edX+DemoX` | `course` | edX Demonstration Course | ✅ |
| `edX+E2E-101` | `course` | Manual Smoke Test Course 1 - Auto | ✅ |
| `IBM+CAD0210EN` | `course` | Developing Front End Apps with React | ❌ |
| `96b52724-0a92-44e7-a856-dbbfbc696a6a` | `program` | edX Demonstration Program | — |

`_json_metadata` for each record is stored verbatim in
`tests/integration/fixtures/algolia_reindexing.json` using real data from
the developer's local environment (see §5.4 for field requirements).

### 5.2 Catalog Queries (fixed UUIDs for reproducibility)

| Constant | UUID | Covers |
|---|---|---|
| `CQ1_UUID` | `aaaaaaaa-1111-1111-1111-000000000001` | Program only |
| `CQ2_UUID` | `aaaaaaaa-1111-1111-1111-000000000002` | All 3 courses |
| `CQ3_UUID` | `aaaaaaaa-1111-1111-1111-000000000003` | `IBM+CAD0210EN` only |

### 5.3 Enterprise Catalogs

One `EnterpriseCatalog` per `CatalogQuery`.  These are required because
`_get_algolia_products_for_batch` populates `enterprise_catalog_query_uuids`
by traversing `CatalogQuery → enterprise_catalogs (reverse FK)`.  Without an
associated catalog the content is indexed with empty enterprise facets.

| Constant | UUID | `enterprise_uuid` | References |
|---|---|---|---|
| `EC1_UUID` | `bbbbbbbb-1111-1111-1111-000000000001` | `cccccccc-1111-1111-1111-000000000001` | CQ1 |
| `EC2_UUID` | `bbbbbbbb-1111-1111-1111-000000000002` | `cccccccc-1111-1111-1111-000000000002` | CQ2 |
| `EC3_UUID` | `bbbbbbbb-1111-1111-1111-000000000003` | `cccccccc-1111-1111-1111-000000000003` | CQ3 |

### 5.4 M2M Associations

**`ContentMetadata.catalog_queries`** (content → which queries include it):

```
edX+DemoX          → [CQ2]
edX+E2E-101        → [CQ2]
IBM+CAD0210EN      → [CQ2, CQ3]
<program-uuid-key> → [CQ1]
```

**`ContentMetadata.associated_content_metadata`** (program → child courses,
drives `IndexingMappings.program_to_course_keys`):

```
<program-uuid-key> → [edX+DemoX, edX+E2E-101]
```

### 5.5 `_json_metadata` Field Requirements

Each content record's `_json_metadata` must satisfy the indexability checks
in `algolia_utils.py` or the record will land in the SKIPPED/non-indexable
bucket and the scenarios will fail silently.

**Course requirements** (`_should_index_course`):

| Field | Requirement |
|---|---|
| `advertised_course_run_uuid` | Must match a UUID in `course_runs[*].uuid` |
| `course_runs` | Advertised run must have `is_enrollable: true` and `availability` not falsy |
| `owners` | At least one entry |
| `normalized_metadata.enroll_by_date` | Null (no deadline) OR a future date |
| `course_runs[advertised].hidden` | `false` or absent |

**Program requirements** (`_should_index_program`):

| Field | Requirement |
|---|---|
| `marketing_url` | Non-empty string |
| `type` | Non-empty string |
| `hidden` | `false` or absent |
| `status` | `"active"` |

**Known gotchas in the fixture data**:

- `edX+E2E-101`: `normalized_metadata.enroll_by_date` is `null` → passes
  (null deadline is treated as "never expires").
- `IBM+CAD0210EN`: `advertised_course_run_uuid` is
  `98212baf-5efe-4122-a7e2-29254e981b63`.  The fixture's `course_runs` list
  **must** include the run with that UUID; if it is absent the course will not
  be indexed.
- Program `content_key` is its UUID string (no `key` field in json_metadata).
  The program's `marketing_url` in the fixture is a relative path
  (`"masters/micromasters/..."`), which passes the non-empty check.

### 5.6 Fixture JSON Schema

```json
{
  "content_metadata": [
    {
      "content_key": "<key>",
      "content_uuid": "<uuid>",
      "content_type": "course|program",
      "parent_content_key": null,
      "_json_metadata": { ... }
    }
  ],
  "catalog_queries": [
    {
      "uuid": "<cq-uuid>",
      "title": "<human-readable title>",
      "content_filter": {}
    }
  ],
  "enterprise_catalogs": [
    {
      "uuid": "<ec-uuid>",
      "enterprise_uuid": "<enterprise-uuid>",
      "enterprise_name": "<name>",
      "title": "<title>",
      "catalog_query_uuid": "<cq-uuid>"
    }
  ],
  "associations": {
    "content_catalog_queries": {
      "<content_key>": ["<cq-uuid>", ...]
    },
    "program_courses": {
      "<program-content-key>": ["<child-course-content-key>", ...]
    }
  }
}
```

---

## 6. Expected Algolia State After a Full Index

| content_key | `enterprise_catalog_query_uuids` |
|---|---|
| `edX+DemoX` | `[CQ2_UUID]` |
| `edX+E2E-101` | `[CQ2_UUID]` |
| `IBM+CAD0210EN` | `[CQ2_UUID, CQ3_UUID]` |
| program | `[CQ1_UUID, CQ2_UUID]` |

> **UUID inheritance is upward, not downward.**  The legacy generator
> (`_get_algolia_products_for_batch`) folds child facets into parent objects.
> A program collects the `enterprise_catalog_query_uuids` from all its child
> courses, so the program gets `CQ2_UUID` (from its courses) in addition to
> `CQ1_UUID` (its own direct membership).  The reverse is not true: courses
> do **not** inherit the program's `CQ1_UUID` just because the program is in
> CQ1.

---

## 7. Scenarios

### Scenario 1 — Batch task: Force Index All

**Purpose**: Verify that the batch tasks `index_courses_batch_in_algolia` and
`index_programs_batch_in_algolia` (called with `force=True`) index all four
fixture records into Algolia with correct enterprise catalog facets.

**Isolation note**: The full `dispatch_algolia_indexing` dispatcher queries
*all* `ContentMetadata` in the database.  In a populated local or staging
environment this would sweep thousands of records, making exact counts
unverifiable.  Scenarios 1 and 2 therefore call the batch tasks directly
with only the fixture content keys, bypassing the dispatcher's DB sweep.  The
dispatcher's stale-detection and fanout logic is covered by the unit test suite
in `test_tasks.py`.

**Setup**: Delete all `ContentMetadataIndexingState` rows for fixture content
keys (ensures a "never indexed" baseline).

**Steps**:

1. Call `index_courses_batch_in_algolia(content_keys=FIXTURE_COURSE_KEYS, force=True)`.
2. Call `index_programs_batch_in_algolia(content_keys=[FIXTURE_PROGRAM_KEY], force=True)`.
3. Wait for Algolia indexing to settle (poll loop; see §9).
4. Assert Algolia **facets** for each content key match the table in §6.
5. Assert all four `ContentMetadataIndexingState.last_indexed_at` values are
   now non-null and recent.

**Verifies**: The batch tasks successfully write to Algolia and the
`enterprise_catalog_query_uuids` facets reflect the fixture's catalog
membership.

---

### Scenario 2 — Batch task: Stale Skip Logic

**Purpose**: Verify that a non-force batch task run re-indexes only stale
records and skips up-to-date ones.

**Prerequisites**: Scenario 1 completed (all four records indexed,
`last_indexed_at` is set for each).

**Isolation note**: Same as Scenario 1 — we call the batch task directly to
avoid the global dispatcher sweep.

**Setup**:

1. Update `edX+DemoX`'s `_json_metadata` (e.g. append
   `_integration_test_touch: true`) — this advances `ContentMetadata.modified`.
2. Backdate `edX+DemoX`'s `indexing_state.last_indexed_at` to a timestamp
   older than `ContentMetadata.modified` (simulating a stale record).

**Steps**:

1. Call `index_courses_batch_in_algolia(content_keys=FIXTURE_COURSE_KEYS, force=False)`.
2. Assert `edX+DemoX`'s `last_indexed_at` is **updated** (more recent than
   before the run).
3. Assert `edX+E2E-101` and `IBM+CAD0210EN` `last_indexed_at` values are
   **unchanged** (skipped because not stale).

**Verifies**: The stale detection logic in `_resolve_indexing_decision`
correctly compares `ContentMetadata.modified` against
`ContentMetadataIndexingState.last_indexed_at` and skips non-stale records.

---

### Scenario 3 — 4b: Per-Catalog Dispatch (basic)

**Purpose**: Verify that `dispatch_algolia_indexing_for_catalog_query`
dispatches only the content belonging to the given `CatalogQuery`.

**Prerequisites**: Algolia has fresh records from Scenario 1 (or an
independent fresh load).

**Steps**:

1. Call `dispatch_algolia_indexing_for_catalog_query(catalog_query_id=CQ3.id,
   force=True)`.
2. Assert dispatcher **summary**:
   - `db_membership_count == 1` (only `IBM+CAD0210EN` is in CQ3)
   - `dispatched.course.records == 1`
   - `dispatched.program.records == 0`
3. Assert Algolia: `IBM+CAD0210EN`'s `enterprise_catalog_query_uuids` still
   contains `CQ3_UUID`.

**Verifies**: `_get_catalog_query_content_keys_by_type` correctly resolves
DB membership for a given `CatalogQuery`, and only that content is dispatched.

---

### Scenario 4 — 4b: Membership Removal

**Purpose**: Verify that when content is removed from a `CatalogQuery`'s DB
membership, the per-catalog dispatcher detects the diff against Algolia and
re-indexes the removed content so its stale facet is cleared.

**Prerequisites**: All four records indexed in Algolia (Scenario 1 completed).
`IBM+CAD0210EN` has `CQ2_UUID` in its `enterprise_catalog_query_uuids`.

**Setup**:

1. Remove `IBM+CAD0210EN` from CQ2's DB membership:
   ```python
   ibm_cm.catalog_queries.remove(cq2)
   ```
   CQ2's DB membership is now `[edX+DemoX, edX+E2E-101]` (2 courses).
   Algolia still has `IBM+CAD0210EN` indexed under `CQ2_UUID` (stale).

**Steps**:

1. Call `dispatch_algolia_indexing_for_catalog_query(catalog_query_id=CQ2.id,
   force=True)`.
2. Assert dispatcher **summary**:
   - `db_membership_count == 2`
   - `algolia_membership_count >= 3` (IBM was in Algolia under CQ2 before)
   - `removed_count >= 1` (IBM detected as removed)
   - `IBM+CAD0210EN` appears in the dispatched course batch (as a removed
     key that must be re-indexed)
3. Wait for Algolia settle.
4. Assert Algolia: `IBM+CAD0210EN`'s `enterprise_catalog_query_uuids` **no
   longer contains** `CQ2_UUID`.
5. Assert Algolia: `IBM+CAD0210EN`'s `enterprise_catalog_query_uuids` **still
   contains** `CQ3_UUID` (membership in CQ3 was not changed).

**Verifies**: The Algolia→DB diff in `dispatch_algolia_indexing_for_catalog_query`
correctly detects membership removals, and the batch task re-indexes the
removed content so stale facets are cleaned up.

**Teardown note**: Restore `IBM+CAD0210EN` to CQ2's membership after the
scenario so subsequent scenarios start from a consistent state, or ensure
this scenario always runs last.

---

## 8. Loader Design (`loader.py`)

```python
FIXTURE_CONTENT_KEYS = [
    "edX+DemoX",
    "edX+E2E-101",
    "IBM+CAD0210EN",
    "96b52724-0a92-44e7-a856-dbbfbc696a6a",
]
FIXTURE_CATALOG_QUERY_UUIDS = [
    "aaaaaaaa-1111-1111-1111-000000000001",  # CQ1
    "aaaaaaaa-1111-1111-1111-000000000002",  # CQ2
    "aaaaaaaa-1111-1111-1111-000000000003",  # CQ3
]
FIXTURE_ENTERPRISE_CATALOG_UUIDS = [
    "bbbbbbbb-1111-1111-1111-000000000001",  # EC1
    "bbbbbbbb-1111-1111-1111-000000000002",  # EC2
    "bbbbbbbb-1111-1111-1111-000000000003",  # EC3
]


def load_fixtures():
    """
    Idempotently load all fixture data.

    Steps:
      1. cleanup_db_fixtures() — remove any stale copies from a prior run.
      2. Create ContentMetadata records from fixture JSON.
      3. Create CatalogQuery records (by UUID).
      4. Create EnterpriseCatalog records (FK to CatalogQuery).
      5. Set catalog_queries M2M on ContentMetadata records.
      6. Set associated_content_metadata M2M on the program record.
      7. invalidate_indexing_mappings_cache() so the dispatcher recomputes.

    Returns:
      (content_map, cq_map, ec_map) — dicts keyed by content_key/uuid.
    """

def cleanup_db_fixtures():
    """
    Delete fixture records from the DB.

    Order matters:
      1. EnterpriseCatalog (FK to CatalogQuery; Django does not cascade
         CatalogQuery deletion to EnterpriseCatalog by default — CatalogQuery
         is nullable on EnterpriseCatalog).
      2. ContentMetadata (cascades to ContentMetadataIndexingState via
         OneToOneField(on_delete=CASCADE)).
      3. CatalogQuery.
    """

def cleanup_algolia_objects(algolia_client):
    """
    Delete Algolia objects written for the fixture content keys.

    Reads algolia_object_ids from ContentMetadataIndexingState for each
    fixture content key, then calls algolia_client.delete_objects_batch()
    for all collected IDs.  Best-effort: logs warnings on failure but does
    not raise.
    """
```

---

## 9. Assertions Design (`assertions.py`)

```python
ALGOLIA_POLL_INTERVAL_SECONDS = 2


def wait_and_assert_enterprise_catalog_query_uuids(
    algolia_index,
    aggregation_key,
    expected_uuids,
    absent_uuids=None,
    timeout=10,
):
    """
    Poll Algolia until:
      - At least one object with the given aggregation_key exists.
      - The union of enterprise_catalog_query_uuids across all matching
        objects contains every UUID in expected_uuids.
      - (If absent_uuids is given) none of those UUIDs appear.

    Raises AssertionError on timeout.

    Implementation notes:
      - Use algolia_index.browse_objects({'attributesToRetrieve':
          ['aggregation_key', 'enterprise_catalog_query_uuids'],
          'filters': f'aggregation_key:"{aggregation_key}"'})
        to retrieve all shards for the content.
      - Collect the union of enterprise_catalog_query_uuids across shards
        (a single piece of content may be split across multiple Algolia
        objects / shards when catalog membership is large).
    """


def assert_indexing_state_updated(content_key, after_timestamp):
    """
    Assert that ContentMetadataIndexingState.last_indexed_at for content_key
    is non-null and >= after_timestamp.
    """


def assert_indexing_state_unchanged(content_key, expected_timestamp):
    """
    Assert that ContentMetadataIndexingState.last_indexed_at for content_key
    equals expected_timestamp (i.e. the record was not re-indexed).
    """
```

---

## 10. Management Command Design

```
./manage.py run_algolia_integration_tests [options]

  --scenario {all,1,2,3,4}
                    Which scenario to run (default: all).
  --no-cleanup      Skip DB + Algolia cleanup after the run.
                    Useful for debugging a failed scenario.
  --dry-run         Run dispatchers with dry_run=True (no Algolia writes).
                    Useful for verifying fixture load and dispatch logic.
  --algolia-wait N  Seconds to wait for Algolia indexing (default: 10).
```

**Runtime overrides applied by the command**:

```python
# Pull Algolia credentials from environment
settings.ALGOLIA = {
    'APPLICATION_ID': os.environ['ALGOLIA_APP_ID'],
    'API_KEY':        os.environ['ALGOLIA_API_KEY'],
    'INDEX_NAME':     os.environ['ALGOLIA_INDEX_NAME'],
    'REPLICA_INDEX_NAME': os.environ.get('ALGOLIA_REPLICA_INDEX_NAME', ''),
}

# Run Celery tasks synchronously (no worker needed)
settings.CELERY_TASK_ALWAYS_EAGER = True
settings.CELERY_TASK_EAGER_PROPAGATES = True
```

**Execution flow**:

```
1. Validate env vars (raise CommandError if missing).
2. Apply runtime settings overrides.
3. loader.load_fixtures()
4. try:
       for each scenario in selected_scenarios:
           invalidate_indexing_mappings_cache()
           run scenario function
           report PASS / FAIL with exception traceback
   finally:
       unless --no-cleanup:
           loader.cleanup_algolia_objects(algolia_client)
           loader.cleanup_db_fixtures()
5. Print summary table (scenario name | PASS/FAIL | duration).
6. Exit with non-zero code if any scenario failed.
```

---

## 11. Running the Tests

**Against local devstack**:

```bash
export ALGOLIA_APP_ID=<local-sandbox-app-id>
export ALGOLIA_API_KEY=<local-sandbox-api-key>
export ALGOLIA_INDEX_NAME=<local-index-name>
export ALGOLIA_REPLICA_INDEX_NAME=<local-replica-name>   # required

# From inside the app container (make app-shell):
./manage.py run_algolia_integration_tests

# Run a single scenario:
./manage.py run_algolia_integration_tests --scenario 4

# Debug a failure without cleaning up DB/Algolia:
./manage.py run_algolia_integration_tests --scenario 4 --no-cleanup
```

**Against staging**:

```bash
export ALGOLIA_APP_ID=<staging-app-id>
export ALGOLIA_API_KEY=<staging-api-key>
export ALGOLIA_INDEX_NAME=<staging-index-name>

./manage.py run_algolia_integration_tests
```

---

## 12. Gotchas and Design Notes

### `EnterpriseCatalog` is required for Algolia enterprise facets

`_get_algolia_products_for_batch` traverses
`CatalogQuery → enterprise_catalogs (reverse FK from EnterpriseCatalog.catalog_query)`
to compute `enterprise_catalog_query_uuids`.  A `CatalogQuery` with no
associated `EnterpriseCatalog` produces content indexed with *empty* enterprise
facets — the content will appear in Algolia but won't be findable via enterprise
catalog filters.  The fixture must include at least one `EnterpriseCatalog` per
`CatalogQuery`.

### UUID inheritance is upward only

When a program's child courses are in CQ2, the *program* object inherits
`CQ2_UUID` into its `enterprise_catalog_query_uuids`.  The courses do **not**
inherit `CQ1_UUID` from the program's direct membership in CQ1.  This matches
the note in `_get_algolia_products_for_batch`:

> "If a course is part of a program, but only the program is in a given
> catalog, that catalog will only be indexed as part of the program."

### IBM course advertised run

`IBM+CAD0210EN`'s `advertised_course_run_uuid` is
`98212baf-5efe-4122-a7e2-29254e981b63`.  The fixture's `course_runs` list
**must include** the run with that exact UUID, otherwise
`get_advertised_course_run()` returns `None` and the course is skipped by
`_should_index_course`.

### Cache invalidation between scenarios

`get_indexing_mappings()` caches the `program_to_course_keys` mapping in
Redis/memcache.  Call `invalidate_indexing_mappings_cache()` at the start of
each scenario to prevent a stale mapping from a previous scenario (or a
previous run) from causing incorrect stale-detection results.

### Algolia write latency

Algolia's `save_objects` is asynchronous on their side: the SDK call returns
after queueing the write, not after it is visible in search.  Even in sandbox
environments, propagation typically takes 1–5 seconds.  The assertion helpers
use a poll loop (check every 2 s, timeout after N seconds) rather than a
fixed sleep so CI runs finish as fast as possible while still being robust.

### Scenario 4 teardown

Scenario 4 removes `IBM+CAD0210EN` from CQ2's M2M during the test.  If
running `--scenario all`, this mutation affects subsequent runs unless the
full `cleanup_db_fixtures` → `load_fixtures` cycle runs between scenarios.
The command's `try/finally` block re-loads fixtures between scenarios when
running in "all" mode, or relies on the final cleanup for single-scenario
runs.  When using `--no-cleanup`, re-run with a fresh `load_fixtures` before
running other scenarios.

### `edX+E2E-101` seat type

`edX+E2E-101` has only a `masters` seat type.  The `_should_index_course`
check does not filter on seat type, so this course remains indexable.

---

## 13. Out of Scope

- Testing pathway (`learner_pathway`) content — no pathway fixtures are
  included in this initial set.  Pathways can be added in a follow-up.
- Testing `force=False` stale detection for programs (child-staleness via
  `program_to_course_keys`) — Scenario 2 covers a simplified version; a
  dedicated program-staleness scenario can be added later.
- Load / performance testing — this harness is for functional correctness
  only.
- Automated CI integration — these tests require real Algolia credentials
  and are designed to be run manually or in a privileged CI environment.
