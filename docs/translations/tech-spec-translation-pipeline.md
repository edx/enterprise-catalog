# Tech Spec: Translation Computation Pipeline

## Status
Authored by Alex Dusenbery circa July 2026.
This doc is currently in review.

## Context

enterprise-catalog currently translates content to Spanish via a manually-run management command
(`populate_spanish_translations`). There is a concrete product requirement to support a handful of
languages. This spec covers only the translation computation
layer -- populating `ContentTranslation` records in a new `translations` Django app. A separate
spec covers reindexing those records into per-language Algolia indices.

The two operations are intentionally decoupled: the translation pipeline writes DB records and
stamps them as needing reindexing; the Algolia pipeline reads those stamps independently.

---

## tl;dr

Here's a quick version of what the proposed future state looks like.
```
    ContentMetadata (English)
           |
           | [bulk sync from course-discovery]
           | computes source_hash, marks stale rows
           v
    translations.ContentTranslation  [status: pending]
           |
           | [cron or similar: dispatch_translation_tasks, every 30 min]
           | claims batch, fires worker tasks
           v
    translations.ContentTranslation  [status: in_progress]
           |
           | [Celery worker: translate_content_batch]
           | one Xpert AI call per item (all fields in one request)
           v
    translations.ContentTranslation  [status: needs_reindex]
           |
           | [Algolia reindex pipeline -- covered by forthcoming spec]
           | writes to per-language index, stamps row current
           v
    Algolia index: enterprise_catalog_en   course-abc123
    Algolia index: enterprise_catalog_es   course-abc123
    Algolia index: enterprise_catalog_fr   course-abc123

```
A new `SupportedLanguage` model drives which languages exist and which fields get translated. 
Adding a language is an admin action, not a code change.

## New Django app: `translations`

Create `enterprise_catalog/apps/translations/` as a self-contained app:

```
translations/
  __init__.py
  apps.py
  admin.py
  constants.py
  models.py       -- SupportedLanguage, ContentTranslation (new)
  signals.py      -- post_save staleness check
  tasks.py        -- translate_content_batch, dispatch_translation_tasks
  utils.py        -- translate_fields_batch()
  migrations/
    0001_initial.py                  -- SupportedLanguage + ContentTranslation schema
    0002_backfill_from_legacy.py     -- data migration from catalog.ContentTranslation
  tests/
    test_models.py
    test_tasks.py
    test_utils.py
```

Add to `INSTALLED_APPS`. The legacy `catalog.ContentTranslation` model stays untouched during this
work; it is deprecated and removed in a follow-up once the new pipeline is proven in production.

---

## Models

### `SupportedLanguage`

```python
class SupportedLanguage(TimeStampedModel):
    language_code       = CharField(max_length=10, unique=True)  # 'es', 'fr', 'pt-br'
    display_name        = CharField(max_length=100)
    is_active           = BooleanField(default=True)
    translatable_fields = JSONField(default=list)
    # e.g. ['title', 'short_description', 'full_description', 'subtitle']
```

Managed via Django admin. No API needed initially. `is_active=False` suspends translation and
reindexing for that language without deleting existing rows.

`translatable_fields` is per-language: different languages can have different field coverage. The
default set is `['title', 'short_description', 'full_description', 'subtitle']`. `outcome` and
`prerequisites` are available in the `ContentTranslation` schema but excluded by default due to
length and quality concerns; they can be added per language via admin.

### `ContentTranslation` (new, in `translations` app)

```python
class ContentTranslation(TimeStampedModel):
    content_metadata   = ForeignKey('catalog.ContentMetadata', on_delete=CASCADE,
                                    related_name='new_translations')
    supported_language = ForeignKey(SupportedLanguage, on_delete=PROTECT,
                                    related_name='translations')
    # translated text fields (all optional)
    title              = CharField(max_length=255, blank=True, null=True)
    short_description  = TextField(blank=True, null=True)
    full_description   = TextField(blank=True, null=True)
    subtitle           = CharField(max_length=255, blank=True, null=True)
    outcome            = TextField(blank=True, null=True)
    prerequisites      = TextField(blank=True, null=True)
    # pipeline state
    source_hash        = CharField(max_length=64, blank=True)
    status             = CharField(max_length=32, choices=STATUS_CHOICES,
                                   default='pending', db_index=True)
    retry_count        = PositiveSmallIntegerField(default=0)
    last_attempted_at  = DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [('content_metadata', 'supported_language')]
        indexes = [models.Index(fields=['status', 'last_attempted_at'])]
```

Status choices: `pending`, `in_progress`, `needs_reindex`, `current`, `failed`.

---

## Status State Machine

