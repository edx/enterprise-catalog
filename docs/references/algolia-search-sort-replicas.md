# Algolia search sort replicas ("newest courses first")

How sort order is implemented for the Learner Portal search page, and how the
"newest courses first" sort is wired end-to-end.

## How sorting works in Algolia here

The enterprise catalog is indexed into a **primary** Algolia index whose name comes
from `settings.ALGOLIA['INDEX_NAME']`. Algolia does not re-sort a single index at query
time; instead each sort order is a **replica** index with its own `customRanking`. The
Learner Portal MFE switches sort by pointing its search at a different index name.

Replicas are declared on the primary index's settings (`ALGOLIA_INDEX_SETTINGS['replicas']`)
and each replica's ranking is set with its own settings call. All of this lives in
[`enterprise_catalog/apps/catalog/algolia_utils.py`](../../enterprise_catalog/apps/catalog/algolia_utils.py)
and is applied by `configure_algolia_index()` during a full reindex.

| Index | Setting key (`settings.ALGOLIA[...]`) | Leading `customRanking` | Used by |
|-------|----------------------------------------|--------------------------|---------|
| primary (relevance) | `INDEX_NAME` | text relevance + `desc(course_bayesian_average)` | default search |
| duration replica | `REPLICA_INDEX_NAME` | `desc(duration)` | video search |
| recently-published replica | `RECENTLY_PUBLISHED_REPLICA_INDEX_NAME` | `desc(recently_published_timestamp)` | "newest courses first" |

## "Newest courses first"

- **What "newest" means:** a course's *earliest published course-run start* — i.e. when the
  course first became available. This is the same signal as the `is_new_content` flag
  (ENT-11384); both are derived from `_earliest_published_course_run_start()`, which ignores
  unpublished/draft runs so backfilled drafts can't change a course's recency.
- **The sort attribute:** `recently_published_timestamp` — a Unix timestamp (int) computed by
  `get_course_recently_published_timestamp()` and added to each course Algolia object.
  Courses with no published run start get `0` so they sort **last** under the `desc` ranking.
  > Do not use `ALGOLIA_DEFAULT_TIMESTAMP` for the "missing" case — it is a far-future
  > sentinel (year 3000) and would float undated courses to the *top* of a newest-first sort.
- **The replica:** `ALGOLIA_RECENTLY_PUBLISHED_REPLICA_INDEX_SETTINGS` leads with
  `desc(recently_published_timestamp)` and then keeps the primary index's tie-breakers.

## Deployment / ops dependency

`ALGOLIA` is **not** in `DICT_UPDATE_KEYS` in
[`settings/production.py`](../../enterprise_catalog/settings/production.py), so the deployment
YAML *replaces* the entire `ALGOLIA` dict rather than merging into the `base.py` default.
Consequences:

1. The `base.py` default (`'RECENTLY_PUBLISHED_REPLICA_INDEX_NAME': ''`) only applies in
   local/test. **Enabling the replica in stage/prod requires ops to add
   `RECENTLY_PUBLISHED_REPLICA_INDEX_NAME` to the `ALGOLIA` block in `edx-internal`.**
2. The code guards on the name being set: the replica is only declared on the primary index
   and only configured when a name is present, so deploying this code *before* ops adds the
   name is a safe no-op (no `virtual(None)` replica is created).
3. After the name is configured, a **full reindex** (`reindex_algolia`) must run so the
   replica is created and `recently_published_timestamp` is populated on every object.

The replica index name is also added to the secured-API-key `restrictIndices` in
[`api_client/algolia.py`](../../enterprise_catalog/apps/api_client/algolia.py) so the MFE's
scoped search key is permitted to query it.

## End-to-end rollout (cross-repo)

This service only produces the sorted replica. The user-facing sort is gated and measured in
the other two repos:

- **`edx-enterprise`** — a `search_default_sort_newest` waffle flag surfaced via
  `enterprise_features`, used as the eligibility gate / kill-switch.
- **`frontend-app-learner-portal-enterprise`** — points the course `<Index>` at the
  recently-published replica when the flag is on **and** the Optimizely Web experiment buckets
  the user into the "newest" variant; control keeps the relevance index.
