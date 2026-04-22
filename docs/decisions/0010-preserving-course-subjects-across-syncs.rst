Preserving course subjects across discovery syncs
=================================================

Status
------

Accepted (March 2026)

Context
-------

Enterprise Catalog stores course metadata in ``ContentMetadata.json_metadata`` using two different
course-discovery payloads:

* ``/api/v1/search/all/`` during catalog membership updates
* ``/api/v1/courses/`` during full course metadata refreshes

This distinction matters for course ``subjects``, which power categories and subcategories in downstream
enterprise experiences.

For net-new course records, the ``/search/all/`` payload is stored when the ``ContentMetadata`` record is
created, so ``subjects`` can be present immediately. For existing course records, however, Enterprise Catalog
does not fully overwrite the stored metadata with ``/search/all/`` results. Instead, it merges only a small
allowlist of fields from ``COURSE_FIELDS_TO_PLUCK_FROM_SEARCH_ALL`` in order to preserve the existing API
contract for full course metadata.

Before this decision, ``subjects`` was not part of that allowlist. As a result:

* some courses retained ``subjects`` because they were newly created from ``/search/all/`` or had already
  been enriched by a successful full metadata refresh
* other existing courses did not pick up subject changes from ``/search/all/`` at all
* a full ``/api/v1/courses/`` refresh could also clear a previously populated ``subjects`` value when the
  response omitted subjects or returned an empty list

This produced inconsistent category and subcategory behavior where otherwise similar courses could appear
with or without those facets depending on which sync path last updated them.

Decision
--------

We will treat ``subjects`` as a field that must be preserved across both discovery sync paths:

* ``subjects`` will be included in ``COURSE_FIELDS_TO_PLUCK_FROM_SEARCH_ALL`` so that existing course
  records receive subject updates during incremental catalog metadata syncs.
* During full ``/api/v1/courses/`` updates, an empty or missing ``subjects`` value will not overwrite an
  existing non-empty ``subjects`` value already stored in ``ContentMetadata.json_metadata``.
* A non-empty ``subjects`` value from ``/api/v1/courses/`` will continue to replace the stored value.

This is a targeted exception to the more general pattern described in :doc:`0002-celery-task-restructuring`,
where full course metadata is normally expected to come from ``/api/v1/courses/``. In practice, ``subjects``
must remain available for category/subcategory faceting even when the full course payload is incomplete.

Consequences
------------

Courses will now converge more reliably on a consistent ``subjects`` value, reducing the chance that some
catalog items display categories/subcategories while others do not.

This decision also introduces an intentional field-level precedence rule:

* ``/search/all/`` is the authoritative incremental source for keeping ``subjects`` current on existing
  course records
* ``/api/v1/courses/`` remains the full metadata source, except that empty ``subjects`` payloads are treated
  as non-authoritative so they do not erase known-good subject data

The tradeoff is that, in cases where ``/api/v1/courses/`` temporarily returns empty ``subjects``, older
stored subjects may persist until discovery returns a non-empty value or ``/search/all/`` updates the field
again. We accept this because preserving known-good subject data is less disruptive than clearing categories
and subcategories from the learner experience.

Alternatives considered
-----------------------

Rely solely on ``/api/v1/courses/`` for subjects
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

We rejected this because the observed issue was caused in part by empty or omitted ``subjects`` values in the
full course payload. Using that payload as the only authority would continue to produce inconsistent category
and subcategory data.

Fully overwrite existing course metadata from ``/search/all/``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

We rejected this because the service intentionally preserves the existing full course metadata contract rather
than replacing it wholesale with the narrower ``/search/all/`` response.
