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
    _batch_content_keys,
    _filter_content_keys_for_indexing,
    _generate_algolia_objects_for_content,
    _get_algolia_client,
    _get_stale_content_keys,
    _index_content_batch,
    _index_single_content_item,
    _mark_content_as_failed,
    dispatch_algolia_indexing,
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


@ddt.ddt
@override_settings(ALGOLIA=ALGOLIA_SETTINGS)
class BatchContentKeysTests(TestCase):
    """
    Tests for _batch_content_keys helper function.
    """

    def test_batch_content_keys_default_size(self):
        """
        Test batching with default batch size.
        """
        content_keys = [f'key-{i}' for i in range(25)]
        batches = list(_batch_content_keys(content_keys))

        # Default batch size is 10
        self.assertEqual(len(batches), 3)
        self.assertEqual(len(batches[0]), 10)
        self.assertEqual(len(batches[1]), 10)
        self.assertEqual(len(batches[2]), 5)

    def test_batch_content_keys_custom_size(self):
        """
        Test batching with custom batch size.
        """
        content_keys = [f'key-{i}' for i in range(7)]
        batches = list(_batch_content_keys(content_keys, batch_size=3))

        self.assertEqual(len(batches), 3)
        self.assertEqual(len(batches[0]), 3)
        self.assertEqual(len(batches[1]), 3)
        self.assertEqual(len(batches[2]), 1)

    def test_batch_content_keys_empty(self):
        """
        Test batching with empty list.
        """
        batches = list(_batch_content_keys([]))
        self.assertEqual(batches, [])


@ddt.ddt
@override_settings(ALGOLIA=ALGOLIA_SETTINGS)
class GetStaleContentKeysTests(TestCase):
    """
    Tests for _get_stale_content_keys helper function.
    """

    def test_get_stale_content_keys_force_mode(self):
        """
        Test that force mode returns all content keys.
        """
        # Create some content
        factories.ContentMetadataFactory(
            content_key='course:edX+Force1',
            content_type=COURSE,
        )
        factories.ContentMetadataFactory(
            content_key='course:edX+Force2',
            content_type=COURSE,
        )

        content_keys = _get_stale_content_keys(COURSE, force=True)

        self.assertIn('course:edX+Force1', content_keys)
        self.assertIn('course:edX+Force2', content_keys)

    def test_get_stale_content_keys_never_indexed(self):
        """
        Test that never-indexed content is included.
        """
        factories.ContentMetadataFactory(
            content_key='course:edX+NeverIndexed',
            content_type=COURSE,
        )
        # No indexing state exists

        content_keys = _get_stale_content_keys(COURSE, force=False)

        self.assertIn('course:edX+NeverIndexed', content_keys)

    def test_get_stale_content_keys_stale_content(self):
        """
        Test that stale content (modified since indexed) is included.
        """
        content = factories.ContentMetadataFactory(
            content_key='course:edX+Stale',
            content_type=COURSE,
        )
        # Create state with old indexed time
        ContentMetadataIndexingState.objects.create(
            content_metadata=content,
            last_indexed_at=timezone.now() - timezone.timedelta(hours=2),
        )
        # Update content modified time (simulating content change)
        content.modified = timezone.now()
        content.save()

        content_keys = _get_stale_content_keys(COURSE, force=False)

        self.assertIn('course:edX+Stale', content_keys)

    def test_get_stale_content_keys_fresh_content_excluded(self):
        """
        Test that fresh content (indexed after modified) is excluded.
        """
        content = factories.ContentMetadataFactory(
            content_key='course:edX+Fresh',
            content_type=COURSE,
        )
        # Create state with recent indexed time
        ContentMetadataIndexingState.objects.create(
            content_metadata=content,
            last_indexed_at=timezone.now(),
        )

        content_keys = _get_stale_content_keys(COURSE, force=False)

        self.assertNotIn('course:edX+Fresh', content_keys)

    def test_get_stale_content_keys_failed_content_included(self):
        """
        Test that failed content is included for retry.
        """
        content = factories.ContentMetadataFactory(
            content_key='course:edX+Failed',
            content_type=COURSE,
        )
        # Create state with failure
        ContentMetadataIndexingState.objects.create(
            content_metadata=content,
            last_indexed_at=timezone.now(),
            last_failure_at=timezone.now(),
            failure_reason='Previous failure',
        )

        content_keys = _get_stale_content_keys(COURSE, force=False, include_failed=True)

        self.assertIn('course:edX+Failed', content_keys)

    def test_get_stale_content_keys_failed_content_excluded(self):
        """
        Test that failed content is excluded when include_failed=False.
        """
        content = factories.ContentMetadataFactory(
            content_key='course:edX+FailedExclude',
            content_type=COURSE,
        )
        # Create state with failure but fresh index
        ContentMetadataIndexingState.objects.create(
            content_metadata=content,
            last_indexed_at=timezone.now(),
            last_failure_at=timezone.now(),
            failure_reason='Previous failure',
        )

        content_keys = _get_stale_content_keys(COURSE, force=False, include_failed=False)

        self.assertNotIn('course:edX+FailedExclude', content_keys)


