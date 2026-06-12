Newest-Courses-First Search Sort via a Recency-Sorted Algolia Replica
=====================================================================

Status
------

Proposed

Context
-------

The enterprise Learner Portal search page sorts a single Algolia index by
relevance.  Algolia does not re-sort an index at query time, so each alternate
sort order is a separate *replica* index with its own ``customRanking``; the
consumer switches sort by pointing its search at a different index name.  To
offer a "newest courses first" sort we add a recency-sorted replica.

* The primary index (``ALGOLIA['INDEX_NAME']``) keeps the relevance ranking.
* A new replica (``ALGOLIA['RECENTLY_PUBLISHED_REPLICA_INDEX_NAME']``) leads its
  ranking with ``desc(recently_published_timestamp)`` — a per-course Unix
  timestamp of the *earliest published course-run start* (the same signal as the
  ``is_new_content`` flag, via the shared ``_earliest_published_course_run_start``
  helper).  Courses with no published run start get ``0`` so they sort last under
  a descending ranking — deliberately not the far-future ``ALGOLIA_DEFAULT_TIMESTAMP``,
  which would float undated courses to the top.

The sort is rolled out across three repositories:

#. **enterprise-catalog** (this service) builds and configures the replica.
#. **edx-enterprise** exposes the ``enterprise.search_default_sort_newest``
   waffle flag via ``enterprise_features`` — the eligibility gate / kill-switch.
#. **frontend-app-learner-portal-enterprise** points the course ``<Index>`` at
   the replica when the flag is on *and* the Optimizely "newest" experiment
   variant is active for the user.

Two facts shape the failure modes:

* ``ALGOLIA`` is *replaced* (not merged) from the deployment YAML, so the replica
  is only live once ops sets ``RECENTLY_PUBLISHED_REPLICA_INDEX_NAME`` in
  ``edx-internal`` and a ``reindex_algolia`` run declares it on the primary.
* An Algolia *virtual* replica exists as soon as it is declared on the primary
  index's settings (it mirrors the primary's records); it does not wait for a
  populated record set.  So once this service is deployed and ``reindex_algolia``
  has run, the replica exists.

Decision
--------

The replica is **config-gated on both sides** and is never queried unless its
name is configured:

* Backend: the replica is declared on the primary index and its settings are
  applied **only** when ``RECENTLY_PUBLISHED_REPLICA_INDEX_NAME`` is set;
  otherwise it is a no-op (no ``virtual(None)`` replica is created).
* MFE: the course search uses the replica only when its index-name config var is
  non-empty (and the flag + experiment gates pass); otherwise it falls back to
  the primary (relevance) index.

**The open question this ADR records:** how should we handle the case where the
replica *name is configured* but the Algolia index does **not yet exist** (e.g.
the MFE env var is set before this service has been deployed and reindexed)?

The proposed answer is to **rely on operational guarantees rather than runtime
index-existence detection**:

#. the documented rollout order — deploy + ``reindex_algolia`` here *before*
   pointing the MFE at the replica; and
#. the ``enterprise.search_default_sort_newest`` waffle flag, which doubles as a
   readiness gate and an instant kill-switch — it should not be enabled until the
   replica is live, and flipping it off immediately reverts every learner to the
   relevance index.

We deliberately do **not** add code that detects a missing Algolia index at
search time and silently falls back to the primary index.

Consequences
------------

* **Covered:** "replica name not configured" → the base (relevance) index is
  used.  This is handled explicitly in both the backend (conditional replica
  declaration) and the MFE (the ``&& recentlyPublishedIndexName`` guard), so the
  default-to-base behavior is guaranteed for the unconfigured case.
* **Not covered in code:** "replica name configured but the Algolia index does
  not exist yet" → the MFE would query a missing index and surface an error /
  empty results rather than falling back.  This is a transient,
  operator-controlled window mitigated by the rollout order and the kill-switch
  flag, not by code.
* **The waffle flag is the readiness contract:** enabling it asserts "the replica
  is live."  This keeps the safe path a single, instantly reversible toggle
  rather than per-request defensive logic in the search hot path.
* **Escape hatch is recorded:** if the operational mitigation proves too fragile
  in practice, the documented next step is an ``onError``/try-primary fallback in
  the MFE search path (see *Alternatives*).  Capturing the question here lets us
  revisit it without re-discovering the trade-off.

Alternatives considered
------------------------

* **Runtime index-existence detection / fall back to base on Algolia error.**
  Rejected for now: react-instantsearch would need an error path that re-renders
  against the primary index, adding per-search complexity and an extra failure
  mode, to protect a transient window the kill-switch flag already guards.  It
  remains the documented escape hatch if the operational approach proves
  insufficient.
* **Always declare the replica (no config gate).**  Rejected: would create a
  ``virtual(None)`` replica on the primary index in environments where the name
  is unset, and would couple every environment to the rollout.
