0014 Filter content after pagination instead of denormalizing flags
==================================================================

Status
------

Accepted

Context
-------

``EnterpriseCatalogGetContentMetadata.get_content_metadata`` was materializing the
entire catalog queryset on every request before paginating. The sequence was:

.. code-block:: python

    len(queryset)                                                   # evaluates entire catalog
    queryset = [item for item in queryset if not is_unpublished(item)]  # reads json_metadata per row
    queryset = [item for item in queryset if is_active(item)]          # reads json_metadata per row again
    page = self.paginate_queryset(queryset)                        # slices a Python list, not SQL

``is_unpublished`` and ``is_active`` both read ``item.json_metadata`` (a large JSON blob) for every
row in the catalog, regardless of which page was requested. A 9.7s trace at ``page=207&page_size=10``
showed ~8.7s unaccounted for by DB or cache -- all of it Python deserializing JSON for rows that
were never returned. ``page=1&page_size=50`` clocked 7.2s for the same reason: the page number
barely matters when you deserialize the whole catalog first.

This was introduced by commits that added the unpublished/active filters as list comprehensions
without realising they forced full queryset evaluation ahead of ``paginate_queryset``.

Decision
--------

Move the ``is_unpublished`` / ``is_active`` filters to run *after* ``paginate_queryset``. The queryset
stays lazy, so ``paginate_queryset`` issues SQL ``COUNT`` + ``LIMIT/OFFSET`` and fetches only
``page_size`` rows. The Python filters then read ``json_metadata`` on those rows only -- 10-50
items instead of potentially thousands.

The tradeoff: in the normal paginated path the ``count`` field in the response reflects the SQL
``COUNT``, which includes items that will be filtered. The count is slightly inflated when
unpublished or inactive courses exist in the catalog. ``next``/``previous`` links still navigate
correctly. For the ``traverse_pagination`` path all results are materialized and filtered before
counting, so that count remains exact.

Rejected alternative: denormalizing onto the table
---------------------------------------------------

The schema approach would add ``is_published`` and ``is_active`` boolean columns to
``ContentMetadata`` with a composite index, push the filters into SQL, and keep both the queryset
and the count exact end-to-end. It requires:

1. A migration adding the two columns and index.
2. A ``ContentMetadata.save()`` override to recompute both flags on every write.
3. Explicit flag recomputation in the ``bulk_update`` path (``models.py:1320``), which bypasses
   ``save()``.
4. A batched backfill management command for existing rows.
5. Deploy coordination between migration, backfill, and the view change.

This was rejected because the performance win is identical for the real bottleneck -- both
approaches reduce per-request JSON deserialization from whole-catalog to one page. The count
inflation in the no-migration approach is minor: the machine-to-machine callers (``AmazonAPIGateway``)
traverse pages using ``next`` links rather than computing page counts from ``count``. The schema
approach adds meaningful deploy risk with no runtime benefit over the simpler fix.

Revisit the schema approach if exact counts become a hard requirement from consumers, or if the
proportion of filtered items grows large enough that under-filled pages cause traversal problems.

Consequences
------------

- Per-request JSON deserialization drops from whole-catalog to ``page_size`` rows.
- ``count`` in paginated responses is slightly inflated when filtered items exist. The
  ``traverse_pagination`` path is unaffected.
- No migration, no backfill, no deploy coordination beyond a single view change.
- Secondary costs (867ms M2M ``ORDER BY``, N+1 ``parent_content_key`` lookups) are deferred to a
  follow-up PR; they are now bounded to one page and no longer on the critical path.
