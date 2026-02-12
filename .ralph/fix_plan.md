# Ralph Fix Plan

## High Priority
- [ ] Implement Phase 0 improvements (see architecture-analysis.md §7):
  - [ ] Add `jsonschema` validation on ingested content metadata
  - [x] Add `is_discoverable` facet to Algolia indexing
  - [ ] Improve Django admin/management command observability for catalog-content associations
- [ ] Implement Phase 1 app separation (structural refactor):
  - [ ] Extract `content_replication` app from `catalog`
  - [ ] Extract `catalog_inclusion` app from `catalog`
  - [ ] Extract `search_indexing` app from `catalog`
- [ ] Get team decisions on Decision 2 (inclusion backend) and Decision 3 (Algolia facets) before proceeding further

## Medium Priority
- [ ] Phase 2: Content replication hardening (schema contracts, error isolation, new API endpoint)
- [ ] Phase 3: Catalog-content inclusion backend upgrade (pending Decision 2)

## Low Priority
- [ ] Phase 4: Async event consumer (blocked on openedx-events content metadata events)
- [ ] Phase 5: Search indexing simplification (pending Decision 3)

## Completed
- [x] Project enabled for Ralph
- [x] Review codebase and understand architecture
- [x] Review .ralph/specs/rearchitecture-ideation.pdf
- [x] Document proposed approaches and strengths/weaknesses → see .ralph/architecture-analysis.md
- [x] Articulate important decision and feedback points → see .ralph/architecture-analysis.md §6
- [x] Develop implementation plan for moving toward future architecture → see .ralph/architecture-analysis.md §7 & §8
- [x] Add `is_discoverable` facet to Algolia indexing
  - Added `is_discoverable` to `ALGOLIA_FIELDS` and `attributesForFaceting` (as `filterOnly`) in `algolia_utils.py`
  - All indexed content receives `is_discoverable=True` via `add_metadata_to_algolia_objects` and `add_video_to_algolia_objects`
  - Non-indexable content is now also indexed with `is_discoverable=False` (previously discarded), improving index observability
  - `_index_content_keys_in_algolia` accepts `nonindexable_content_keys` param and indexes both types in one atomic replace
  - `add_metadata_to_algolia_objects` accepts `is_discoverable` param (default True) for explicit control
  - `_get_algolia_products_for_batch` accepts `is_discoverable` param and propagates it through
  - Tests added: `test_add_metadata_to_algolia_objects_sets_is_discoverable` (True default),
    `test_add_metadata_to_algolia_objects_sets_is_discoverable_false` (explicit False),
    `test_index_content_keys_in_algolia_with_nonindexable` (integration: nonindexable indexed with False)

## Notes
- Full analysis written to `.ralph/architecture-analysis.md`
- Phase 0 items are safe to start immediately without team decisions
- Phase 1 (app separation) is a large refactor but preserves all behavior
- Decision 2 (inclusion backend) is the most critical architectural decision — recommend Algolia as logic engine
- Decision 3 (dropping facets) has the highest simplification potential but requires frontend migration coordination
- Real-time incremental sync via naive Python filtering is NOT feasible at current scale (35 filter ops/sec)
