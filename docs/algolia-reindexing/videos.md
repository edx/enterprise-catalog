# Video Indexing in the Incremental Reindex Pipeline

## Background

Videos in Algolia come from the `Video` model (`video_catalog` app), not from `ContentMetadata`.
The monolithic reindex handles them inside `_get_algolia_products_for_batch` via
`add_video_to_algolia_objects`. The incremental pipeline (`dispatch_algolia_indexing` and its
batch tasks) was built for `ContentMetadata`-backed content types only, which produced a ~36k
record gap in the v2 index during Phase 7 validation.

This document describes the design for adding video support to the incremental pipeline.

## Data Model

```
Video
  └─ parent_content_metadata (FK → ContentMetadata, course-run type)
       └─ parent_content_key → content_key of the parent course ContentMetadata
                                    └─ ContentMetadataIndexingState.last_indexed_at
```

Production counts (as of Phase 7 validation):

| Entity | Count |
|--------|-------|
| `Video` rows | 893 |
| `VideoSkill` rows | ~5,000 |
| Algolia records (including enterprise-customer and Spanish duplicates) | ~36,500 |

## Staleness Semantics

A video is treated as stale when its **parent course** is stale. There is no separate
`ContentMetadataIndexingState` row for `Video` records — the parent course's state acts as
the proxy.

- `force=True`: all videos are dispatched.
- `force=False`: only videos whose `parent_content_metadata__parent_content_key` appears in
  the stale-course set (as computed by `_get_course_keys_for_dispatch`) are dispatched.

After a video batch task runs, **no state row is written**. The parent course's state row
(updated by `index_courses_batch_in_algolia` when the course itself is indexed in the same
dispatcher pass) covers it.

**Edge case**: a `--content-type video`-only run leaves the parent course's
`ContentMetadataIndexingState` unchanged, so those videos will appear stale again on the next
run and be re-indexed redundantly. This is acceptable at the current scale (893 videos).

## Dispatch Ordering

Videos slot as a fourth group at the end of the existing chain:

```
courses (parallel group)
    ↓  [Celery chain barrier — all course tasks complete]
programs (parallel group)
    ↓
pathways (parallel group)
    ↓
videos (parallel group)
```

Videos have no ordering dependency on programs or pathways (their catalog membership is
derived from DB joins at task execution time, not from Algolia state). The barrier before
the video group costs nothing and keeps the chain structure uniform.

## Batch Size

**20 video PKs per task** (module-level constant `VIDEO_BATCH_SIZE = 20`). Not pulled from
settings — the fixed small scale doesn't justify a configurable knob.

~893 videos ÷ 20 = ~45 tasks per full-force run.

## Implementation Plan

### `enterprise_catalog/apps/search/tasks.py`

**New imports**

```python
from enterprise_catalog.apps.catalog.constants import VIDEO
from enterprise_catalog.apps.video_catalog.models import Video
from enterprise_catalog.apps.api.tasks import add_video_to_algolia_objects
```

**`_SUPPORTED_CONTENT_TYPES`** — add `VIDEO`.

**New constant**

```python
VIDEO_BATCH_SIZE = 20
```

**New helper: `_get_video_pks_for_dispatch(stale_course_keys, force) -> list[str]`**

Takes the already-computed stale course key list from `_get_keys_to_dispatch_by_type` rather
than re-deriving it. This avoids duplicating the staleness DB query and keeps `include_failed`
logic in one place.

```python
def _get_video_pks_for_dispatch(stale_course_keys: list[str], force: bool) -> list[str]:
    if force:
        return list(Video.objects.values_list('edx_video_id', flat=True))
    return list(
        Video.objects.filter(
            parent_content_metadata__parent_content_key__in=stale_course_keys
        ).values_list('edx_video_id', flat=True)
    )
```

**`_get_keys_to_dispatch_by_type`** — compute courses first, then pass the result into the
video helper:

```python
dispatched_course_keys = _get_course_keys_for_dispatch(...)
...
VIDEO: _get_video_pks_for_dispatch(
    stale_course_keys=dispatched_course_keys,
    force=force,
),
```

**`_build_ordered_groups`** — append a video group after pathways. The video group uses
`video_pks` instead of `content_keys` to reflect the different primary key type.

**New helper: `_get_catalog_membership_by_key(course_keys) -> tuple[dict, dict, dict]`**

Extracted from the monolithic `_get_algolia_products_for_batch` pattern. Returns
`(customer_uuids_by_key, catalog_uuids_by_key, catalog_queries_by_key)` for the given
course content keys. Used by `index_videos_batch_in_algolia`; may be useful for other
future non-`ContentMetadata` content types.

**New task: `index_videos_batch_in_algolia(self, video_pks, index_name=None)`**

Does **not** use `_index_content_batch` (which is `ContentMetadata`-only and manages
`ContentMetadataIndexingState`). Simpler flow:

1. `Video.objects.filter(edx_video_id__in=video_pks).select_related('parent_content_metadata')`
2. Collect unique parent course keys via `video.parent_content_metadata.parent_content_key`
3. Call `_get_catalog_membership_by_key(parent_course_keys)` to get the three membership dicts
4. For each video, call `add_video_to_algolia_objects(video, customer_uuids, catalog_uuids, catalog_queries)` to produce Algolia objects
5. `algolia_client.save_objects_batch(all_objects, index_name=index_name)`
6. Log and return a plain dict summary — no `ContentMetadataIndexingState` writes

**`dispatch_algolia_indexing`** — no structural changes; the `_get_keys_to_dispatch_by_type`
and `_build_ordered_groups` updates above wire it in. The `dispatched_summary` dict gains a
`video` key.

**`dispatch_algolia_indexing_for_catalog_query`** — after the existing course/program/pathway
fan-out, dispatch video tasks for videos whose parent course is among the catalog query's
course membership. Video tasks are not chained with courses here (no ordering dependency).

### `enterprise_catalog/apps/search/management/commands/incremental_reindex_algolia.py`

- Add `VIDEO` to `_ALL_CONTENT_TYPES`
- Add `VIDEO` to `--content-type` choices

### Tests

**`search/tests/test_tasks.py`**

- `TestIndexVideoBatchInAlgolia`
  - Happy path: videos loaded, catalog membership queried, `save_objects_batch` called with correct objects
  - Empty batch: no Algolia calls made
  - Unknown video PKs: handled gracefully (empty queryset)

- `TestGetVideoPksForDispatch`
  - `force=True` returns all video PKs regardless of parent course staleness
  - `force=False` returns only videos whose parent course is stale
  - `force=False` with no stale courses returns an empty list

- `TestDispatchAlgoliaIndexing` — existing suite extended:
  - `VIDEO` appears in `dispatched_summary`
  - Video tasks appear as a fourth group after pathways in the dispatched chain

- `TestDispatchAlgoliaIndexingForCatalogQuery` — existing suite extended:
  - Video tasks are dispatched when the catalog query has matching courses

**`search/management/commands/tests/test_incremental_reindex_algolia.py`**

- `VIDEO` accepted as a `--content-type` value
- `VIDEO` appears in dispatch summary output

## Out of Scope

- `ContentMetadataIndexingState` rows for `Video` records (can be added later if
  video-specific staleness tracking — e.g. responding to `fetch_video_skills` runs — is needed)
- Video deletion from Algolia when a `Video` row is removed (follows the existing
  content-removal pattern; deferred to a follow-up)