@ddt.ddt
@override_settings(ALGOLIA=ALGOLIA_SETTINGS)
class DispatchAlgoliaIndexingTests(TestCase):
    """
    Tests for dispatch_algolia_indexing task.
    """

    @mock.patch('enterprise_catalog.apps.search.tasks.index_courses_batch_in_algolia')
    @mock.patch('enterprise_catalog.apps.search.tasks._get_stale_content_keys')
    def test_dispatch_single_content_type(self, mock_get_stale, mock_task):
        """
        Test dispatching for a single content type.
        """
        mock_get_stale.return_value = ['course:edX+Test1', 'course:edX+Test2']

        result = dispatch_algolia_indexing.run(
            content_type=COURSE,
            force=False,
        )

        mock_get_stale.assert_called_once_with(COURSE, force=False)
        mock_task.delay.assert_called_once_with(
            content_keys=['course:edX+Test1', 'course:edX+Test2'],
            index_name=None,
            force=False,
        )
        self.assertEqual(result['dispatched_tasks'], 1)

    @mock.patch('enterprise_catalog.apps.search.tasks.index_pathways_batch_in_algolia')
    @mock.patch('enterprise_catalog.apps.search.tasks.index_programs_batch_in_algolia')
    @mock.patch('enterprise_catalog.apps.search.tasks.index_courses_batch_in_algolia')
    @mock.patch('enterprise_catalog.apps.search.tasks._get_stale_content_keys')
    def test_dispatch_all_content_types(
        self, mock_get_stale, mock_courses_task, mock_programs_task, mock_pathways_task
    ):
        """
        Test dispatching for all content types when none specified.
        """
        mock_get_stale.side_effect = [
            ['course1', 'course2'],  # Courses
            ['program1'],  # Programs
            [],  # Pathways (empty)
        ]

        result = dispatch_algolia_indexing.run(force=True)

        # Should query for all three types
        self.assertEqual(mock_get_stale.call_count, 3)

        # Should dispatch for courses and programs, not pathways
        mock_courses_task.delay.assert_called_once()
        mock_programs_task.delay.assert_called_once()
        mock_pathways_task.delay.assert_not_called()

        self.assertEqual(result['dispatched_tasks'], 2)

    @mock.patch('enterprise_catalog.apps.search.tasks.index_courses_batch_in_algolia')
    @mock.patch('enterprise_catalog.apps.search.tasks._get_stale_content_keys')
    def test_dispatch_with_batching(self, mock_get_stale, mock_task):
        """
        Test that large content key lists are batched properly.
        """
        # Return 25 content keys (should create 3 batches with default size of 10)
        mock_get_stale.return_value = [f'course{i}' for i in range(25)]

        result = dispatch_algolia_indexing.run(content_type=COURSE)

        # Should dispatch 3 batches
        self.assertEqual(mock_task.delay.call_count, 3)
        self.assertEqual(result['dispatched_tasks'], 3)
        self.assertEqual(result['content_types'][COURSE]['total_content_keys'], 25)
        self.assertEqual(result['content_types'][COURSE]['batches_dispatched'], 3)

    @mock.patch('enterprise_catalog.apps.search.tasks.index_courses_batch_in_algolia')
    @mock.patch('enterprise_catalog.apps.search.tasks._get_stale_content_keys')
    def test_dispatch_dry_run(self, mock_get_stale, mock_task):
        """
        Test that dry_run mode logs but doesn't dispatch tasks.
        """
        mock_get_stale.return_value = ['course:edX+DryRun']

        result = dispatch_algolia_indexing.run(
            content_type=COURSE,
            dry_run=True,
        )

        # Should not dispatch any tasks
        mock_task.delay.assert_not_called()
        # But should still count as if it would have
        self.assertEqual(result['dispatched_tasks'], 1)

    @mock.patch('enterprise_catalog.apps.search.tasks.index_courses_batch_in_algolia')
    @mock.patch('enterprise_catalog.apps.search.tasks._get_stale_content_keys')
    def test_dispatch_with_custom_index_name(self, mock_get_stale, mock_task):
        """
        Test that custom index name is passed to batch tasks.
        """
        mock_get_stale.return_value = ['course:edX+Custom']

        dispatch_algolia_indexing.run(
            content_type=COURSE,
            index_name='v2-test-index',
        )

        mock_task.delay.assert_called_once_with(
            content_keys=['course:edX+Custom'],
            index_name='v2-test-index',
            force=False,
        )

    @mock.patch('enterprise_catalog.apps.search.tasks._get_stale_content_keys')
    def test_dispatch_no_stale_content(self, mock_get_stale):
        """
        Test dispatch with no stale content.
        """
        mock_get_stale.return_value = []

        result = dispatch_algolia_indexing.run(content_type=COURSE)

        self.assertEqual(result['dispatched_tasks'], 0)
        self.assertEqual(result['content_types'][COURSE]['total_content_keys'], 0)
