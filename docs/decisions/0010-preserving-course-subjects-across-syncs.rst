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

Critically, the two endpoints return ``subjects`` in **different shapes**:

* ``/search/all/`` returns subjects as a flat list of strings, e.g.
  ``["Communication", "Art & Culture", "Humanities"]``
* ``/api/v1/courses/`` returns subjects as a list of rich dicts containing ``name``, ``slug``, ``uuid``,
  ``description``, ``banner_image_url``, ``card_image_url``, and other fields

Consumers of the ``get-content-metadata`` API expect the rich dict format. Because of this shape mismatch,
``subjects`` is intentionally excluded from ``COURSE_FIELDS_TO_PLUCK_FROM_SEARCH_ALL`` — the allowlist of
fields merged from ``/search/all/`` into existing course records. Plucking the flat-string version would
break the API contract (see `PR #284 <https://github.com/openedx/enterprise-catalog/pull/284>`_).

This means that ``/api/v1/courses/`` is the sole authoritative source of ``subjects`` for existing course
records. However, that endpoint can occasionally return an empty or missing ``subjects`` list for courses
that do have subjects in the discovery database.

Without protection, an empty ``subjects`` response from ``/api/v1/courses/`` would overwrite the existing
rich subject data, causing categories and subcategories to disappear from the learner experience until a
later sync restores them.

Decision
--------

We will add a defensive guard in ``_update_single_full_course_record`` (in ``tasks.py``) so that an empty
or missing ``subjects`` value from ``/api/v1/courses/`` does not overwrite an existing non-empty ``subjects``
value in ``ContentMetadata.json_metadata``:

* Before merging the full course metadata dict, snapshot the existing ``subjects`` value.
* After the merge, if the resulting ``subjects`` is empty but the prior value was non-empty, restore the
  prior value.
* A non-empty ``subjects`` value from ``/api/v1/courses/`` continues to replace the stored value normally.

This is a targeted exception to the general pattern described in :doc:`0002-celery-task-restructuring`,
where full course metadata is normally expected to come from ``/api/v1/courses/``.

Consequences
------------

* Empty ``subjects`` payloads from ``/api/v1/courses/`` are treated as non-authoritative and will not
  clear existing known-good subject data.
* Non-empty ``subjects`` payloads from ``/api/v1/courses/`` update the stored value normally.
* ``subjects`` remains excluded from ``COURSE_FIELDS_TO_PLUCK_FROM_SEARCH_ALL`` because of the shape
  mismatch between the two endpoints.  The ``/search/all/`` ingestion path does not touch stored
  ``subjects`` at all for existing course records.

The tradeoff is that, in cases where ``/api/v1/courses/`` temporarily returns empty ``subjects``, older
stored subjects may persist until a later sync provides a non-empty value. We accept this because preserving
known-good subject data is less disruptive than clearing categories and subcategories from the learner
experience.

Alternatives considered
-----------------------

Add ``subjects`` to ``COURSE_FIELDS_TO_PLUCK_FROM_SEARCH_ALL``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

We rejected this because the ``/search/all/`` endpoint returns subjects as flat strings while the
``get-content-metadata`` API contract requires rich dicts. Plucking the flat-string version would silently
change the shape of ``subjects`` in downstream responses.

Fully overwrite existing course metadata from ``/search/all/``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

We rejected this because the service intentionally preserves the existing full course metadata contract rather
than replacing it wholesale with the narrower ``/search/all/`` response.
