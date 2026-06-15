Add a new Algolia sort replica
==============================

The Learner Portal search page sorts a single Algolia index by relevance. Algolia does
not re-sort an index at query time, so every alternate sort order is a separate *replica*
index with its own ``customRanking``; the consumer (the MFE) switches sort by pointing its
search at a different index name.

All replicas are driven by **one registry**, so adding a sort is mostly additive. This guide
walks through it end to end. See ``docs/decisions/0014-newest-courses-sort-replica.rst`` for
the design rationale, and treat the **recently-published ("newest first") replica** as the
canonical example to copy.

The mental model: names vs. definitions
---------------------------------------

There are two layers, and the split matters:

* **Index *names* live in Django settings** (``settings.ALGOLIA``), populated per-environment
  from the deployment config (edx-internal). These vary by environment and are owned by ops.
* **Replica *definitions* live in code** — which replicas exist (the registry) and how each is
  ranked (``customRanking``). A replica's ranking sorts on a field that the indexing code must
  compute, so the definition is intrinsically code, not config.

A replica is declared, configured, and made queryable **only when its index-name key holds a
non-empty value**. With the name unset (the default everywhere until ops sets it), the new
replica is completely inert — nothing is declared, no ``virtual(None)`` is created, and the
secured API key does not reference it. This is what makes it safe to merge and deploy the code
*before* ops turns it on.

Worked example
--------------

Suppose we want a **"price: low to high"** sort. We'll use the settings key
``PRICE_ASC_REPLICA_INDEX_NAME`` and (in a given environment) an index named
``enterprise_catalog_price_asc``.

Backend steps (this service)
----------------------------

**1. Declare the settings key.** In ``enterprise_catalog/settings/base.py``, add the key to the
``ALGOLIA`` dict with an empty default and a one-line comment describing the sort:

.. code-block:: python

    ALGOLIA = {
        'INDEX_NAME': '',
        'REPLICA_INDEX_NAME': '',                       # base replica, desc(duration); MFE video search
        'RECENTLY_PUBLISHED_REPLICA_INDEX_NAME': '',     # "newest first", desc(recently_published_timestamp)
        # "price: low to high", asc(first_enrollable_paid_seat_price)
        'PRICE_ASC_REPLICA_INDEX_NAME': '',
        'APPLICATION_ID': '',
        'API_KEY': '',
    }

The empty default keeps it inert until ops provides a real index name.

**2. Register the key.** Add it to ``ALGOLIA_REPLICA_CONFIG_KEYS`` in
``enterprise_catalog/apps/api_client/constants.py`` — the single source of truth for *which*
replicas exist, shared by the indexer and the secured-key restriction. (It lives here, not in
``settings``/``algolia_utils``, so both ``api_client.algolia`` and ``catalog.algolia_utils`` can
import it without a circular dependency.)

.. code-block:: python

    ALGOLIA_REPLICA_CONFIG_KEYS = (
        'REPLICA_INDEX_NAME',
        'RECENTLY_PUBLISHED_REPLICA_INDEX_NAME',
        'PRICE_ASC_REPLICA_INDEX_NAME',
    )

**3. Make sure the field you sort on is indexed.** A ``customRanking`` can only sort on a numeric
attribute that exists on the records. If your sort uses a field that is *already* indexed (the
price example reuses ``first_enrollable_paid_seat_price``), skip this step. If it needs a new
signal, in ``enterprise_catalog/apps/catalog/algolia_utils.py``:

* write a ``get_course_<signal>(course)`` helper that returns the numeric value (see
  ``get_course_recently_published_timestamp`` — note it returns ``0`` for "no value" so those
  records sort last under a ``desc`` ranking; pick a sentinel that sorts your missing values to
  the *end* of *your* order);
* add the field name to ``ALGOLIA_FIELDS``;
* set it on the course object in ``_algolia_object_from_product`` (the ``content_type == COURSE``
  branch).

**4. Define the replica's ranking.** Still in ``algolia_utils.py``, add a settings dict. Lead with
your sort criterion, then append the primary index's shared tie-breakers so records that tie on
your criterion (and any "missing value" bucket) fall back to the relevance ordering and
pagination stays deterministic:

