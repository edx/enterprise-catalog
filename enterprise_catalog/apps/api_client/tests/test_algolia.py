"""
Tests for the Algolia API client.
"""
from unittest import mock

import ddt
from algoliasearch.exceptions import AlgoliaException
from django.test import TestCase, override_settings

from enterprise_catalog.apps.api_client.algolia import AlgoliaSearchClient


ALGOLIA_SETTINGS = {
    'APPLICATION_ID': 'test-app-id',
    'API_KEY': 'test-api-key',
    'SEARCH_API_KEY': 'test-search-key',
    'INDEX_NAME': 'test-index',
    'REPLICA_INDEX_NAME': 'test-replica-index',
}


@ddt.ddt
@override_settings(ALGOLIA=ALGOLIA_SETTINGS)
class AlgoliaSearchClientBatchMethodsTests(TestCase):
    """
    Tests for the new batch methods on AlgoliaSearchClient.
    """
    # pylint: disable=protected-access

    def setUp(self):
        self.client = AlgoliaSearchClient()

    @mock.patch('enterprise_catalog.apps.api_client.algolia.SearchClient')
    def test_save_objects_batch_success(self, mock_search_client):
        """
        Test that save_objects_batch saves objects successfully.
        """
        mock_index = mock.MagicMock()
        mock_index.save_objects.return_value = {'objectIDs': ['obj-1', 'obj-2']}
        mock_search_client.create.return_value.init_index.return_value = mock_index

        self.client.init_index()
        self.client.algolia_index = mock_index

        objects = [
            {'objectID': 'obj-1', 'title': 'Test 1'},
            {'objectID': 'obj-2', 'title': 'Test 2'},
        ]

        result = self.client.save_objects_batch(objects)

        self.assertEqual(result, {'objectIDs': ['obj-1', 'obj-2']})
        mock_index.save_objects.assert_called_once_with(
            objects,
            {'autoGenerateObjectIDIfNotExist': False},
        )

    @mock.patch('enterprise_catalog.apps.api_client.algolia.SearchClient')
    def test_save_objects_batch_empty_list(self, mock_search_client):
        """
        Test that save_objects_batch handles empty list gracefully.
        """
        mock_index = mock.MagicMock()
        mock_search_client.create.return_value.init_index.return_value = mock_index

        self.client.init_index()
        self.client.algolia_index = mock_index

        result = self.client.save_objects_batch([])

        self.assertIsNone(result)
        mock_index.save_objects.assert_not_called()

    @mock.patch('enterprise_catalog.apps.api_client.algolia.SearchClient')
    def test_save_objects_batch_with_custom_index(self, mock_search_client):
        """
        Test that save_objects_batch can use a custom index name.
        """
        mock_index = mock.MagicMock()
        mock_custom_index = mock.MagicMock()
        mock_custom_index.save_objects.return_value = {'objectIDs': ['obj-1']}

        mock_client = mock.MagicMock()
        mock_client.init_index.side_effect = lambda name: (
            mock_custom_index if name == 'custom-index' else mock_index
        )
        mock_search_client.create.return_value = mock_client

        self.client.init_index()
        self.client._client = mock_client

        objects = [{'objectID': 'obj-1', 'title': 'Test'}]
        result = self.client.save_objects_batch(objects, index_name='custom-index')

        self.assertEqual(result, {'objectIDs': ['obj-1']})
        mock_custom_index.save_objects.assert_called_once()

    @mock.patch('enterprise_catalog.apps.api_client.algolia.SearchClient')
    def test_save_objects_batch_algolia_exception(self, mock_search_client):
        """
        Test that save_objects_batch raises AlgoliaException on failure.
        """
        mock_index = mock.MagicMock()
        mock_index.save_objects.side_effect = AlgoliaException('Save failed')
        mock_search_client.create.return_value.init_index.return_value = mock_index

        self.client.init_index()
        self.client.algolia_index = mock_index

        with self.assertRaises(AlgoliaException):
            self.client.save_objects_batch([{'objectID': 'obj-1'}])

    @mock.patch('enterprise_catalog.apps.api_client.algolia.SearchClient')
    def test_delete_objects_batch_success(self, mock_search_client):
        """
        Test that delete_objects_batch deletes objects successfully.
        """
        mock_index = mock.MagicMock()
        mock_index.delete_objects.return_value = {'objectIDs': ['obj-1', 'obj-2']}
        mock_search_client.create.return_value.init_index.return_value = mock_index

        self.client.init_index()
        self.client.algolia_index = mock_index

        object_ids = ['obj-1', 'obj-2']
        result = self.client.delete_objects_batch(object_ids)

        self.assertEqual(result, {'objectIDs': ['obj-1', 'obj-2']})
        mock_index.delete_objects.assert_called_once_with(object_ids)

    @mock.patch('enterprise_catalog.apps.api_client.algolia.SearchClient')
    def test_delete_objects_batch_empty_list(self, mock_search_client):
        """
        Test that delete_objects_batch handles empty list gracefully.
        """
        mock_index = mock.MagicMock()
        mock_search_client.create.return_value.init_index.return_value = mock_index

        self.client.init_index()
        self.client.algolia_index = mock_index

        result = self.client.delete_objects_batch([])

        self.assertIsNone(result)
        mock_index.delete_objects.assert_not_called()

    @mock.patch('enterprise_catalog.apps.api_client.algolia.SearchClient')
    def test_delete_objects_batch_algolia_exception(self, mock_search_client):
        """
        Test that delete_objects_batch raises AlgoliaException on failure.
        """
        mock_index = mock.MagicMock()
        mock_index.delete_objects.side_effect = AlgoliaException('Delete failed')
        mock_search_client.create.return_value.init_index.return_value = mock_index

        self.client.init_index()
        self.client.algolia_index = mock_index

        with self.assertRaises(AlgoliaException):
            self.client.delete_objects_batch(['obj-1'])

    @mock.patch('enterprise_catalog.apps.api_client.algolia.SearchClient')
    def test_get_object_ids_by_prefix_success(self, mock_search_client):
        """
        Test that get_object_ids_by_prefix returns matching object IDs.
        """
        mock_index = mock.MagicMock()
        mock_index.browse_objects.return_value = [
            {'objectID': 'course:edX+Demo-0'},
            {'objectID': 'course:edX+Demo-1'},
            {'objectID': 'course:edX+Demo-2'},
        ]
        mock_search_client.create.return_value.init_index.return_value = mock_index

        self.client.init_index()
        self.client.algolia_index = mock_index

        result = self.client.get_object_ids_by_prefix('course:edX+Demo')

        self.assertEqual(result, [
            'course:edX+Demo-0',
            'course:edX+Demo-1',
            'course:edX+Demo-2',
        ])
        mock_index.browse_objects.assert_called_once_with({
            'attributesToRetrieve': ['objectID'],
            'filters': "aggregation_key:'course:edX+Demo'"
        })

    @mock.patch('enterprise_catalog.apps.api_client.algolia.SearchClient')
    def test_get_object_ids_by_prefix_no_matches(self, mock_search_client):
        """
        Test that get_object_ids_by_prefix returns empty list when no matches.
        """
        mock_index = mock.MagicMock()
        mock_index.browse_objects.return_value = []
        mock_search_client.create.return_value.init_index.return_value = mock_index

        self.client.init_index()
        self.client.algolia_index = mock_index

        result = self.client.get_object_ids_by_prefix('nonexistent')

        self.assertEqual(result, [])

    @mock.patch('enterprise_catalog.apps.api_client.algolia.SearchClient')
    def test_get_object_ids_by_prefix_algolia_exception(self, mock_search_client):
        """
        Test that get_object_ids_by_prefix raises AlgoliaException on failure.
        """
        mock_index = mock.MagicMock()
        mock_index.browse_objects.side_effect = AlgoliaException('Browse failed')
        mock_search_client.create.return_value.init_index.return_value = mock_index

        self.client.init_index()
        self.client.algolia_index = mock_index

        with self.assertRaises(AlgoliaException):
            self.client.get_object_ids_by_prefix('test')

    @mock.patch('enterprise_catalog.apps.api_client.algolia.SearchClient')
    def test_get_index_returns_primary_index_by_default(self, mock_search_client):
        """
        Test that _get_index returns the primary index when no name is provided.
        """
        mock_index = mock.MagicMock()
        mock_search_client.create.return_value.init_index.return_value = mock_index

        self.client.init_index()
        self.client.algolia_index = mock_index

        result = self.client._get_index()

        self.assertEqual(result, mock_index)

    @mock.patch('enterprise_catalog.apps.api_client.algolia.SearchClient')
    def test_get_index_creates_custom_index(self, mock_search_client):
        """
        Test that _get_index creates and returns a custom index when name is provided.
        """
        mock_primary_index = mock.MagicMock()
        mock_custom_index = mock.MagicMock()

        mock_client = mock.MagicMock()
        mock_client.init_index.side_effect = lambda name: (
            mock_custom_index if name == 'custom-index' else mock_primary_index
        )
        mock_search_client.create.return_value = mock_client

        self.client.init_index()
        self.client._client = mock_client

        result = self.client._get_index('custom-index')

        self.assertEqual(result, mock_custom_index)
        mock_client.init_index.assert_called_with('custom-index')

    def test_get_index_returns_none_without_client(self):
        """
        Test that _get_index returns None when client is not initialized.
        """
        result = self.client._get_index('some-index')
        self.assertIsNone(result)
