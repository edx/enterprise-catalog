Search App for Incremental Algolia Indexing
===========================================

Status
------

Accepted

Context
-------

The monolithic ``reindex_algolia`` job (``replace_all_objects``) takes 2+ hours
in production and grows with the catalog. Any failure restarts from zero, and a
single changed course triggers a full rebuild. We're moving to an incremental,
per-record indexing system — a dispatcher that fans out small batch tasks to
Celery workers, plus a tracking model so we can detect stale records and retry
failures independently.

See ``docs/algolia-reindexing/tech-spec.md`` for the full design.

Decision
--------

Put the new indexing code in a dedicated Django app at
``enterprise_catalog.apps.search`` rather than extending the existing ``catalog``
or ``api`` apps. The first artifact in the app is the
``ContentMetadataIndexingState`` model, which holds per-``ContentMetadata``
indexing state (``last_indexed_at``, ``last_failure_at``,
``removed_from_index_at``, ``algolia_object_ids``, ``failure_reason``).
Subsequent phases will add the dispatcher tasks, batch tasks, Algolia client
batch methods, and management command to this same app.

Why a new app:

* **Isolation during rollout.** The legacy ``reindex_algolia`` flow stays
  untouched while the new system is built behind a feature flag. A separate app
  keeps the two code paths from tangling and makes the eventual legacy removal
  a directory-scoped delete.
* **Sub-domain boundary.** Search indexing is a distinct concern from catalog
  modeling and the REST API. ``catalog`` already carries a lot — adding the
  indexing state model, dispatcher, batch tasks, and a management command would
  bloat it further and blur responsibilities.
* **Migration scoping.** ``ContentMetadataIndexingState`` and any future
  indexing-related schema changes live in their own migration history,
  independent of the frequently-churned ``catalog`` migrations.

Consequences
------------

* One more app to wire into ``INSTALLED_APPS`` and keep in mind when reading
  the codebase.
* ``ContentMetadataIndexingState`` has a ``OneToOne`` to
  ``catalog.ContentMetadata`` (cross-app FK). This is already a common pattern
  here (``video_catalog``, ``curation``, ``ai_curation`` all reach into
  ``catalog``), so it doesn't introduce a new shape.
* No history on ``ContentMetadataIndexingState``: the row is rewritten on every
  successful index, so ``simple_history`` would produce a lot of noise for
  little debugging value. ``failure_reason`` holds the most recent failure
  only; if we need trend data later, we'll send it to an external system
  rather than grow the table.