.. code-block:: python

    ALGOLIA_PRICE_ASC_REPLICA_INDEX_SETTINGS = {
        'customRanking': [
            'asc(first_enrollable_paid_seat_price)',
            # shared tie-breakers (same as the primary index) -- keep ties stable
            'asc(metadata_language)',
            'asc(visible_via_association)',
            'asc(created)',
            'desc(course_bayesian_average)',
            'desc(recent_enrollment_count)',
        ],
    }

**5. Map the key to its settings.** Add an entry to
``ALGOLIA_REPLICA_INDEX_SETTINGS_BY_CONFIG_KEY`` in ``algolia_utils.py``:

.. code-block:: python

    ALGOLIA_REPLICA_INDEX_SETTINGS_BY_CONFIG_KEY = {
        'REPLICA_INDEX_NAME': ALGOLIA_REPLICA_INDEX_SETTINGS,
        'RECENTLY_PUBLISHED_REPLICA_INDEX_NAME': ALGOLIA_RECENTLY_PUBLISHED_REPLICA_INDEX_SETTINGS,
        'PRICE_ASC_REPLICA_INDEX_NAME': ALGOLIA_PRICE_ASC_REPLICA_INDEX_SETTINGS,
    }

**That is all the wiring.** You do **not** touch ``_build_algolia_replicas``,
``_configured_replicas``, ``configure_algolia_index``, or the secured-key
``replica_index_names`` — they all loop the registry, so the new replica is automatically
declared on the primary index, has its settings applied during a reindex, and is added to the
secured API key's ``restrictIndices`` once its name is configured.

Tests
-----

* Extend the registry / configure tests in
  ``enterprise_catalog/apps/catalog/tests/test_algolia_utils.py`` (e.g.
  ``test_build_algolia_replicas_only_includes_configured_replicas`` and
  ``test_configure_algolia_index_configures_*``) to cover the new key, using
  ``override_settings(ALGOLIA={...})``.
* If you added a field computation, unit-test the ``get_course_<signal>`` helper.
* The secured-key tests in ``api_client/tests/test_algolia.py`` already loop the registry; add
  the new index name to the "all indices" expectation if you want explicit coverage.

Deploy / ops
------------

Once the code is merged and deployed:

#. Ops sets ``PRICE_ASC_REPLICA_INDEX_NAME`` (to e.g. ``enterprise_catalog_price_asc``) in the
   ``ALGOLIA`` config in edx-internal. **The whole** ``ALGOLIA`` **dict is replaced, not merged**,
   so the entire dict must be restated with the new key included.
#. Run ``./manage.py reindex_algolia``. A *virtual* replica exists as soon as it is declared on
   the primary index's settings (it mirrors the primary's records), so the replica is live after
   one reindex — no separate population step.

Frontend (only if the MFE will use the sort)
--------------------------------------------

The backend builds the replica regardless; a sort is only *user-visible* once the MFE points a
search at it. In ``frontend-app-learner-portal-enterprise``:

* add an ``ALGOLIA_<NAME>_REPLICA_INDEX_NAME`` env var in ``src/index.tsx`` and
  ``src/types/types.d.ts`` (mirror of the backend settings key);
* point the relevant ``<Index indexName=...>`` at it (see ``SearchVideo.jsx``, which uses the
  base replica, or ``SearchCourse.jsx`` for the recency replica);
* if the sort changes default behavior, gate it behind a waffle flag and/or an Optimizely
  experiment, exactly as the recency sort does (the flag doubles as a kill-switch — see ADR 0014).

Safety properties you get for free
----------------------------------

* **Inert by default.** No configured name → the replica is never declared, configured, or
  queryable. Merge and deploy ahead of ops with no effect.
* **Fail-safe configuration.** ``configure_algolia_index`` wraps each replica's settings call so an
  ``AlgoliaException`` is logged and skipped — one replica failing to configure never aborts the
  reindex, and the primary index plus the other replicas stay configured.
* **No** ``virtual(None)``. Unconfigured keys are skipped entirely, never declared as a broken
  ``virtual(None)`` replica on the primary index.