```
new content / source hash changed
        |
    [pending]  <---- (source hash changes again while current)
        |  beat task dispatches batch
  [in_progress]
     |                  |
[needs_reindex]       [failed]
        |                  |  (retry window elapsed, retry_count < MAX)
    [current]           [pending]
        ^
  (Algolia pipeline stamps this -- covered in the reindexing spec)
```

---

## What triggers `pending`

### Signal on ContentMetadata save

```python
# translations/signals.py
@receiver(post_save, sender=ContentMetadata)
def check_translation_staleness(sender, instance, **kwargs):
    new_hash = compute_source_hash(instance.json_metadata)
    ContentTranslation.objects.filter(
        content_metadata=instance,
    ).exclude(source_hash=new_hash).update(status='pending')
```

For bulk content syncs that bypass `post_save`, the sync task calls
`mark_stale_translations(content_metadata_qs)` which runs the same hash comparison via bulk
`.update()` -- no N+1 queries.

### New content with no rows yet

`dispatch_translation_tasks` creates missing rows on each tick (see below). Rows are created with
`status='pending'` and picked up on the next dispatch cycle.

---

## Celery Task Design

### `translate_content_batch` (worker task)

Processes a list of `ContentTranslation` IDs that have already been claimed as `in_progress`.

```python
@shared_task(base=LoggedTaskWithRetry, bind=True)
def translate_content_batch(self, content_translation_ids):
    records = ContentTranslation.objects.filter(
        id__in=content_translation_ids,
        status='in_progress',
    ).select_related('content_metadata', 'supported_language')

    to_update = []
    for ct in records:
        try:
            _translate_single(ct)
        except Exception:
            ct.status = 'failed'
            ct.retry_count += 1
            logger.exception("Translation failed for ContentTranslation %s", ct.id)
        to_update.append(ct)

    ContentTranslation.objects.bulk_update(
        to_update,
        ['status', 'source_hash', 'retry_count', 'last_attempted_at',
         'title', 'short_description', 'full_description', 'subtitle',
         'outcome', 'prerequisites', 'modified'],
    )
```

`_translate_single(ct)`:
1. Reads `ct.supported_language.translatable_fields` for field list.
2. Builds `{field: value}` dict from `ct.content_metadata.json_metadata`.
3. Calls `translate_fields_batch(fields_dict, language_code)` -- one Xpert AI call for all fields.
4. Writes translated values, updated `source_hash`, `status='needs_reindex'`, and
   `last_attempted_at=now()` onto the `ct` instance in memory. `bulk_update` persists them.

### `dispatch_translation_tasks` (beat task / orchestrator)

Runs on a configurable schedule (default every 30 minutes).

**Step 1: create missing rows.**
For each active `SupportedLanguage`, find `ContentMetadata` records (courses and programs eligible
for Algolia indexing) with no `ContentTranslation` row for that language. `bulk_create` them with
`status='pending'` and `ignore_conflicts=True` to keep the step idempotent.

**Step 2: re-queue eligible failed records.**
```python
retry_cutoff = now() - timedelta(hours=1)
ContentTranslation.objects.filter(
    status='failed',
    retry_count__lt=MAX_TRANSLATION_RETRIES,
    last_attempted_at__lt=retry_cutoff,
).update(status='pending')
```

**Step 3: dispatch pending batches.**
```python
pending_ids = list(
    ContentTranslation.objects.filter(status='pending')
    .values_list('id', flat=True)[:MAX_DISPATCH_PER_RUN]
)
ContentTranslation.objects.filter(id__in=pending_ids).update(
    status='in_progress', last_attempted_at=now()
)
for batch in chunked(pending_ids, TRANSLATION_TASK_BATCH_SIZE):
    translate_content_batch.delay(batch)
```

`MAX_DISPATCH_PER_RUN` caps how many records a single beat tick claims, preventing runaway
dispatch if a large backlog builds up (e.g., after adding a new language).

---

## Xpert AI: one call per content item

New `translate_fields_batch(fields_dict, language_code)` in `translations/utils.py`:

```python
def translate_fields_batch(fields_dict, language_code):
    """
    Translate multiple fields in a single Xpert AI call.
    Returns a dict with the same keys. Missing keys in the response are omitted -- callers
    should treat missing keys as untranslated rather than raising.
    """
    payload = json.dumps({k: v for k, v in fields_dict.items() if v})
    system_msg = (
        f"You are a professional translator. Translate the JSON values to {language_code}. "
        "Return ONLY valid JSON with the same keys and no commentary."
    )
    raw = chat_completion(system_msg, [{"role": "user", "content": payload}])
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Xpert AI returned non-JSON for language %s", language_code)
        raise ValueError("Non-JSON response from Xpert AI")
```

