Dispatcher Uses Serial Chain-of-Groups for Content-Type Ordering
================================================================

Status
------

Accepted

Context
-------

The Phase 4a dispatcher (``dispatch_algolia_indexing``) fans out three
content types — courses, programs, and learner pathways — to parallel batch
indexing tasks.  The original design enqueued all batch tasks with flat
``.delay()`` calls, treating the three types as independent.

During code review it became clear that this independence assumption is
incorrect.  Staleness detection for **programs** and **pathways** is
implemented by comparing the parent's ``last_indexed_at`` against the
``last_indexed_at`` of its *child* content (child courses for programs;
child programs for pathways).  Specifically:

* A program is considered stale — and therefore re-dispatched — when any of
  its child courses has a ``last_indexed_at`` that is *newer* than the
  program's own ``last_indexed_at``.
* A pathway is considered stale when any of its child programs is newer.

``ContentMetadataIndexingState.last_indexed_at`` is only written **after**
a batch task successfully completes.  If course batches and program batches
are dispatched simultaneously (or interleaved by Celery), a program batch
task may execute before its child course tasks have finished writing their
updated timestamps.  In that case the program incorrectly appears
non-stale, is skipped, and its Algolia shards are left with outdated
parent-level metadata until the next dispatcher run catches it.

Decision
--------

The dispatcher assembles batch task signatures into a **chain of Celery
groups**:

.. code-block:: text

    chain(
        group(all_course_batches),    # parallel; must ALL complete first
        group(all_program_batches),   # parallel; must ALL complete first
        group(all_pathway_batches),   # parallel
    ).apply_async()

Each group runs its contained tasks in parallel.  Celery does not start the
next group in the chain until every task in the current group has
acknowledged completion.  This guarantees:

1. All course ``last_indexed_at`` timestamps are up to date before any
   program staleness check runs.
2. All program ``last_indexed_at`` timestamps are up to date before any
   pathway staleness check runs.

Batch task signatures are built with ``.si()`` (immutable signatures) so
the return values from one group are not forwarded as positional arguments
to the next group's tasks — each task receives only the kwargs it was
constructed with.  Empty groups (content types with zero batches to
dispatch) are omitted from the chain so no no-op tasks are enqueued.

Consequences
------------

* **Correctness**: child-staleness propagation now works in a single
  dispatcher pass.  Without this change, a course updated between two
  dispatcher runs would correctly trigger a program re-index only on the
  *second* run after the update (the first run would update the course
  timestamp, but the program would only see the new timestamp on the next
  pass).
* **Latency**: total wall-clock time for a full dispatcher pass increases
  slightly because programs cannot start until all course batches finish.
  In practice the added latency is small relative to the batch task
  processing time, and the correctness gain outweighs it.
* **Celery backend required**: Celery chains with groups require a result
  backend (e.g. Redis) to track group completion.  This is already a
  requirement for the broader Celery setup in this service (Redis is used
  for the task queue), so no new infrastructure is needed.
* **``CELERY_TASK_ALWAYS_EAGER`` compatibility**: in eager mode (used by
  unit tests), Celery executes chains and groups synchronously in the
  correct order.  The unit tests mock the task ``.si()`` method and the
  ``chain``/``group`` callables to prevent actual dispatch and to assert
  on which content_keys were selected for each content type.
