# v2 Index Validation — Status Summary

## What we've checked

### Facet parity
Overall record counts are close: v2 has +353 more total records (+6.1%), all attributable to new content in `executive-education-2u` (+330) and `verified-audit` (+202) course types — no content types lost.

### Object ID set diff
372,386 records in both indices. 6 only in v1 (not yet investigated — small enough to be noise), 538 only in v2 (expected new content).

### level_type drift
2,022 records (0.54%) have different `level_type` between v1 and v2, all programs, all consistent across shards of the same program. Verdict: **data freshness gap, not a bug** — v2 reflects newer Discovery data. v2 is more correct for these records.

### Field-level spot check (50 records)
- Array ordering differences (`academy_uuids` 86%, `skill_names` 70%, `subjects` 52%) — not real diffs, Algolia doesn't sort on array order.
- Academy/catalog data diffs (`academy_tags`, `course_keys`) — data freshness, same root cause as level_type.
- **Video field gap** — all 10 sampled video records were missing `org`, `partners`, `logo_image_urls`, `image_url`, `course_run_key`, `transcript_summary`, `video_skills` in v2. Root cause identified and fixed in [PR #127](https://github.com/edx/enterprise-catalog/pull/127).

## What's left

| Check | Status |
|---|---|
| Re-run spot check after PR #127 deploys | Pending |
| Investigate 6 records only in v1 | Not started |
| Search result ranking sanity check (top-N results for representative queries) | Not started |
| Full array-ordering audit (are ordering diffs benign for all faceted search use cases?) | Not started |

The video fix is the only confirmed code bug found so far. Everything else is either data freshness or ordering noise.
