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
consumer switches sort by pointing its search at a different index name.

The fundamental design of the enterprise-catalog logic assumes we will only ever
provision *one* replica: there is a single setting
(``ALGOLIA['REPLICA_INDEX_NAME']`` — the base "duration" replica the Learner
Portal points its video search at) and a single Python function that provisions
that one replica.

Historically the whole ``ALGOLIA`` dict has been *replaced* (not merged) from the
deployment YAML, so anything code wants to keep in it — like a replica's ranking
definition — would be lost unless ops restated it in ``edx-internal``.

We want to offer learners a "newest courses first" sort.  The problem is how to
add a *second* replica — and roll it out across the repositories that together
own enterprise search — without a partially-configured replica degrading or
breaking the existing relevance search.

Decision
--------

Add a recency-sorted replica (the ``enterprise_catalog_recently_released_desc``
entry in ``ALGOLIA['ADDITIONAL_REPLICA_INDEX_SETTINGS']``) that leads its ranking
with ``desc(recently_released_timestamp)`` — a per-course
Unix timestamp of the *earliest course-run start of any status* (the Discovery
course release date, the same signal as the ``is_new_content`` flag, via the
shared ``_earliest_course_run_start`` helper — ENT-11386).  Courses with no run
start get ``0`` so they sort last under a descending ranking — deliberately not
the far-future ``ALGOLIA_DEFAULT_TIMESTAMP``, which would float undated courses to
the top.  The primary index (``ALGOLIA['INDEX_NAME']``) keeps the relevance
ranking.

The replica is declared as an Algolia *virtual* replica (``virtual(name)``), so it
mirrors the primary's records instead of duplicating them.  This is a deliberate
cost/precision tradeoff versus a standard replica — see *Alternatives considered*
and *Consequences*.

The sort is rolled out across three repositories:

#. **enterprise-catalog** (this service) builds and configures the replica.
#. **edx-enterprise** exposes the ``enterprise.search_default_sort_newest``
   waffle flag via ``enterprise_features`` — the eligibility gate / kill-switch.
#. **frontend-app-learner-portal-enterprise** points the course ``<Index>`` at
   the replica when the flag is on *and* the Optimizely "newest" experiment
   variant is active for the user.

The design generalizes the old one-replica assumption to a settings-driven map, and
gates *user exposure* with the waffle flag rather than gating *declaration* on ops:

* Backend: additional sort replicas are declared in
  ``ALGOLIA['ADDITIONAL_REPLICA_INDEX_SETTINGS']`` — an ``index_name -> index settings``
  map defined in ``settings/base.py`` as config-as-code (the ``customRanking`` is code,
  since it sorts on a field the indexer computes).  ``ALGOLIA`` is added to
  ``DICT_UPDATE_KEYS`` so the deployment YAML is now *merged*, not replaced: ops can
  override per-environment index names and credentials while these code-defined replica
  settings are preserved.  One map is the single source of truth — its entries are
  declared on the primary index, configured during a reindex, and added to the secured-key
  ``restrictIndices`` — so adding a future sort is one new entry plus the field its
  ``customRanking`` sorts on.  (The base ``REPLICA_INDEX_NAME`` keeps its own
  per-environment key and its required-core-pair lifecycle in ``init_index`` /
  ``index_exists``.)
* Backend (fail-safe): configuring each replica in ``configure_algolia_index`` is
  wrapped so that any ``AlgoliaException`` is logged and skipped.  One replica
  failing to configure never aborts the reindex — the primary (relevance) index
  and the other replicas are still configured.  The failed replica keeps its prior
  settings (or, when brand new, mirrors the primary's relevance ranking) until the
  next successful run, so the degraded state is still the safe base sort.
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

* **Exposure is MFE/flag-gated, not declaration-gated:** the backend now declares the
  replica in every environment (it ships in ``ADDITIONAL_REPLICA_INDEX_SETTINGS``), so
  "is it declared" is no longer the gate.  The course search falls back to the primary
  (relevance) index whenever the MFE's replica env var is unset or the flag/experiment is
  off (the ``&& recentlyReleasedIndexName`` guard), so user exposure is controlled by the
  MFE env var + waffle flag.  Because the replica is *virtual* (no extra records), always
  declaring it costs nothing.
* **Not covered in code:** "the replica is pointed at by the MFE but its Algolia index
  does not exist yet" (e.g. the MFE env var is set before this service has been deployed
  and reindexed) → the MFE would query a missing index and surface an error / empty
  results rather than falling back.  This is a transient, operator-controlled window
  mitigated by the rollout order and the kill-switch flag, not by code.
* **Reindex degrades gracefully:** if configuring a replica fails, the error is
  logged and the reindex continues with the primary index and the other replicas
  intact, so a problem with one sort can never take down core search indexing.
  The worst case is that one replica lags its ranking until the next run — never a
  broken or empty primary index.
* **No added record cost:** because the replica is *virtual*, it mirrors the
  primary's records rather than duplicating them, so adding it does not grow our
  Algolia record count.  A standard (non-virtual) replica would roughly double the
  indexed record count — and its cost — for each sort we add (see *Alternatives*).
* **Non-course records sort last, by design:** the replica is *virtual* over the
  primary index, so it mirrors every record — programs, executive education, videos,
  etc. — not just courses.  ``recently_released_timestamp`` is only computed in the
  ``content_type == COURSE`` branch, so non-course records have no such attribute and
  Algolia ranks them last under ``desc(recently_released_timestamp)``.  This is fine
  because the consumer (the Learner Portal) points only its course ``<Index>`` at the
  replica and filters by content type; the "newest courses first" sort is, by
  contract, a course sort.  Were a future caller to query this replica for non-course
  content, those records would all tie at the bottom — that caller would need its own
  recency field.
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
* **A standard (non-virtual) replica instead of a virtual one.**  A standard
  replica is a full, independent copy of the index that sorts strictly by its own
  ``ranking``/``customRanking`` — a fully deterministic newest-first order
  regardless of the search query.  We chose a *virtual* replica instead: a virtual
  replica reuses the primary's records, so it adds no record count or cost (see
  *Consequences*), whereas a standard replica roughly doubles our indexed record
  count and its associated cost.  The accepted tradeoff is that a virtual replica
  always keeps textual relevance as the top-priority sort factor, so under a text
  query "newest first" is relevance-biased rather than strictly chronological (it
  *is* strictly chronological when browsing with no query).  If a strictly
  deterministic order under query later proves necessary, switching this one
  replica to a standard replica is the documented escape hatch.
