# v1 Decommission Plan: legacy writes off, backend reads to v2

## Context

The v1→v2 Algolia migration is far along in prod: v2 writes are enabled
(`ENABLE_INCREMENTAL_ALGOLIA_INDEXING` on, `INCREMENTAL_INDEX_NAME` set to the v2 index) and
all frontends already read v2. Two loose ends remained in `enterprise-catalog` before v1 can be
removed from `ALLOWED_INDEX_NAMES` and deleted:

1. **Legacy writes still hit v1.** `index_enterprise_catalog_in_algolia_task` →
   `_reindex_algolia` → `replace_all_objects` wrote the primary (v1) index. It fired from the
   `reindex_algolia` cron and from the refresh API view.
2. **Backend reads still hit v1.** Every server-side Algolia read used
   `algolia_client.algolia_index` (primary = v1), via the admin `API_KEY`, which
   `ALLOWED_INDEX_NAMES` does not gate. These served stale data and hard-break when v1 is deleted.

## What was implemented (PR #146)

### Read routing: update `algolia_index_name`, not call sites

Rather than adding a new `search_index` accessor and updating 10 call sites, we updated the
`algolia_index_name` property in `AlgoliaSearchClient` to resolve to `INCREMENTAL_INDEX_NAME`
when configured, falling back to `INDEX_NAME`:

```python
@property
def algolia_index_name(self):
    return settings.ALGOLIA.get('INCREMENTAL_INDEX_NAME') or settings.ALGOLIA.get('INDEX_NAME')
```

Because `init_index()` uses `self.algolia_index_name` to initialize `self.algolia_index`, the
primary index object now points to v2 in prod. Every call site that uses
`algolia_client.algolia_index` (CSV/workbook export, AI curation, default catalog results,
academy tag facets) routes to v2 without any renaming. Test mocks on `.algolia_index` remain
valid. `_get_index()` also benefits: callers passing `INCREMENTAL_INDEX_NAME` now return the
cached primary object instead of creating a duplicate.

### API trigger and management command: drop the flag entirely

`ENABLE_INCREMENTAL_ALGOLIA_INDEXING` is removed from `settings/base.py`. The flag had no
meaningful off-state: frontends already read v2, and `base.py` had it `False` (prod depended on
an env override to enable it at all).

`EnterpriseCatalogRefreshDataFromDiscovery.post` previously branched on the flag. The `else`
branch (legacy chain with `index_enterprise_catalog_in_algolia_task`) is gone. The view now
always chains:

```python
chain(
    update_catalog_metadata_task.si(catalog_query_id),
    update_full_content_metadata_task.si(),
    dispatch_algolia_indexing_for_catalog_query.si(catalog_query_id),
)
```

`update_content_metadata` (the cron management command) previously guarded
`dispatch_algolia_indexing` behind the same flag. The guard is removed; the dispatch is now
unconditional.

Removed across both files: `index_enterprise_catalog_in_algolia_task`, `group`, `settings`,
and all `ENABLE_INCREMENTAL_ALGOLIA_INDEXING` references.

## Remaining: Step 3 (ops + cleanup)

1. **Cron** (`reindex_algolia`): remove the schedule from ops config. The command and
   `index_enterprise_catalog_in_algolia_task` remain in code as a documented rollback tool
   (tech-spec Phase 8f).
2. **`ALLOWED_INDEX_NAMES`**: remove v1 from the list once cron is confirmed stopped and
   PR #146 is deployed and verified.
3. **Delete v1 index**: delete the v1 index and replica in the Algolia dashboard.
4. **Dead code cleanup**: after v1 is deleted, remove the legacy reader/writer helpers on the
   client (`replace_all_objects`, browse/delete methods,
   `get_all_objects_associated_with_aggregation_key`) and `index_enterprise_catalog_in_algolia_task`.

## Verification

After PR #146 deploys:
- CSV/workbook export, AI curation, default catalog results, and academy tag facets return v2 data.
- No v1 write ops in the Algolia dashboard after a refresh-API call.
- After cron removal: no v1 write ops in the Algolia dashboard across the cron window.

`algolia_index_name` unit tests (both branches) run in `api_client/tests/test_algolia.py`:

```
docker exec -e DJANGO_SETTINGS_MODULE=enterprise_catalog.settings.test enterprise.catalog.app \
  pytest enterprise_catalog/apps/api_client/tests/test_algolia.py -q
```
