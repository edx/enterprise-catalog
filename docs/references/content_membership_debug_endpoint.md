# Content membership debug endpoint

Staff-only read endpoint for answering "why isn't this course showing up for this customer?"
without opening a Django shell.

```
GET /api/v1/enterprise-customer/<enterprise_uuid>/content-membership-debug/<content_key>/
GET /api/v2/enterprise-customer/<enterprise_uuid>/content-membership-debug/<content_key>/
```

Both versions route the same view and return byte-identical payloads. Unlike the other v2 views,
there is no `include_restricted` flag for v2 to flip: the response already reports the restricted
and unrestricted answers side by side, so a v2 subclass would override nothing. v2 exists so
callers standardized on `/api/v2/` don't have to special-case one path.

Auth: `IsAdminUser` (Django staff/superuser), same as `distinct-catalog-queries`. It is not
scoped to a single enterprise, so any staff user can inspect any customer.

Documented in the OpenAPI schema via drf-spectacular under the `Enterprise Customer` tag, with
the response shape defined by `ContentMembershipDebugResponseSerializer` and friends in
`api/v1/serializers.py`. Those serializers exist for the schema, but the view also renders its
response through them (the same pattern as `secured_algolia_api_key`), so the documented shape
and the actual shape cannot drift.

One consequence worth knowing: `content` always carries the same key set. When the content was
never synced from Discovery, `exists` is `false` and every other field is `null`, rather than the
block collapsing to a single key. Consumers never have to branch on presence.

The block does not repeat the content key, since the lookup is an exact match on the requested
key and the top-level `content_key` already carries it.

Implemented in `enterprise_catalog/apps/api/v1/views/content_membership_debug.py`. It adds no
membership logic of its own: it calls `EnterpriseCatalog.get_matching_content()` twice per
catalog, once with `include_restricted=False` and once with `True`, so its answers always match
what `contains_content_items` (v1 and v2) would return.

## What the payload answers

| Field | Question |
| --- | --- |
| `catalogs[].contains_content`, `catalogs_containing_content` | Which catalogs contain the content |
| `catalogs[].contains_content_including_restricted`, `restricted_only` | How that changes with restricted runs |
| `catalogs[].catalog_query_modified`, `enterprise_catalog_modified` | When membership last changed (stand-ins, see below) |
| `content.modified`, `content.discovery_modified` | When the content itself last changed |
| `algolia.last_indexed_at`, `is_stale`, `algolia_object_ids` | When the content was last indexed |

Under v2 semantics, "does the catalog contain this?" means the `include_restricted=True`
answer, so a v2 consumer reads `contains_content_including_restricted` where a v1 consumer reads
`contains_content`. Both are always present in both versions.

`restricted_only` is the field to look at first. `contains_content: false` with
`contains_content_including_restricted: true` means the content *is* in the catalog and is being
hidden because the run is restricted, which is a different bug from the content not being in the
catalog at all.

## Two things the endpoint cannot tell you, and why

Both are surfaced as explicit fields rather than omitted, so nobody mistakes a proxy for the real
value.

**There is no "membership last changed" field**, because there is no timestamp anywhere for when
a catalog-to-content membership row was written:

- `ContentMetadata.catalog_queries` (`catalog/models.py`) is a plain `ManyToManyField` with no
  through model, so the join table has no `created`/`modified`.
- `ContentMetadata.history` is `HistoricalRecords()` with no `m2m_fields`, so django-simple-history
  does not track the M2M either.
- `associate_content_metadata_with_query()` calls
  `catalog_query.contentmetadata_set.set(metadata_list, clear=True)`, which drops and rewrites
  every row on every sync. Even if the join table had timestamps, they would all read "last sync."

The endpoint returns `catalog_query_modified` and `enterprise_catalog_modified` instead, named
for exactly what they are rather than wrapped in something that implies more. Adding a real
timestamp means an explicit through model *and* rewriting the sync path to diff adds and removes
rather than clearing, which is why it wasn't done here.

**Nothing confirms the indexed payload.** `ContentMetadataIndexingState` (`search/models.py`)
stores `last_indexed_at` and the `algolia_object_ids` shards that were written, but not the
`enterprise_customer_uuids` / `enterprise_catalog_uuids` inside them. The endpoint makes no
outbound Algolia call, so it cannot confirm the customer or catalog was actually present in the
indexed record.

The DB-only substitute is `catalogs[].indexed_after_last_membership_change`
(`last_indexed_at > catalog_query.modified`). When it is `false`, the index predates the last
catalog query change and is simply behind; when it is `true` and the content still isn't
searchable, the problem is upstream of indexing. It is `null` when either timestamp is missing.

If that flag turns out not to localize a bug, `AlgoliaSearchClient.get_object_ids_for_aggregation_key()`
(`api_client/algolia.py`) already supports reading the live index; the intended extension is a
`?check_algolia=true` query param rather than a network call on every request.

## Tests

`enterprise_catalog/apps/api/v1/tests/test_content_membership_debug.py` holds the behaviour
suite. `enterprise_catalog/apps/api/v2/tests/test_content_membership_debug.py` covers only the
v2 route: that it exists, that it is staff-only, and that its payload is byte-identical to v1's
for the same fixtures (including the restricted-run case, where v1 and v2 semantics normally
diverge).

The v2 file deliberately does not re-run the v1 suite by inheriting from it. edx-lint enforces
`test-inherits-tests` (E7603), which forbids a test class inheriting `test_*` methods from
another class, because those tests then run once per subclass. That is also why
`BaseEnterpriseCustomerViewSetTests` in `api/base/tests/` holds only helpers and no test
methods -- if you add shared tests there, CI will fail even though pytest passes locally.

The devstack container's pylint may abort before linting with
`E0015: Unrecognized option found: pii-terms`. That is a version skew between the container's
edx-lint and the checked-in `pylintrc`, not a problem with your code. To lint locally, copy
`pylintrc` with that option removed and pass `--rcfile` to the copy; the checkers themselves
still load.