This replaces the current per-field approach (N_fields calls per item) with one call per item,
reducing API calls by a factor of the field count. `xpert_ai.py` requires no changes.

---

## Data migration from legacy model

`0002_backfill_from_legacy.py`:

1. Get or create `SupportedLanguage(language_code='es', display_name='Spanish', is_active=True,
   translatable_fields=['title', 'short_description', 'full_description', 'subtitle'])`.
2. For each `catalog.ContentTranslation` row with `language_code='es'`, create a corresponding
   `translations.ContentTranslation` row with copied text fields and `status='current'`.
   Existing translations are assumed good; they will transition to `pending` naturally if source
   content changes.
3. Run as a Django data migration. On large tables this may be slow -- add a note in the deploy
   runbook to run it with awareness of migration timeout limits.

---

## Configuration constants

```python
# translations/constants.py
TRANSLATION_TASK_BATCH_SIZE = 50    # ContentTranslation IDs per worker task
MAX_TRANSLATION_RETRIES     = 5     # give up after this many failures
MAX_DISPATCH_PER_RUN        = 5000  # cap on records dispatched per beat tick
```

Beat schedule entry in `settings/base.py`:
```python
'dispatch-translation-tasks': {
    'task': 'enterprise_catalog.apps.translations.tasks.dispatch_translation_tasks',
    'schedule': crontab(minute='*/30'),
},
```

---

## Files created / touched

| File | Change |
|---|---|
| `translations/` (whole app) | new |
| `settings/base.py` | add app to `INSTALLED_APPS`, add beat schedule entry |
| `catalog/management/commands/update_content_metadata.py` | call `mark_stale_translations()` after bulk sync |

Existing files left untouched during this work (legacy path deprecated, not removed):
`catalog/models.py`, `api_client/xpert_ai.py`, `catalog/translation_utils.py`,
`catalog/management/commands/populate_spanish_translations.py`.

---

## Out of scope: Reindexing of supported languages

There will be a separate layer to all this for actually getting translated content into our Algolia records:
- Per-language Algolia index structure
- Refactor/replacement of `create_spanish_algolia_object()` to read from `translations.ContentTranslation`
- How state of translation search index records is persisted and queried

---

## Verification

1. Create `SupportedLanguage(language_code='fr', translatable_fields=['title', 'short_description'])`
   in Django admin.
2. Run `dispatch_translation_tasks` manually; confirm `pending` rows are created for all eligible
   content for 'fr'.
3. Confirm `translate_content_batch` transitions rows to `needs_reindex` and populates translated
   fields.
4. Mutate a `ContentMetadata.json_metadata` field and save; confirm the corresponding row returns
   to `pending`.
5. Mock Xpert AI to raise `ValueError`; confirm the row transitions to `failed` with
   `retry_count=1`.
6. Run tests:
   ```
   docker exec -e DJANGO_SETTINGS_MODULE=enterprise_catalog.settings.test \
     enterprise.catalog.app \
     pytest enterprise_catalog/apps/translations/
   ```


## Disclosure

This plan was created with Claude Code Sonnet 4.6 alongside Alex Dusenbery in plan mode. 

For historical context, here are the series of prompts that got me to this proposed plan. This may be helpful during
review, but we could delete it once review is completed.

- "how are spanish translations currently handled in this repo?"
- "so the transifex flow is completely decomissioned - we no longer run anything through transifex.
  can you write me up a little doc explaining how ContentTranslation is populated and consumed? What is the general approach? Where do translations live in terms of algolia records?
  pros/cons of approach, whether its suitable to extend for more languages?"
- "should we instead have one search index per supported language?"
- "there's a concrete ask to do it for a handful of languages from the product owner. The search result content is one of three main areas we have to translate, the other being the static
  content on the learner and admin portal frontends, and the course content served from the edx platform LMS."
- "the direction I'm actually thinking is to hook a separate translation population layer downstream of the incremental algolia reindexing. that would simplify things in terms of
  the catalog-membership facets on the search records."
- "we should explicitly design things as two de-coupled operations - computing the translations vs. re-indexing their corresponding search indices. I would actually like to think of
  those as two self-contained tech specs. lets stay in plan mode and think about how to efficiently track translation status and execute those translations via the XPert AI API. rough
  outline of what I want is a top-level model called something like "SupportedLanguage" <- FK - ContentTranslation (perhaps in a new model), with some state on that child model that helps
  determine if it *needs* reindexing. We'll also need to consider configuration of which fields are translatable; how to "batch" translation requests of xpert; celery task design; cron
  cadence, if needed;"
- "I moved the doc describing spanish translations to @docs/translations/current-state.md update the doc to indicate that we run the populate command on a k8s cronjob, defined in separate
  config. Also update the pros and cons with the context you have about wanting to support N languages"
