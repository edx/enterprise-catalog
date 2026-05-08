"""
Tests for the AlgoliaSearchClient batch methods.
"""
from unittest import mock

import ddt
from algoliasearch.exceptions import AlgoliaException
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase

from enterprise_catalog.apps.api_client.algolia import AlgoliaSearchClient


@ddt.ddt
class TestAlgoliaSearchClientBatchMethods(TestCase):
    """
    Tests for ``save_objects_batch``, ``delete_objects_batch``,
    ``get_object_ids_for_aggregation_key``, ``get_aggregation_keys_for_catalog_query``,
    and the ``_get_index`` helper.
    """
    # pylint: disable=protected-access
    PRIMARY_INDEX_NAME = 'enterprise_catalog'
    ALT_INDEX_NAME = 'enterprise_catalog_v2'

    def _build_client(self):
        """
        Build an ``AlgoliaSearchClient`` with stubbed-out internal state so we don't have
        to call ``init_index()`` (which requires real settings + network).
        """
        client = AlgoliaSearchClient()
        client._client = mock.MagicMock()
        client.algolia_index = mock.MagicMock(name='primary_index')
        client.replica_index = mock.MagicMock(name='replica_index')
        # Patch the property to return our test index name.
        patcher = mock.patch.object(
            AlgoliaSearchClient,
            'algolia_index_name',
            new_callable=mock.PropertyMock,
            return_value=self.PRIMARY_INDEX_NAME,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return client

    def test_get_index_defaults_to_primary(self):
        """
        ``_get_index()`` returns the primary index when no name is given.
        """
        client = self._build_client()
        self.assertIs(client._get_index(), client.algolia_index)

    def test_get_index_returns_primary_when_name_matches(self):
        """
        Passing the primary index name returns the cached primary index (no extra init).
        """
        client = self._build_client()
        result = client._get_index(self.PRIMARY_INDEX_NAME)
        self.assertIs(result, client.algolia_index)
        client._client.init_index.assert_not_called()

    def test_get_index_initializes_alternate_index(self):
        """
        Passing a different name triggers ``client.init_index(name)`` for that name.
        """
        client = self._build_client()
        alt_index = mock.MagicMock(name='alt_index')
        client._client.init_index.return_value = alt_index

        result = client._get_index(self.ALT_INDEX_NAME)

        self.assertIs(result, alt_index)
        client._client.init_index.assert_called_once_with(self.ALT_INDEX_NAME)

    def test_get_index_raises_when_client_uninitialized_for_alternate(self):
        """
        Asking for an alternate index before ``init_index()`` raises ImproperlyConfigured.
        """
        client = AlgoliaSearchClient()  # Not built via _build_client; nothing initialized.
        with self.assertRaises(ImproperlyConfigured):
            client._get_index(self.ALT_INDEX_NAME)

    def test_get_index_raises_when_primary_uninitialized(self):
        """
        Asking for the default (primary) index before init raises ImproperlyConfigured.
        """
        client = AlgoliaSearchClient()
        with self.assertRaises(ImproperlyConfigured):
            client._get_index()

    def test_save_objects_batch_calls_save_on_primary(self):
        """
        ``save_objects_batch`` delegates to ``save_objects`` on the primary index by default.
        """
        client = self._build_client()
        objects = [{'objectID': 'course-abc-catalog-query-uuids-0'}]

        client.save_objects_batch(objects)

        client.algolia_index.save_objects.assert_called_once_with(objects)

    def test_save_objects_batch_chunks_into_multiple_calls(self):
        """
        With ``chunk_size`` smaller than the input length, the call is split
        across multiple ``save_objects`` invocations on the index — each one
        independently raises on its own failure, which is the property that
        makes the bulk-with-fallback path in the search tasks work.
        """
        client = self._build_client()
        objects = [{'objectID': f'shard-{i}'} for i in range(5)]

        client.save_objects_batch(objects, chunk_size=2)

        self.assertEqual(client.algolia_index.save_objects.call_count, 3)
        chunks = [call.args[0] for call in client.algolia_index.save_objects.call_args_list]
        # All input objects accounted for, in order, across 2-2-1 chunks.
        self.assertEqual(
            [obj['objectID'] for chunk in chunks for obj in chunk],
            [obj['objectID'] for obj in objects],
        )
        self.assertEqual([len(chunk) for chunk in chunks], [2, 2, 1])

    def test_save_objects_batch_noop_when_no_objects(self):
        """
        Empty input returns early without calling Algolia.
        """
        client = self._build_client()
        client.save_objects_batch([])
        client.algolia_index.save_objects.assert_not_called()

    def test_save_objects_batch_uses_setting_default_chunk_size(self):
        """
        ``chunk_size=None`` (the default) reads from the Django setting; the
        request fits in one chunk at the configured size.
        """
        client = self._build_client()
        objects = [{'objectID': f'shard-{i}'} for i in range(50)]

        with self.settings(ALGOLIA_INDEXING_CHUNK_SIZE=100):
            client.save_objects_batch(objects)

        client.algolia_index.save_objects.assert_called_once()
        self.assertEqual(len(client.algolia_index.save_objects.call_args.args[0]), 50)

    def test_save_objects_batch_targets_alternate_index(self):
        """
        With ``index_name`` set, ``save_objects_batch`` writes to that index instead.
        """
        client = self._build_client()
        alt_index = mock.MagicMock(name='alt_index')
        client._client.init_index.return_value = alt_index
        objects = [{'objectID': 'course-abc-catalog-query-uuids-0'}]

        client.save_objects_batch(objects, index_name=self.ALT_INDEX_NAME)

        alt_index.save_objects.assert_called_once_with(objects)
        client.algolia_index.save_objects.assert_not_called()

    def test_save_objects_batch_raises_when_index_unavailable(self):
        """
        With no initialized index, the method raises ImproperlyConfigured rather than
        silently no-op'ing — silent success would let batch tasks falsely mark records
        as indexed.
        """
        client = AlgoliaSearchClient()
        with self.assertRaises(ImproperlyConfigured):
            client.save_objects_batch([{'objectID': 'x'}])

    def test_save_objects_batch_reraises_algolia_exception(self):
        """
        Algolia errors are re-raised so the caller can record the failure.
        """
        client = self._build_client()
        client.algolia_index.save_objects.side_effect = AlgoliaException('boom')

        with self.assertRaises(AlgoliaException):
            client.save_objects_batch([{'objectID': 'x'}])

    def test_delete_objects_batch_calls_delete_on_primary(self):
        """
        ``delete_objects_batch`` delegates to ``delete_objects`` on the primary index.
        """
        client = self._build_client()
        ids = ['course-abc-catalog-query-uuids-0', 'course-abc-catalog-query-uuids-1']

        client.delete_objects_batch(ids)

        client.algolia_index.delete_objects.assert_called_once_with(ids)

    def test_delete_objects_batch_chunks_into_multiple_calls(self):
        """
        With ``chunk_size`` smaller than the input length, the call is split
        across multiple ``delete_objects`` invocations.
        """
        client = self._build_client()
        ids = [f'shard-{i}' for i in range(5)]

        client.delete_objects_batch(ids, chunk_size=2)

        self.assertEqual(client.algolia_index.delete_objects.call_count, 3)
        chunks = [call.args[0] for call in client.algolia_index.delete_objects.call_args_list]
        self.assertEqual([oid for chunk in chunks for oid in chunk], ids)
        self.assertEqual([len(chunk) for chunk in chunks], [2, 2, 1])

    def test_delete_objects_batch_targets_alternate_index(self):
        """
        With ``index_name`` set, ``delete_objects_batch`` deletes from that index.
        """
        client = self._build_client()
        alt_index = mock.MagicMock(name='alt_index')
        client._client.init_index.return_value = alt_index
        ids = ['course-abc-catalog-query-uuids-0']

        client.delete_objects_batch(ids, index_name=self.ALT_INDEX_NAME)

        alt_index.delete_objects.assert_called_once_with(ids)
        client.algolia_index.delete_objects.assert_not_called()

    @ddt.data([], None)
    def test_delete_objects_batch_noop_when_no_ids(self, ids):
        """
        Empty/None object_ids returns early without calling Algolia.
        """
        client = self._build_client()
        client.delete_objects_batch(ids)
        client.algolia_index.delete_objects.assert_not_called()

    def test_delete_objects_batch_reraises_algolia_exception(self):
        """
        Algolia errors are re-raised.
        """
        client = self._build_client()
        client.algolia_index.delete_objects.side_effect = AlgoliaException('boom')

        with self.assertRaises(AlgoliaException):
            client.delete_objects_batch(['course-abc-catalog-query-uuids-0'])

    def test_get_object_ids_for_aggregation_key_returns_object_ids(self):
        """
        Browses the index filtered by aggregation_key and collects objectIDs.
        """
        client = self._build_client()
        client.algolia_index.browse_objects.return_value = iter([
            {'objectID': 'course-abc-catalog-query-uuids-0'},
            {'objectID': 'course-abc-catalog-query-uuids-1'},
        ])

        result = client.get_object_ids_for_aggregation_key('course-abc')

        self.assertEqual(result, [
            'course-abc-catalog-query-uuids-0',
            'course-abc-catalog-query-uuids-1',
        ])
        client.algolia_index.browse_objects.assert_called_once_with({
            'attributesToRetrieve': ['objectID'],
            'filters': "aggregation_key:'course-abc'",
        })

    def test_get_object_ids_for_aggregation_key_empty_when_no_matches(self):
        """
        No matching shards yields an empty list.
        """
        client = self._build_client()
        client.algolia_index.browse_objects.return_value = iter([])

        self.assertEqual(client.get_object_ids_for_aggregation_key('course-missing'), [])

    def test_get_object_ids_for_aggregation_key_targets_alternate_index(self):
        """
        Browses the alternate index when ``index_name`` is provided.
        """
        client = self._build_client()
        alt_index = mock.MagicMock(name='alt_index')
        alt_index.browse_objects.return_value = iter([{'objectID': 'course-abc-x-0'}])
        client._client.init_index.return_value = alt_index

        result = client.get_object_ids_for_aggregation_key('course-abc', index_name=self.ALT_INDEX_NAME)

        self.assertEqual(result, ['course-abc-x-0'])
        alt_index.browse_objects.assert_called_once()
        client.algolia_index.browse_objects.assert_not_called()

    def test_get_object_ids_for_aggregation_key_reraises_algolia_exception(self):
        """
        Algolia errors are re-raised.
        """
        client = self._build_client()
        client.algolia_index.browse_objects.side_effect = AlgoliaException('boom')

        with self.assertRaises(AlgoliaException):
            client.get_object_ids_for_aggregation_key('course:edx-abc')

    def test_get_aggregation_keys_for_catalog_query_dedupes_across_shards(self):
        """
        Multiple shards of the same content yield a single aggregation_key in the result.
        """
        client = self._build_client()
        client.algolia_index.browse_objects.return_value = iter([
            {'aggregation_key': 'course:edx-abc'},
            {'aggregation_key': 'course:edx-abc'},  # second shard, same content key
            {'aggregation_key': 'course:edx-def'},
        ])

        result = client.get_aggregation_keys_for_catalog_query('cq-uuid-1')

        self.assertEqual(result, {'course:edx-abc', 'course:edx-def'})
        client.algolia_index.browse_objects.assert_called_once_with({
            'attributesToRetrieve': ['aggregation_key'],
            'facetFilters': ['enterprise_catalog_query_uuids:cq-uuid-1'],
        })

    def test_get_aggregation_keys_for_catalog_query_skips_missing_aggregation_key(self):
        """
        Records without ``aggregation_key`` are ignored rather than added as None.
        """
        client = self._build_client()
        client.algolia_index.browse_objects.return_value = iter([
            {'aggregation_key': 'course:edx-abc'},
            {},
            {'aggregation_key': None},
        ])

        result = client.get_aggregation_keys_for_catalog_query('cq-uuid-1')
        self.assertEqual(result, {'course:edx-abc'})

    def test_get_aggregation_keys_for_catalog_query_targets_alternate_index(self):
        """
        Browses the alternate index when ``index_name`` is provided.
        """
        client = self._build_client()
        alt_index = mock.MagicMock(name='alt_index')
        alt_index.browse_objects.return_value = iter([{'aggregation_key': 'course:edx-abc'}])
        client._client.init_index.return_value = alt_index

        result = client.get_aggregation_keys_for_catalog_query(
            'cq-uuid-1', index_name=self.ALT_INDEX_NAME,
        )

        self.assertEqual(result, {'course:edx-abc'})
        alt_index.browse_objects.assert_called_once()
        client.algolia_index.browse_objects.assert_not_called()

    def test_get_aggregation_keys_for_catalog_query_reraises_algolia_exception(self):
        """
        Algolia errors are re-raised.
        """
        client = self._build_client()
        client.algolia_index.browse_objects.side_effect = AlgoliaException('boom')

        with self.assertRaises(AlgoliaException):
            client.get_aggregation_keys_for_catalog_query('cq-uuid-1')

    def test_get_aggregation_keys_for_catalog_query_raises_when_index_unavailable(self):
        """
        With no initialized index, raises ImproperlyConfigured.
        """
        client = AlgoliaSearchClient()
        with self.assertRaises(ImproperlyConfigured):
            client.get_aggregation_keys_for_catalog_query('cq-uuid-1')

    def test_get_object_ids_for_aggregation_key_raises_when_index_unavailable(self):
        """
        With no initialized index, raises ImproperlyConfigured.
        """
        client = AlgoliaSearchClient()
        with self.assertRaises(ImproperlyConfigured):
            client.get_object_ids_for_aggregation_key('course:edx-abc')
