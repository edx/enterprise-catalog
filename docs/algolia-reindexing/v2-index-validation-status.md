# v2 Index Validation — Status Summary

## What we've checked

### Facet parity
Overall record counts are close: v2 has +353 more total records (+6.1%), all attributable to new content in `executive-education-2u` (+330) and `verified-audit` (+202) course types — no content types lost.

### Object ID set diff (updated after force-all reindex)
After deploying the video fix and running a force-all reindex, the diff widened to 218 v1-only and 240 v2-only shards (shard-level counts, not course-level). Investigation confirmed this is entirely catalog churn and new content:

- Several courses (`WOBI+WOBI006`, `MITx+2.854.1x`) appear in **both** v1-only and v2-only with different shard IDs, proving their catalog/customer UUID assignments changed — old shards are stale in v1, v2 has the current assignments.
- v2-only new courses include recent Microsoft AI courses, `ZHAWx+PHRS1x`, `StanfordOnline+SOM.Y0016`, and two new programs added after v1's last full reindex.
- No content is silently dropped. Verdict: **catalog churn + new content, not a bug**.

### level_type drift
2,022 records (0.54%) have different `level_type` between v1 and v2, all programs, all consistent across shards of the same program. Verdict: **data freshness gap, not a bug** — v2 reflects newer Discovery data. v2 is more correct for these records.

### Video field gap — fixed
All sampled video records in v2 were missing `org`, `partners`, `logo_image_urls`, `image_url`, `course_run_key`, `transcript_summary`, `video_skills`, `duration`. Root cause: `index_videos_batch_in_algolia` in the incremental path was not calling `create_algolia_objects`, skipping the DB-enrichment step that the monolithic path performs inside `_get_algolia_products_for_batch`. Fixed in [PR #127](https://github.com/edx/enterprise-catalog/pull/127).

### Field-level spot check — 50 records (pre-fix), 250 records (post-fix)

**Post-fix (250 records, 25 videos): all 25 video records matched cleanly.**

18 fields with diffs, all falling into three categories:

| Category | Fields | Verdict |
|---|---|---|
| Array ordering | `academy_uuids` (87.6%), `subjects` (40%), `skill_names`* (56.4%), `enterprise_catalog_uuids` (36%), `enterprise_customer_uuids` (25.6%), `availability` (4.4%), `program_titles` (5.6%), `programs` (0.8%), `course_keys` (10%) | Noise — Algolia search is order-insensitive |
| Live-updating values | `recent_enrollment_count` (37.2%), `course_bayesian_average` (29.6%), `entitlements` (0.4% — one price change $3,250→$3,247) | Expected drift between a full reindex and a live incremental index |
| Data freshness from Discovery | `academy_tags` (6.4%), `translation_languages` (2.8%), `original_image_url` (0.8%), `partners` (0.8%), `video_ids` (0.4% — videos replaced on one course), `level_type` (0.4%) | v2 is more current than v1 in every case |

\* `skill_names` diffs include actual content changes (different skills, not just reordering) — Discovery updated skills on these courses between v1's full reindex and now. Expected.

### Search ranking sanity check

Ran 9 representative queries (`ai`, `law`, `python`, `project management`, `sql`, `cybersecurity`, `introduction to project management`, `inteligencia artificial`, `instructional design`) against both indices, comparing top-10 results.

8/9 queries: perfect or near-perfect parity (5/5 top-5 overlap, 9–10/10 top-10 overlap).

One flagged query: **`ai`** — 1/5 top-5 overlap, 1/10 top-10 overlap. v1 returns courses ("Unlocking the Power of…" series, "AI Foundations for Business Leaders"). v2 returns video records ("Understanding AI: Key Insights From…", "Mastering AI Fundamentals…", "Unraveling AI…").

Root cause: v2 has newer AI-focused video content added incrementally after v1's last full reindex. Those videos have "AI" prominent in the title (position 1–2), which scores higher on Algolia's textual position criterion (criterion 5) than courses where "AI" appears later in the title. Both indices have identical configuration; `visible_via_association` and `course_bayesian_average` only break ties after textual criteria, so the videos win before those apply. Verdict: **expected behavior given newer content in v2, not a ranking regression**.

Follow-up (not a blocker): if courses-before-videos ranking is a product requirement for short single-word queries, add a `content_type_rank` custom ranking signal to the index config.

## What's left

| Check | Status |
|---|---|
| Re-run spot check after PR #127 deploys | ✅ Done — 25 videos all clean |
| Investigate records only in v1 | ✅ Done — catalog churn + new content, no data loss |
| Search result ranking sanity check | ✅ Done — 8/9 perfect parity; "ai" divergence explained (see above) |
| Array-ordering audit (are ordering diffs benign for all faceted search use cases?) | Not started |

**No structural schema gaps remain.** The only confirmed code bug (video field gap) is fixed. All remaining diffs are data freshness — v2 is consistently more current than v1.
