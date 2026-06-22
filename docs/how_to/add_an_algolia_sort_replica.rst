Add a new Algolia sort replica
==============================

The Learner Portal search page sorts a single Algolia index by relevance. Algolia does
not re-sort an index at query time, so every alternate sort order is a separate *replica*
index with its own ``customRanking``; the consumer (the MFE) switches sort by pointing its
search at a different index name.

Beyond the base replica (``ALGOLIA['REPLICA_INDEX_NAME']``, used by the MFE video search),
every additional sort replica is declared in **one place** -- the
``ALGOLIA['ADDITIONAL_VIRTUAL_REPLICA_INDEX_SETTINGS']`` map -- so adding a sort is mostly additive.
This guide walks through it end to end. See
``docs/decisions/0014-newest-courses-sort-replica.rst`` for the design rationale, and treat the
**recently-released ("newest first") replica** as the canonical example to copy.

The mental model: one settings-driven map
------------------------------------------

``ALGOLIA['ADDITIONAL_VIRTUAL_REPLICA_INDEX_SETTINGS']`` maps an **index name** to that replica's
**Algolia index settings** (its ``customRanking``). It is defined in ``settings/base.py`` as
config-as-code: a replica's ranking sorts on a field the indexing code must compute, so the
definition is intrinsically code, not deployment config.

``ALGOLIA`` is *merged* (not replaced) from the deployment config -- it is listed in
``DICT_UPDATE_KEYS`` (``settings/production.py``) -- so an environment can override the per-env
index names / credentials while the code-defined ``ADDITIONAL_VIRTUAL_REPLICA_INDEX_SETTINGS`` defaults are
preserved.

The backend declares and configures every replica in this map on each reindex, and the secured API
key grants access to them. A sort is only *user-visible* once the MFE points a search at it, gated
by a waffle flag / Optimizely experiment -- so the flag, not the map, controls exposure (see
*Frontend* below and ADR 0014).

Worked example
--------------

Suppose we want a **"price: low to high"** sort, using an index named
``enterprise_catalog_price_asc``.

Backend steps (this service)
----------------------------

**1. Make sure the field you sort on is indexed.** A ``customRanking`` can only sort on a numeric
attribute that exists on the records. If your sort uses a field that is *already* indexed (the
price example reuses ``first_enrollable_paid_seat_price``), skip this step. If it needs a new
signal, in ``enterprise_catalog/apps/catalog/algolia_utils.py``:

* write a ``get_course_<signal>(course)`` helper that returns the numeric value (see
  ``get_course_recently_released_timestamp`` -- note it returns ``0`` for "no value" so those
  records sort last under a ``desc`` ranking; pick a sentinel that sorts your missing values to the
  *end* of *your* order);
* add the field name to ``ALGOLIA_FIELDS``;
* set it on the course object in ``_algolia_object_from_product`` (the ``content_type == COURSE``
  branch).

**2. Define the replica's ranking and register it.** In ``enterprise_catalog/settings/base.py``,
add a settings constant and an entry in ``ALGOLIA['ADDITIONAL_VIRTUAL_REPLICA_INDEX_SETTINGS']`` keyed by
the index name. Lead the ``customRanking`` with your sort criterion, then append the primary
index's shared tie-breakers so records that tie on your criterion (and any "missing value" bucket)
fall back to the relevance ordering and pagination stays deterministic:

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

    ALGOLIA = {
        'INDEX_NAME': '',
        'REPLICA_INDEX_NAME': '',
        'ADDITIONAL_VIRTUAL_REPLICA_INDEX_SETTINGS': {
            'enterprise_catalog_recently_released_desc': ALGOLIA_RECENTLY_RELEASED_REPLICA_INDEX_SETTINGS,
            'enterprise_catalog_price_asc': ALGOLIA_PRICE_ASC_REPLICA_INDEX_SETTINGS,
        },
        'APPLICATION_ID': '',
        'API_KEY': '',
    }

**That is all the wiring.** You do **not** touch ``_get_algolia_replica_names``,
``_configured_replicas``, ``configure_algolia_index``, or the secured-key ``replica_index_names``
-- they all read ``ADDITIONAL_VIRTUAL_REPLICA_INDEX_SETTINGS``, so the new replica is automatically declared
on the primary index, has its settings applied during a reindex, and is added to the secured API
key's ``restrictIndices``.

Tests
-----

* Extend the configure / registry tests in
  ``enterprise_catalog/apps/catalog/tests/test_algolia_utils.py`` (e.g.
  ``test_get_algolia_replica_names_combines_base_and_additional_replicas`` and
  ``test_configure_algolia_index_configures_additional_replica``) to cover the new replica, using
  ``override_settings(ALGOLIA={...})``.
* If you added a field computation, unit-test the ``get_course_<signal>`` helper.
* The secured-key tests in ``api_client/tests/test_algolia.py`` exercise ``restrictIndices``; add
  the new index name to the "all indices" expectation if you want explicit coverage.

Deploy / ops
------------

Once the code is merged and deployed:

#. The replica's name and settings ship in code (``ADDITIONAL_VIRTUAL_REPLICA_INDEX_SETTINGS``), so no
   edx-internal change is required to *declare* it -- and normally ops should not override it at all.
   The ``ALGOLIA`` merge is *shallow*: the deployment YAML's top-level ``ALGOLIA`` keys override the
   code defaults, but nested dicts are **not** deep-merged. So setting
   ``ALGOLIA['ADDITIONAL_VIRTUAL_REPLICA_INDEX_SETTINGS']`` in edx-internal **replaces the entire map**
   for that environment (dropping every code-defined replica it does not restate) rather than
   overriding individual entries. Prefer setting the index name in code; override the map only when
   you intend to fully restate it.
#. Run ``./manage.py reindex_algolia``. A *virtual* replica exists as soon as it is declared on the
   primary index's settings (it mirrors the primary's records), so the replica is live after one
   reindex -- no separate population step.

Frontend (only if the MFE will use the sort)
--------------------------------------------

The backend builds the replica regardless; a sort is only *user-visible* once the MFE points a
search at it. In ``frontend-app-learner-portal-enterprise``:

* add an ``ALGOLIA_<NAME>_REPLICA_INDEX_NAME`` env var in ``src/index.tsx`` and
  ``src/types/types.d.ts`` whose value matches the backend index name;
* point the relevant ``<Index indexName=...>`` at it (see ``SearchVideo.jsx``, which uses the base
  replica, or ``SearchCourse.jsx`` for the recency replica);
* gate it behind a waffle flag and/or an Optimizely experiment, exactly as the recency sort does
  (the flag doubles as a kill-switch -- see ADR 0014).

Safety properties
-----------------

* **Virtual replicas, no extra records.** Each replica is declared ``virtual(name)``, so it mirrors
  the primary's records rather than duplicating them -- no added Algolia record count or cost.
* **Fail-safe configuration.** ``configure_algolia_index`` wraps each replica's settings call so an
  ``AlgoliaException`` is logged and skipped -- one replica failing to configure never aborts the
  reindex, and the primary index plus the other replicas stay configured.
* **Flag-gated exposure.** Declaring a replica does not make it user-visible; the MFE only queries
  it when its waffle flag / experiment is on, so the flag is the kill-switch.
