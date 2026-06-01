"""
Integration test scenarios for Phase 4a/4b Algolia incremental indexing.

**Isolation note**: The 4a dispatcher (``dispatch_algolia_indexing``) sweeps
*all* ContentMetadata in the database, which makes it unsuitable for isolated
integration testing in environments that already have real records.

Scenarios 1 and 2 therefore call the Phase 3 batch tasks directly with our
fixture content keys instead of going through the full dispatcher.  This still
exercises the complete indexing path end-to-end (Algolia writes, orphan-shard
cleanup, ContentMetadataIndexingState updates) — we just skip the
"which keys need indexing?" sweep that the dispatcher adds.  That sweep is
covered thoroughly by the dispatcher's own unit tests.

Scenarios 3 and 4 use the per-catalog dispatcher (``dispatch_algolia_indexing_for_catalog_query``),
which is naturally scoped to a single CatalogQuery and therefore only touches
our fixture records.
"""
import logging
import time
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

FIXTURE_COURSE_KEYS = ['edX+DemoX', 'edX+E2E-101', 'IBM+CAD0210EN']
FIXTURE_PROGRAM_KEY = '96b52724-0a92-44e7-a856-dbbfbc696a6a'

CQ1_UUID = 'aaaaaaaa-1111-1111-1111-000000000001'
CQ2_UUID = 'aaaaaaaa-1111-1111-1111-000000000002'
CQ3_UUID = 'aaaaaaaa-1111-1111-1111-000000000003'


def scenario_1_force_index_all(content_map, cq_map, algolia_client, wait_seconds=10, dry_run=False):
    """
    Scenario 1 (Phase 3 batch tasks / 4a path): Index all 4 fixture records.

    Calls the batch tasks directly with our fixture keys so the test is
    isolated from any other ContentMetadata records in the environment.

    Verifies:
    - Algolia contains the indexed records with correct enterprise_catalog_query_uuids facets.
    - ContentMetadataIndexingState.last_indexed_at is set for all 4 records.
    """
    from enterprise_catalog.apps.search.models import ContentMetadataIndexingState
    from enterprise_catalog.apps.search.tasks import (
        index_courses_batch_in_algolia,
        index_programs_batch_in_algolia,
    )
    from .assertions import (
        wait_and_assert_enterprise_catalog_query_uuids,
        assert_indexing_state_updated,
    )

    # Reset indexing state for a clean baseline
    for cm in content_map.values():
        try:
            cm.indexing_state.delete()
        except ContentMetadataIndexingState.DoesNotExist:
            pass

    before_time = timezone.now()
    time.sleep(0.1)

    # Index courses and program directly (bypasses the global dispatcher sweep)
    index_courses_batch_in_algolia(content_keys=FIXTURE_COURSE_KEYS, force=True)
    index_programs_batch_in_algolia(content_keys=[FIXTURE_PROGRAM_KEY], force=True)

    # Wait for Algolia to settle
    time.sleep(wait_seconds)

    # Courses get CQ2_UUID only (UUID inheritance is upward: courses do NOT
    # inherit the program's CQ1 facet).
    wait_and_assert_enterprise_catalog_query_uuids(
        algolia_client,
        'course:edX+DemoX',
        [CQ2_UUID],
        timeout=wait_seconds,
    )
    wait_and_assert_enterprise_catalog_query_uuids(
        algolia_client,
        'course:edX+E2E-101',
        [CQ2_UUID],
        timeout=wait_seconds,
    )
    wait_and_assert_enterprise_catalog_query_uuids(
        algolia_client,
        'course:IBM+CAD0210EN',
        [CQ2_UUID, CQ3_UUID],
        timeout=wait_seconds,
    )
    # Program gets CQ1_UUID (direct membership) + CQ2_UUID (inherited from child courses).
    wait_and_assert_enterprise_catalog_query_uuids(
        algolia_client,
        f'program:{FIXTURE_PROGRAM_KEY}',
        [CQ1_UUID, CQ2_UUID],
        timeout=wait_seconds,
    )

    # Verify state rows were written
    for content_key in FIXTURE_COURSE_KEYS + [FIXTURE_PROGRAM_KEY]:
        assert_indexing_state_updated(content_key, before_time)

    logger.info('Scenario 1: PASS')


