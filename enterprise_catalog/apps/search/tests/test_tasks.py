"""
Tests for search app tasks.
"""
from unittest import mock

import ddt
from django.test import TestCase, override_settings
from django.utils import timezone

from enterprise_catalog.apps.catalog.constants import (
    COURSE,
    LEARNER_PATHWAY,
    PROGRAM,
)
from enterprise_catalog.apps.catalog.tests import factories
from enterprise_catalog.apps.search.models import ContentMetadataIndexingState
from enterprise_catalog.apps.search.tasks import (
    _filter_content_keys_for_indexing,
    _generate_algolia_objects_for_content,
    _get_algolia_client,
    _index_content_batch,
    _index_single_content_item,
    _mark_content_as_failed,
    index_courses_batch_in_algolia,
    index_pathways_batch_in_algolia,
    index_programs_batch_in_algolia,
)


ALGOLIA_SETTINGS = {
    'APPLICATION_ID': 'test-app-id',
    'API_KEY': 'test-api-key',
    'SEARCH_API_KEY': 'test-search-key',
    'INDEX_NAME': 'test-index',
    'REPLICA_INDEX_NAME': 'test-replica-index',
}


@ddt.ddt
@override_settings(ALGOLIA=ALGOLIA_SETTINGS)
class GetAlgoliaClientTests(TestCase):
    """
    Tests for _get_algolia_client helper function.
    """

    @mock.patch('enterprise_catalog.apps.search.tasks.AlgoliaSearchClient')
    def test_get_algolia_client_success(self, mock_client_class):
        """
        Test successful client initialization.
        """
        mock_client = mock.MagicMock()
        mock_client.algolia_index = mock.MagicMock()
        mock_client_class.return_value = mock_client

        result = _get_algolia_client()

        self.assertEqual(result, mock_client)
        mock_client.init_index.assert_called_once()

    @mock.patch('enterprise_catalog.apps.search.tasks.AlgoliaSearchClient')
    def test_get_algolia_client_failure(self, mock_client_class):
        """
        Test client initialization failure.
        """
        mock_client = mock.MagicMock()
        mock_client.algolia_index = None
        mock_client_class.return_value = mock_client

        result = _get_algolia_client()

        self.assertIsNone(result)


@ddt.ddt
@override_settings(ALGOLIA=ALGOLIA_SETTINGS)
class FilterContentKeysForIndexingTests(TestCase):
    """
    Tests for _filter_content_keys_for_indexing helper function.
    """

    def test_filter_nonexistent_content(self):
        """
        Test that non-existent content keys are skipped.
        """
        results = {'indexed': 0, 'skipped': 0, 'failed': 0}
        content_keys = ['nonexistent-key-1', 'nonexistent-key-2']

        filtered = _filter_content_keys_for_indexing(
            content_keys, COURSE, force=False, results=results
        )

        self.assertEqual(filtered, [])
        self.assertEqual(results['skipped'], 2)

    def test_filter_already_indexed_content(self):
        """
        Test that already-indexed content is skipped when not forced.
        """
        content = factories.ContentMetadataFactory(
            content_key='course:edX+Test',
            content_type=COURSE,
        )
        ContentMetadataIndexingState.objects.create(
            content_metadata=content,
            last_indexed_at=timezone.now(),  # Indexed after creation
        )

        results = {'indexed': 0, 'skipped': 0, 'failed': 0}

        filtered = _filter_content_keys_for_indexing(
            ['course:edX+Test'], COURSE, force=False, results=results
        )

        self.assertEqual(filtered, [])
        self.assertEqual(results['skipped'], 1)

    def test_filter_stale_content_included(self):
        """
        Test that stale content is included for indexing.
        """
        content = factories.ContentMetadataFactory(
            content_key='course:edX+Stale',
            content_type=COURSE,
        )
        # Content is stale - never indexed
        ContentMetadataIndexingState.objects.create(
            content_metadata=content,
            last_indexed_at=None,
        )

        results = {'indexed': 0, 'skipped': 0, 'failed': 0}

        filtered = _filter_content_keys_for_indexing(
            ['course:edX+Stale'], COURSE, force=False, results=results
        )

        self.assertEqual(filtered, ['course:edX+Stale'])
        self.assertEqual(results['skipped'], 0)

    def test_filter_force_includes_all(self):
        """
        Test that force=True includes content even if already indexed.
        """
        content = factories.ContentMetadataFactory(
            content_key='course:edX+Force',
            content_type=COURSE,
        )
        ContentMetadataIndexingState.objects.create(
            content_metadata=content,
            last_indexed_at=timezone.now(),
        )

        results = {'indexed': 0, 'skipped': 0, 'failed': 0}

        filtered = _filter_content_keys_for_indexing(
            ['course:edX+Force'], COURSE, force=True, results=results
        )

        self.assertEqual(filtered, ['course:edX+Force'])
        self.assertEqual(results['skipped'], 0)


