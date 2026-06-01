Integration Tests
=================

This directory holds integration tests that run against real external services
and therefore cannot be part of the normal pytest suite.

Why a separate directory?
-------------------------

The Django app unit tests live inside each app under
``enterprise_catalog/apps/*/tests/``.  Those tests use mocks, an in-memory
SQLite database, and no network access, so they run in CI on every PR.

The tests here require a live Algolia index and a populated Django database,
which makes them unsuitable for CI.  They live outside the app tree so pytest
does not discover and attempt to run them automatically.

Current suites
--------------

``integration/algolia_reindexing/``
    End-to-end smoke tests for the Phase 4a/4b incremental Algolia indexing
    dispatchers (``dispatch_algolia_indexing`` and
    ``dispatch_algolia_indexing_for_catalog_query``).  Four scenarios cover
    force-indexing, staleness detection, per-catalog dispatch, and membership
    removal.

How to run
----------

Use the ``run_algolia_integration_tests`` management command from inside the
app container.  It needs four environment variables pointing at a sandbox
Algolia index::

    ./manage.py run_algolia_integration_tests \
        --init-index      # only needed the first time on a fresh index

Set the required env vars before invoking::

    ALGOLIA_APP_ID=...
    ALGOLIA_API_KEY=...
    ALGOLIA_INDEX_NAME=...          # must not contain "prod"
    ALGOLIA_REPLICA_INDEX_NAME=...

Options: ``--scenario 1|2|3|4``, ``--no-cleanup``, ``--algolia-wait N``,
``--dry-run``.  Run ``./manage.py run_algolia_integration_tests --help`` for
the full list.
