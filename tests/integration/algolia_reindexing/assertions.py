"""
Algolia assertion helpers for Phase 4a/4b integration tests.
"""
import time
import logging

logger = logging.getLogger(__name__)

ALGOLIA_POLL_INTERVAL_SECONDS = 2


def wait_and_assert_enterprise_catalog_query_uuids(
    algolia_index,
    aggregation_key,
    expected_uuids,
    absent_uuids=None,
    timeout=10,
):
    """
    Poll Algolia until the record(s) with the given aggregation_key have
    the expected enterprise_catalog_query_uuids facet values.

    Uses browse_objects with attributesToRetrieve and a filters clause.
    Raises AssertionError on timeout.
    """
    if absent_uuids is None:
        absent_uuids = []

    start_time = time.time()
    absent_set = set(absent_uuids)
    while True:
        found_uuids = get_algolia_catalog_query_uuids(algolia_index, aggregation_key)

        # Check if all expected UUIDs are present
        expected_set = set(expected_uuids)
        found_set = set(found_uuids)

        if expected_set.issubset(found_set) and absent_set.isdisjoint(found_set):
            logger.info(
                'Algolia assertion passed for %s: found %s',
                aggregation_key, found_uuids
            )
            return found_uuids

        elapsed = time.time() - start_time
        if elapsed >= timeout:
            raise AssertionError(
                f'Algolia assertion timeout for {aggregation_key}: '
                f'expected {expected_set}, found {found_set}, '
                f'absent {absent_set}, elapsed {elapsed:.1f}s'
            )

        time.sleep(ALGOLIA_POLL_INTERVAL_SECONDS)


def get_algolia_catalog_query_uuids(algolia_index, aggregation_key):
    """
    Return the union of enterprise_catalog_query_uuids across all Algolia
    shards for the given aggregation_key.  Returns an empty set if no
    matching objects exist.
    """
    all_uuids = set()

    try:
        # Use the filters DSL (not facetFilters) because aggregation_key
        # values contain a colon (e.g. "course:edX+DemoX"), which the
        # facetFilters parser handles ambiguously.
        iterator = algolia_index.browse_objects({
            'attributesToRetrieve': ['aggregation_key', 'enterprise_catalog_query_uuids'],
            'filters': f"aggregation_key:'{aggregation_key}'",
        })

        for hit in iterator:
            if hit.get('enterprise_catalog_query_uuids'):
                uuids = hit['enterprise_catalog_query_uuids']
                if isinstance(uuids, list):
                    all_uuids.update(uuids)
                else:
                    all_uuids.add(uuids)
    except Exception as exc:
        logger.warning('Error browsing Algolia for %s: %s', aggregation_key, exc)

    return list(all_uuids)


def assert_indexing_state_updated(content_key, after_timestamp):
    """
    Assert that ContentMetadataIndexingState.last_indexed_at for content_key
    is non-null and >= after_timestamp.
    """
    from enterprise_catalog.apps.search.models import ContentMetadataIndexingState
    from enterprise_catalog.apps.catalog.models import ContentMetadata

    cm = ContentMetadata.objects.get(content_key=content_key)
    state = ContentMetadataIndexingState.objects.get(content_metadata=cm)
    assert state.last_indexed_at is not None, f"{content_key}: last_indexed_at is None"
    assert state.last_indexed_at >= after_timestamp, (
        f"{content_key}: last_indexed_at {state.last_indexed_at} < {after_timestamp}"
    )


def assert_indexing_state_unchanged(content_key, expected_timestamp):
    """
    Assert that ContentMetadataIndexingState.last_indexed_at for content_key
    equals expected_timestamp (not re-indexed).
    """
    from enterprise_catalog.apps.search.models import ContentMetadataIndexingState
    from enterprise_catalog.apps.catalog.models import ContentMetadata

    cm = ContentMetadata.objects.get(content_key=content_key)
    state = ContentMetadataIndexingState.objects.get(content_metadata=cm)
    assert state.last_indexed_at == expected_timestamp, (
        f"{content_key}: expected {expected_timestamp}, got {state.last_indexed_at}"
    )