@ddt.ddt
@override_settings(ALGOLIA=ALGOLIA_SETTINGS)
class MarkContentAsFailedTests(TestCase):
    """
    Tests for _mark_content_as_failed helper function.
    """

    def test_mark_as_failed_success(self):
        """
        Test marking existing content as failed.
        """
        content = factories.ContentMetadataFactory(
            content_key='course:edX+Failed',
            content_type=COURSE,
        )

        _mark_content_as_failed('course:edX+Failed', 'Test failure reason')

        state = ContentMetadataIndexingState.objects.get(content_metadata=content)
        self.assertIsNotNone(state.last_failure_at)
        self.assertEqual(state.failure_reason, 'Test failure reason')

    def test_mark_as_failed_nonexistent(self):
        """
        Test marking non-existent content as failed (should not raise).
        """
        # Should not raise an exception
        _mark_content_as_failed('nonexistent-key', 'Test failure')


@ddt.ddt
@override_settings(ALGOLIA=ALGOLIA_SETTINGS)
class GenerateAlgoliaObjectsTests(TestCase):
    """
    Tests for _generate_algolia_objects_for_content helper function.
    """

    def test_generate_empty_keys(self):
        """
        Test that empty content keys returns empty list.
        """
        result = _generate_algolia_objects_for_content([], COURSE)
        self.assertEqual(result, [])

    @mock.patch('enterprise_catalog.apps.search.tasks._get_algolia_products_for_batch')
    @mock.patch('enterprise_catalog.apps.search.tasks._precalculate_content_mappings')
    def test_generate_algolia_objects(self, mock_precalculate, mock_get_products):
        """
        Test generating Algolia objects for content keys.
        """
        mock_precalculate.return_value = ({}, {})
        mock_get_products.return_value = [
            {'objectID': 'course:edX+Test-0', 'aggregation_key': 'course:edX+Test'},
            {'objectID': 'course:edX+Test-1', 'aggregation_key': 'course:edX+Test'},
        ]

        result = _generate_algolia_objects_for_content(['course:edX+Test'], COURSE)

        self.assertEqual(len(result), 2)
        mock_get_products.assert_called_once()


@ddt.ddt
@override_settings(ALGOLIA=ALGOLIA_SETTINGS)
class IndexSingleContentItemTests(TestCase):
    """
    Tests for _index_single_content_item helper function.
    """

    def test_index_single_content_item_success(self):
        """
        Test successfully indexing a single content item.
        """
        content = factories.ContentMetadataFactory(
            content_key='course:edX+Single',
            content_type=COURSE,
        )

        mock_client = mock.MagicMock()
        mock_client.get_object_ids_by_prefix.return_value = []

        objects_by_key = {
            'course:edX+Single': [
                {'objectID': 'course:edX+Single-0', 'title': 'Test'},
            ]
        }

        _index_single_content_item(
            mock_client, 'course:edX+Single', COURSE, objects_by_key, None
        )

        mock_client.save_objects_batch.assert_called_once()
        state = ContentMetadataIndexingState.objects.get(content_metadata=content)
        self.assertIsNotNone(state.last_indexed_at)
        self.assertEqual(state.algolia_object_ids, ['course:edX+Single-0'])

    def test_index_single_content_item_with_orphans(self):
        """
        Test indexing deletes orphaned shards.
        """
        factories.ContentMetadataFactory(
            content_key='course:edX+Orphan',
            content_type=COURSE,
        )

        mock_client = mock.MagicMock()
        mock_client.get_object_ids_by_prefix.return_value = [
            'course:edX+Orphan-0',
            'course:edX+Orphan-1',
            'course:edX+Orphan-2',  # This one will be orphaned
        ]

        objects_by_key = {
            'course:edX+Orphan': [
                {'objectID': 'course:edX+Orphan-0'},
                {'objectID': 'course:edX+Orphan-1'},
            ]
        }

        _index_single_content_item(
            mock_client, 'course:edX+Orphan', COURSE, objects_by_key, None
        )

        mock_client.delete_objects_batch.assert_called_once()
        deleted_ids = mock_client.delete_objects_batch.call_args[0][0]
        self.assertEqual(deleted_ids, ['course:edX+Orphan-2'])


