# Catalog query course-count endpoint

Read endpoint returning how many courses a `CatalogQuery` currently contains.

```
GET /api/v1/catalog-queries/<uuid>/course-count/
```

Response:

```json
{"uuid": "<catalog query uuid>", "course_count": 42}
```

Returns 404 when no `CatalogQuery` has that UUID, 401 when unauthenticated, and 405 for any
write method.

Implemented by `CatalogQueryCourseCountView` in `api/v1/views/catalog_query.py`, routed
explicitly in `api/v1/urls.py`.

## Auth

`JwtAuthentication` + `SessionAuthentication`, `IsAuthenticated` only. No enterprise role is
required. This is intentional: the intended callers are other services (e.g. enterprise-access)
authenticating with a service-user JWT that carries no enterprise-admin roles.

That is why the endpoint is a standalone `APIView` and not an `@action` on
`CatalogQueryViewSet`. An action would inherit the viewset's edx-rbac
`PermissionRequiredForListingMixin` and `check_permissions()` logic, which rejects callers
without admin access to an enterprise linked to the query. Keep it separate if you touch it.

The response exposes only `uuid` and `course_count`, so the relaxed auth does not leak the
query's `content_filter` or title.

## What is counted

`catalog_query.contentmetadata_set.filter(content_type=COURSE).count()`: only `COURSE` rows
associated with the query. Course runs, programs and learner pathways are excluded. The
association is populated by the `update_content_metadata` sync, so the count reflects the last
sync, not a live Discovery search (see `content-sync-and-algolia-indexing.md`).

## History

`course_count` (along with `created`/`modified`) used to be a field on `CatalogQuerySerializer`,
added in #178. It was moved to this endpoint because the viewset's admin-only checks blocked
the service callers that needed it, and a `SerializerMethodField` cost an extra COUNT query for
every serialized `CatalogQuery`. As a result, `GET /api/v1/catalog-queries/<uuid>/` and the other
`CatalogQueryViewSet` responses no longer include `course_count`, `created` or `modified`.
