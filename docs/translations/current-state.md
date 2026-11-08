# Content Translation

This doc covers how Spanish translations are stored, populated, and consumed in enterprise-catalog.

Aside: the Transifex flow is decommissioned across edX - all references to it in this repo are outdated and should be ignored.

## tl;dr

```
    ContentMetadata (English)
           |
           | [k8s CronJob: populate_spanish_translations]
           | calls Xpert AI per field, writes result
           v
    ContentTranslation (language_code='es')
           |
           | [Algolia reindex task]
           | deep-copies English Algolia object, swaps text fields
           v
    Algolia: course-abc123      (metadata_language='en')
    Algolia: course-abc123-es   (metadata_language='es')
```
A Spanish translation cron job runs on an independent schedules, with no coordination w.r.t. reindexing.
There's no status tracking -- no way to tell from the outside whether a translation row is fresh, stale, or failed.

## Model

`ContentTranslation` (`catalog/models.py`) is a simple DB-backed cache of translated text fields, keyed by
`(content_metadata, language_code)`. One row per content item per language. Fields:

- `title`, `short_description`, `full_description`, `subtitle`, `outcome`, `prerequisites`
- `source_hash` -- SHA256 of the English source fields, used to detect staleness
- Unique constraint on `(content_metadata, language_code)`

Only `es` is in use. `AVAILABLE_TRANSLATION_LANGUAGES = ['es']` in `api/v1/constants.py`.

## How records are populated

`populate_spanish_translations` management command (one-shot, no Celery trigger on save):

1. Queries `ContentMetadata` for courses and programs eligible for Algolia indexing.
2. Computes a SHA256 hash of the English source fields.
3. Skips rows where a `ContentTranslation(language_code='es')` already exists with a matching hash,
   unless `--force` is passed.
4. Calls `translate_object_fields()` in `catalog/translation_utils.py`, which sends the fields to
   Xpert AI via `api_client/xpert_ai.py`.
5. Writes the result to a `ContentTranslation` row.

Useful flags: `--missing-only`, `--content-keys`, `--dry-run`, `--batch-size`, `--all`.

The command runs on a scheduled Kubernetes CronJob, defined in a separate infrastructure config
repo (not this service). Nothing triggers re-translation automatically when `ContentMetadata`
changes mid-cycle. If source content drifts, the hash check will catch it on the next scheduled
run.

## How records are consumed

### Algolia indexing

`create_spanish_algolia_object()` in `catalog/algolia_utils.py` is called inside
`add_metadata_to_algolia_objects()` (and the video variant) in `api/tasks.py`.

For each content item that has a `ContentTranslation(language_code='es')`, it:

1. Deep-copies the English Algolia object.
2. Overwrites `title`, `short_description`, `full_description`, `subtitle` with translated values.
3. Sets `objectID` to `{original_id}-es` (e.g., `course-abc123-es`).
4. Sets `metadata_language = 'es'`.

The result is a second, separate Algolia record. Spanish and English live as sibling records with
the same enterprise catalog/customer UUIDs but different `objectID` and `metadata_language`.
Videos are not supported yet and return `None` early.

### Highlights API

`HighlightSetViewSet` passes `?lang=es` from the query param into serializer context.
`HighlightedContentSerializer.get_title()` checks context for a pre-fetched `ContentTranslation`
and returns the translated title if one exists, falling back to English otherwise.

## Where translations live in Algolia

Spanish content is a **separate Algolia record**, not a field on the English record.

```
objectID: course-abc123        metadata_language: en
objectID: course-abc123-es     metadata_language: es
```

Both records carry the same `enterprise_catalog_uuids` and `enterprise_customer_uuids`. The frontend
filters by `metadata_language` to show one or the other.

## Pros and cons

**Pros:**
- Simple. The model is a plain DB table -- no external dependency for read path.
- Source hash provides cheap change detection without re-translating on every sync.
- Algolia's separate-record approach makes language filtering trivial on the frontend.

**Cons:**
- Population is driven by a k8s CronJob running `populate_spanish_translations`, not wired into
  the content sync pipeline. Translations lag behind content changes between cron runs, with no
  visibility into that lag.
- `outcome` and `prerequisites` are in the model schema but the management command doesn't
  translate them -- silent gap with no warning.
- `create_spanish_algolia_object()` is hardcoded to `'es'` by name and logic. Adding a second
  language requires a real refactor of the Algolia indexing path, not just a config change.
- The Highlights API serializer and the Algolia indexing path are separate implementations of
  "give me the translated version" with no shared abstraction -- a second language means two
  places to update.
- There is no status tracking on `ContentTranslation` rows. There is no way to tell whether a
  row failed translation, is stale, or is pending a first run -- everything looks the same from
  the outside.
- The k8s CronJob and the Algolia reindex are scheduled independently with no coordination.
  Content can be reindexed before its translation is current, or a translation can sit as
  `needs_reindex` indefinitely with no signal back to the indexing pipeline.
- The separate-record Algolia approach means index size scales linearly with language count. At
  N languages, every content item becomes N Algolia records, each carrying a full copy of the
  catalog membership facets (`enterprise_catalog_uuids`, `enterprise_customer_uuids`, etc.).

## Suitability for N languages

The current approach is an MVP built for one language. Adding a second language would
require coordinated changes across the management command, `algolia_utils.py`, the Highlights
serializer, and the k8s CronJob config -- none of which share a language configuration source.
There is also no mechanism to track translation status per language, so operational visibility
(how many records are stale? which failed?) would require ad-hoc DB queries.

For a handful of languages, this architecture needs to be replaced rather than extended. See
`tech-spec-translation-pipeline.md` in this directory for the planned approach.