def scenario_2_stale_detection(content_map, cq_map, algolia_client, wait_seconds=10, dry_run=False):
    """
    Scenario 2 (Phase 3 batch tasks with stale skip logic): Only stale records
    are re-indexed when ``force=False``.

    Calls the batch task directly (force=False) so the skip logic inside
    ``_resolve_indexing_decision`` is exercised without sweeping the whole DB.

    Prerequisites: Scenario 1 completed (all 4 records have last_indexed_at set).

    Verifies:
    - edX+DemoX (made stale) is re-indexed; its last_indexed_at advances.
    - edX+E2E-101 and IBM+CAD0210EN (not stale) are skipped; their
      last_indexed_at values are unchanged.
    """
    from enterprise_catalog.apps.catalog.models import ContentMetadata
    from enterprise_catalog.apps.search.models import ContentMetadataIndexingState
    from enterprise_catalog.apps.search.tasks import index_courses_batch_in_algolia
    from .assertions import (
        assert_indexing_state_updated,
        assert_indexing_state_unchanged,
    )

    # Capture current last_indexed_at for the three courses
    initial_timestamps = {}
    for content_key in FIXTURE_COURSE_KEYS:
        cm = ContentMetadata.objects.get(content_key=content_key)
        state = ContentMetadataIndexingState.objects.get(content_metadata=cm)
        initial_timestamps[content_key] = state.last_indexed_at

    time.sleep(0.1)

    # Make edX+DemoX stale: touch its json_metadata via .save() so that the
    # auto_now field on ContentMetadata.modified is updated.  Using .update()
    # bypasses auto_now and would leave .modified unchanged.
    cm_demo = content_map['edX+DemoX']
    cm_demo._json_metadata = dict(cm_demo._json_metadata)
    cm_demo._json_metadata['_integration_test_touch'] = True
    cm_demo.save()
    cm_demo.refresh_from_db()

    state = cm_demo.indexing_state
    old_time = cm_demo.modified - timedelta(seconds=10)
    ContentMetadataIndexingState.objects.filter(pk=state.pk).update(last_indexed_at=old_time)

    before_reindex = timezone.now()
    time.sleep(0.1)

    # Run batch task with force=False — only edX+DemoX should be re-indexed
    index_courses_batch_in_algolia(content_keys=FIXTURE_COURSE_KEYS, force=False)

    # edX+DemoX should have an updated last_indexed_at
    assert_indexing_state_updated('edX+DemoX', before_reindex)
    # The other two should be unchanged (skipped)
    assert_indexing_state_unchanged('edX+E2E-101', initial_timestamps['edX+E2E-101'])
    assert_indexing_state_unchanged('IBM+CAD0210EN', initial_timestamps['IBM+CAD0210EN'])

    logger.info('Scenario 2: PASS')


def scenario_3_per_catalog_dispatch(content_map, cq_map, algolia_client, wait_seconds=10, dry_run=False):
    """
    Scenario 3 (Phase 4b): Per-catalog dispatcher for CQ3 (IBM+CAD0210EN only).

    Uses ``dispatch_algolia_indexing_for_catalog_query``, which is naturally
    scoped to the fixture CatalogQuery and does not touch other DB records.

    Verifies:
    - db_membership_count == 1 (only IBM+CAD0210EN is in CQ3).
    - IBM+CAD0210EN is dispatched and indexed.
    - Algolia: IBM+CAD0210EN has CQ3_UUID in enterprise_catalog_query_uuids.
    """
    from enterprise_catalog.apps.search.tasks import dispatch_algolia_indexing_for_catalog_query
    from .assertions import wait_and_assert_enterprise_catalog_query_uuids

    cq3 = cq_map[CQ3_UUID]

    summary = dispatch_algolia_indexing_for_catalog_query(
        catalog_query_id=cq3.id,
        force=True,
        dry_run=dry_run,
    )
    logger.info('Scenario 3 dispatcher summary: %s', summary)

    assert summary.get('db_membership_count') == 1, (
        f"Expected db_membership_count=1, got {summary}"
    )
    assert summary.get('dispatched', {}).get('course', {}).get('records') == 1, (
        f"Expected 1 course dispatched, got {summary}"
    )

    time.sleep(wait_seconds)

    wait_and_assert_enterprise_catalog_query_uuids(
        algolia_client,
        'course:IBM+CAD0210EN',
        [CQ3_UUID],
        timeout=wait_seconds,
    )

    logger.info('Scenario 3: PASS')


def scenario_4_membership_removal(content_map, cq_map, algolia_client, wait_seconds=10, dry_run=False):
    """
    Scenario 4 (Phase 4b): Membership removal detection via per-catalog dispatcher.

    Prerequisites: All 4 records indexed (scenario_1 completed). IBM+CAD0210EN
    has CQ2_UUID in its Algolia enterprise_catalog_query_uuids.

    Verifies:
    - Removing IBM+CAD0210EN from CQ2's DB membership is detected by the diff
      against Algolia (removed_count >= 1).
    - After re-indexing, IBM+CAD0210EN no longer has CQ2_UUID in its facets.
    - IBM+CAD0210EN still has CQ3_UUID (unchanged membership).
    """
    from enterprise_catalog.apps.search.tasks import dispatch_algolia_indexing_for_catalog_query
    from .assertions import wait_and_assert_enterprise_catalog_query_uuids

    cq2 = cq_map[CQ2_UUID]

    # Remove IBM from CQ2 membership
    ibm_cm = content_map['IBM+CAD0210EN']
    ibm_cm.catalog_queries.remove(cq2)

    try:
        summary = dispatch_algolia_indexing_for_catalog_query(
            catalog_query_id=cq2.id,
            force=True,
            dry_run=dry_run,
        )
        logger.info('Scenario 4 dispatcher summary: %s', summary)

        assert summary.get('removed_count', 0) >= 1, (
            f"Expected removed_count >= 1, got {summary}"
        )
        assert summary.get('dispatched', {}).get('course', {}).get('records', 0) >= 1, (
            f"Expected >= 1 course dispatched (IBM as removed key), got {summary}"
        )

        time.sleep(wait_seconds)

        # CQ2_UUID should be gone; CQ3_UUID should remain
        wait_and_assert_enterprise_catalog_query_uuids(
            algolia_client,
            'course:IBM+CAD0210EN',
            [CQ3_UUID],
            absent_uuids=[CQ2_UUID],
            timeout=wait_seconds,
        )
    finally:
        # Always restore IBM to CQ2 so the fixture is left consistent
        ibm_cm.catalog_queries.add(cq2)

    logger.info('Scenario 4: PASS')
