# Algolia Incremental Reindexing

Status: **Complete.** Shipped through Phase 8f as of 2026-07-20. The monolithic
`replace_all_objects` reindex and the v1 index it wrote are gone; all indexing runs through the
incremental pipeline in `enterprise_catalog.apps.search`.

## What this directory is

The design and migration record for replacing a 2+ hour single-threaded Algolia reindex with
per-record incremental indexing. It is history, not documentation of how the system works
today.

**For how content sync and indexing work now, read
[`docs/references/content-sync-and-algolia-indexing.md`](../references/content-sync-and-algolia-indexing.md).**

## What shipped

A `search` Django app holding a `ContentMetadataIndexingState` row per `ContentMetadata`, two
dispatchers, and per-content-type batch tasks. Records are dispatched when
`ContentMetadata.modified` outruns `last_indexed_at`, when a child was indexed more recently
than its parent, or when a prior attempt failed. Batches of 10 fan out across Celery workers,
upsert Algolia objects, and delete orphaned shards.

Three triggers: the daily `update_content_metadata` chain, a stragglers cron running
`incremental_reindex_algolia`, and the per-catalog `refresh_metadata` API,
which additionally diffs database membership against the index so catalog additions and
removals reindex even when nothing about the content changed.

Cutover ran through a dual-index window. Both indices were writable and readable at once, the
frontend chose between them with a feature flag, and v1 stayed fresh until the flag flipped,
which made rollback a flag toggle rather than a deploy.

## Files

| File | What it is |
|---|---|
| `pitch.md` | Business case |
| `tech-spec.md` | Full design, all 9 build phases, execution findings recorded as each phase landed |
| `v2-index-validation-status.md` | Parity validation against the old index: facet counts, field-level spot checks, ranking sanity checks, and the resolution of every diff found |
| `videos.md` | How video records reach the index |
| `algolia-frontend-architecture.md` | How the frontends query Algolia |
| `plans/` | Implementation plans written for AI coding agents during the build. Historical artifacts, not sources of truth |

## Decisions extracted from this work

- [ADR 0011](../decisions/0011-search-app-for-incremental-algolia-indexing.rst): why a separate `search` app
- [ADR 0012](../decisions/0012-incremental-indexing-state-authority-and-outcome-contract.rst): the state row is authority for shard ownership; what an Algolia "save" guarantees
- [ADR 0013](../decisions/0013-dispatcher-serial-chain-ordering.rst): why the dispatcher sequences content types with nested chords