@ddt.ddt
@override_settings(ALGOLIA=ALGOLIA_SETTINGS)
class IndexContentBatchTests(TestCase):
    """
    Tests for _index_content_batch function.
    """

    def test_index_content_batch_empty_keys(self):
        """
        Test indexing with empty content keys.
        """
        results = _index_content_batch([], COURSE)

        self.assertEqual(results['indexed'], 0)
        self.assertEqual(results['skipped'], 0)
        self.assertEqual(results['failed'], 0)

    @mock.patch('enterprise_catalog.apps.search.tasks._get_algolia_client')
    def test_index_content_batch_client_failure(self, mock_get_client):
        """
        Test indexing when Algolia client fails to initialize.
        """
        mock_get_client.return_value = None

        content = factories.ContentMetadataFactory(
            content_key='course:edX+ClientFail',
            content_type=COURSE,
        )

        results = _index_content_batch(['course:edX+ClientFail'], COURSE)

        self.assertEqual(results['failed'], 1)
        state = ContentMetadataIndexingState.objects.get(content_metadata=content)
        self.assertIsNotNone(state.last_failure_at)

    @mock.patch('enterprise_catalog.apps.search.tasks.configure_algolia_index')
    @mock.patch('enterprise_catalog.apps.search.tasks._generate_algolia_objects_for_content')
    @mock.patch('enterprise_catalog.apps.search.tasks._get_algolia_client')
    def test_index_content_batch_success(self, mock_get_client, mock_generate, _mock_configure):
        """
        Test successful batch indexing.
        """
        mock_client = mock.MagicMock()
        mock_client.get_object_ids_by_prefix.return_value = []
        mock_get_client.return_value = mock_client

        mock_generate.return_value = [
            {'objectID': 'course:edX+Batch-0', 'aggregation_key': 'course:edX+Batch'},
        ]

        content = factories.ContentMetadataFactory(
            content_key='course:edX+Batch',
            content_type=COURSE,
        )

        results = _index_content_batch(['course:edX+Batch'], COURSE)

        self.assertEqual(results['indexed'], 1)
        self.assertEqual(results['skipped'], 0)
        self.assertEqual(results['failed'], 0)

        state = ContentMetadataIndexingState.objects.get(content_metadata=content)
        self.assertIsNotNone(state.last_indexed_at)

    @mock.patch('enterprise_catalog.apps.search.tasks.configure_algolia_index')
    @mock.patch('enterprise_catalog.apps.search.tasks._generate_algolia_objects_for_content')
    @mock.patch('enterprise_catalog.apps.search.tasks._get_algolia_client')
    def test_index_content_batch_generation_failure(self, mock_get_client, mock_generate, _mock_configure):
        """
        Test batch indexing when object generation fails.
        """
        mock_client = mock.MagicMock()
        mock_get_client.return_value = mock_client
        mock_generate.side_effect = Exception('Generation failed')

        content = factories.ContentMetadataFactory(
            content_key='course:edX+GenFail',
            content_type=COURSE,
        )

        results = _index_content_batch(['course:edX+GenFail'], COURSE)

        self.assertEqual(results['failed'], 1)
        state = ContentMetadataIndexingState.objects.get(content_metadata=content)
        self.assertIn('Generation failed', state.failure_reason)


@ddt.ddt
@override_settings(ALGOLIA=ALGOLIA_SETTINGS)
class IndexTasksTests(TestCase):
    """
    Tests for the Celery task entry points.
    """

    @mock.patch('enterprise_catalog.apps.search.tasks._index_content_batch')
    def test_index_courses_batch_task(self, mock_index_batch):
        """
        Test index_courses_batch_in_algolia task.
        """
        mock_index_batch.return_value = {'indexed': 1, 'skipped': 0, 'failed': 0}

        # Call the underlying function directly (bypass Celery's bind=True)
        result = index_courses_batch_in_algolia.run(
            content_keys=['course:edX+Test'],
            index_name='custom-index',
            force=True,
        )

        mock_index_batch.assert_called_once_with(
            content_keys=['course:edX+Test'],
            content_type=COURSE,
            index_name='custom-index',
            force=True,
        )
        self.assertEqual(result['indexed'], 1)

    @mock.patch('enterprise_catalog.apps.search.tasks._index_content_batch')
    def test_index_programs_batch_task(self, mock_index_batch):
        """
        Test index_programs_batch_in_algolia task.
        """
        mock_index_batch.return_value = {'indexed': 1, 'skipped': 0, 'failed': 0}

        # Call the underlying function directly (bypass Celery's bind=True)
        result = index_programs_batch_in_algolia.run(
            content_keys=['program-uuid-123'],
        )

        mock_index_batch.assert_called_once_with(
            content_keys=['program-uuid-123'],
            content_type=PROGRAM,
            index_name=None,
            force=False,
        )
        self.assertEqual(result['indexed'], 1)

    @mock.patch('enterprise_catalog.apps.search.tasks._index_content_batch')
    def test_index_pathways_batch_task(self, mock_index_batch):
        """
        Test index_pathways_batch_in_algolia task.
        """
        mock_index_batch.return_value = {'indexed': 2, 'skipped': 1, 'failed': 0}

        # Call the underlying function directly (bypass Celery's bind=True)
        result = index_pathways_batch_in_algolia.run(
            content_keys=['pathway-1', 'pathway-2', 'pathway-3'],
            force=True,
        )

        mock_index_batch.assert_called_once_with(
            content_keys=['pathway-1', 'pathway-2', 'pathway-3'],
            content_type=LEARNER_PATHWAY,
            index_name=None,
            force=True,
        )
        self.assertEqual(result['indexed'], 2)
        self.assertEqual(result['skipped'], 1)
