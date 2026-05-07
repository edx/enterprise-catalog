"""
Tests for ``enterprise_catalog.apps.search.indexing_mappings``.
"""
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from enterprise_catalog.apps.search import indexing_mappings as mappings_module
from enterprise_catalog.apps.search.indexing_mappings import (
    CACHE_KEY,
    IndexingMappings,
    get_indexing_mappings,
    invalidate_indexing_mappings_cache,
)


class TestGetIndexingMappings(TestCase):
    """
    Tests for the cache wrapper. The underlying compute path is tested separately
    to keep these focused on caching semantics.
    """

    def setUp(self):
        cache.delete(CACHE_KEY)

    def tearDown(self):
        cache.delete(CACHE_KEY)

    @mock.patch.object(mappings_module, '_compute_indexing_mappings')
    def test_cache_miss_computes_and_stores(self, mock_compute):
        """
        On cache miss, the underlying compute is called and the result is cached.
        """
        expected = IndexingMappings(all_indexable_content_keys={'course-a'})
        mock_compute.return_value = expected

        result = get_indexing_mappings()

        self.assertEqual(result, expected)
        mock_compute.assert_called_once()
        # Now stored in cache:
        self.assertEqual(cache.get(CACHE_KEY), expected)

    @mock.patch.object(mappings_module, '_compute_indexing_mappings')
    def test_cache_hit_skips_compute(self, mock_compute):
        """
        On cache hit, ``_compute_indexing_mappings`` is not called.
        """
        cached = IndexingMappings(all_indexable_content_keys={'course-cached'})
        cache.set(CACHE_KEY, cached, timeout=60)

        result = get_indexing_mappings()

        self.assertEqual(result, cached)
        mock_compute.assert_not_called()

    @mock.patch.object(mappings_module, '_compute_indexing_mappings')
    def test_force_refresh_bypasses_cache(self, mock_compute):
        """
        ``force_refresh=True`` recomputes even when a cached value exists.
        """
        stale = IndexingMappings(all_indexable_content_keys={'course-stale'})
        fresh = IndexingMappings(all_indexable_content_keys={'course-fresh'})
        cache.set(CACHE_KEY, stale, timeout=60)
        mock_compute.return_value = fresh

        result = get_indexing_mappings(force_refresh=True)

        self.assertEqual(result, fresh)
        mock_compute.assert_called_once()
        self.assertEqual(cache.get(CACHE_KEY), fresh)

    def test_invalidate_clears_cache(self):
        """
        ``invalidate_indexing_mappings_cache()`` removes the cached entry.
        """
        cache.set(CACHE_KEY, IndexingMappings(), timeout=60)
        invalidate_indexing_mappings_cache()
        self.assertIsNone(cache.get(CACHE_KEY))

    @override_settings(ALGOLIA_INDEXING_MAPPINGS_CACHE_TIMEOUT=42)
    @mock.patch.object(mappings_module.cache, 'set')
    @mock.patch.object(mappings_module, '_compute_indexing_mappings')
    def test_uses_configured_cache_timeout(self, mock_compute, mock_cache_set):
        """
        Cache writes use the value from settings.
        """
        mock_compute.return_value = IndexingMappings()
        get_indexing_mappings()
        mock_cache_set.assert_called_once()
        _, kwargs = mock_cache_set.call_args
        self.assertEqual(kwargs.get('timeout'), 42)


class TestComputeIndexingMappings(TestCase):
    """
    Tests for ``_compute_indexing_mappings``. Mocks the upstream legacy helpers
    so this stays focused on what the function does itself: pass through the
    membership mappings and build the indexable-content-keys set from the
    partition functions.
    """
    # pylint: disable=protected-access

    @mock.patch.object(mappings_module, 'ContentMetadata')
    @mock.patch.object(mappings_module, 'partition_program_keys_for_indexing')
    @mock.patch.object(mappings_module, 'partition_course_keys_for_indexing')
    @mock.patch.object(mappings_module, '_precalculate_content_mappings')
    def test_compute_passes_through_mappings_and_builds_indexable_set(
        self,
        mock_precalc,
        mock_partition_courses,
        mock_partition_programs,
        mock_content_metadata,
    ):
        """
        Membership mappings are returned as-is (already content_key strings);
        ``all_indexable_content_keys`` is the union of indexable courses,
        indexable programs, and all pathway keys.
        """
        mock_precalc.return_value = (
            {'program-p': {'course-a', 'course-b'}},
            {'pathway-x': {'program-p', 'course-a'}},
        )
        mock_partition_courses.return_value = (['course-a', 'course-b'], ['course-skipped'])
        mock_partition_programs.return_value = (['program-p'], [])
        mock_content_metadata.objects.filter.return_value.values_list.return_value = ['pathway-x']

        result = mappings_module._compute_indexing_mappings()

        self.assertEqual(result.program_to_course_keys, {'program-p': {'course-a', 'course-b'}})
        self.assertEqual(result.pathway_to_program_course_keys, {'pathway-x': {'program-p', 'course-a'}})
        self.assertEqual(
            result.all_indexable_content_keys,
            {'course-a', 'course-b', 'program-p', 'pathway-x'},
        )
        # Nonindexable courses must NOT be in the indexable set.
        self.assertNotIn('course-skipped', result.all_indexable_content_keys)
