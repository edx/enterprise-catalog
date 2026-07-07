# v1 Decommission Plan: legacy writes off, backend reads to v2

## Context

The v1→v2 Algolia migration is far along in prod: v2 writes are enabled
(`ENABLE_INCREMENTAL_ALGOLIA_INDEXING` on, `INCREMENTAL_INDEX_NAME` set to the v2 index) and
all frontends already read v2. Two loose ends remain in `enterprise-catalog` before v1 can be
removed from `ALLOWED_INDEX_NAMES` and deleted:

1. **Legacy writes still hit v1.** `index_enterprise_catalog_in_algolia_task` →
   `_reindex_algolia` → `replace_all_objects` writes the primary (v1) index. It fires from the
   `reindex_algolia` cron and from the refresh API view. These keep costing write ops on a
   soon-dead index.
2. **Backend reads still hit v1.** Every server-side Algolia read uses
   `algolia_client.algolia_index` (primary = v1), via the admin `API_KEY`, which
   `ALLOWED_INDEX_NAMES` does not gate. Frontends moved; the backend did not. These serve stale
   data (v1 no longer written incrementally) and hard-break when v1 is deleted.

Rollout order: stop legacy writes first, then move backend reads, then retire v1.

## Read accessor design: reuse `INCREMENTAL_INDEX_NAME`

Add one property to `AlgoliaSearchClient` (`enterprise_catalog/apps/api_client/algolia.py`,
near the index-name properties around line 47):

```python
@property
def search_index(self):
    """
    Algolia index object for backend read/search paths.

    Resolves to the incremental (v2) index when INCREMENTAL_INDEX_NAME is set, else
    falls back to the primary index. Reads follow writes onto v2 once the incremental
    pipeline is configured.
    """
    return self._get_index(settings.ALGOLIA.get('INCREMENTAL_INDEX_NAME'))
```

Reuses `_get_index()` (`algolia.py:141`): `None` returns the primary index
(dev/test/pre-cutover, a no-op); a v2 name returns `_client.init_index(v2)`. No new setting.
Read cutover is coupled to the write config, which is fine here: writes and frontend reads are
already on v2, so backend reads should follow.

## Step 1: decommission legacy writes (do first)

- **API-trigger** (`api/v1/views/enterprise_catalog_refresh_data_from_discovery.py`): in the
  `ENABLE_INCREMENTAL_ALGOLIA_INDEXING` branch (lines 41-51), drop
  `index_enterprise_catalog_in_algolia_task.si()` from the `group`, leaving only
  `dispatch_algolia_indexing_for_catalog_query.si(catalog_query_id)`. The group collapses to a
  single chained step. Leave the `else` branch (lines 52-58) untouched: it is the flag-off
  rollback path that restores legacy v1 writes. Drop the now-unused
  `index_enterprise_catalog_in_algolia_task` import if nothing else references it.
- **Cron** (`reindex_algolia`): ops/config change outside this repo. Remove the schedule. Keep
  the `reindex_algolia` command and `index_enterprise_catalog_in_algolia_task` intact as the
  documented rollback tool (tech-spec Phase 8f, "re-enable old cron if needed").

## Step 2: move backend reads to v2

Add the `search_index` accessor, then replace `algolia_client.algolia_index` with
`algolia_client.search_index` at the 10 backend read call sites:

- `api/v1/views/catalog_csv.py:66,96` — `.search()`
- `api/v1/views/catalog_csv_data.py:70,99` — `.search()`
- `api/v1/views/catalog_workbook.py:79,134` — `.search()`
- `ai_curation/utils/algolia_utils.py:99,124` — `.search('')`
- `api/v1/views/default_catalog_results.py:96` — `.search('')`
- `api/v1/serializers.py:544` — `.search_for_facet_values('academy_tags', ...)`

Test-mock updates (they mock `.algolia_index.search` today; change to `.search_index.search`):
- `ai_curation/tests/test_utils.py:59`
- `api/v1/tests/test_views.py`
- `api_client/tests/test_algolia.py`

Parity checks before relying on v2 for these reads (validation, not code):
1. `search_for_facet_values('academy_tags', ...)` needs `academy_tags` declared searchable in
   v2's `attributesForFaceting`. Confirm v2 index settings match v1.
2. v2 carries the facets/attributes these readers use: `enterprise_catalog_query_titles`,
   `learning_type`/`learning_type_v2`, `course_type`, `availability`, `academy_uuids`,
   `enterprise_customer_uuids`, `aggregation_key`, and the export attribute set.

## Step 3: retire v1 (ops)

Remove v1 from `ALLOWED_INDEX_NAMES`; delete the v1 index and replica once Steps 1-2 are live
and verified. Legacy reader/writer helpers on the client (`replace_all_objects`, the browse and
delete methods, `get_all_objects_associated_with_aggregation_key`) become dead code, removable
afterward.

## Verification

Accessor unit test (`api_client/tests/test_algolia.py`), both branches:
- `INCREMENTAL_INDEX_NAME` unset → `search_index` returns the primary index (same object as
  `algolia_index`).
- `INCREMENTAL_INDEX_NAME='enterprise_catalog_v2'` → `search_index` calls
  `_client.init_index('enterprise_catalog_v2')` (assert against the mocked client).

```
docker exec -e DJANGO_SETTINGS_MODULE=enterprise_catalog.settings.test enterprise.catalog.app \
  pytest enterprise_catalog/apps/api_client/tests/test_algolia.py -q
```

After Step 1, confirm no v1 write ops in the Algolia dashboard following a refresh-API call and
after the cron window. After Step 2, confirm backend read paths (CSV/workbook export, AI
curation, default catalog results, academy tag facets) return results from v2.
