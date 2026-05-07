"""
Algolia api client code.
"""

import logging
from datetime import timedelta

from algoliasearch.exceptions import AlgoliaException
from algoliasearch.search_client import SearchClient
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from enterprise_catalog.apps.catalog.utils import localized_utcnow


logger = logging.getLogger(__name__)


class AlgoliaSearchClient:
    """
    Object builds an API client to make calls to an Algolia index.
    """

    def __init__(self):
        self._client = None
        self.algolia_index = None
        self.replica_index = None

    @property
    def algolia_application_id(self):
        return settings.ALGOLIA.get('APPLICATION_ID')

    @property
    def algolia_api_key(self):
        return settings.ALGOLIA.get('API_KEY')

    @property
    def algolia_search_api_key(self):
        return settings.ALGOLIA.get('SEARCH_API_KEY')

    @property
    def algolia_index_name(self):
        return settings.ALGOLIA.get('INDEX_NAME')

    @property
    def algolia_replica_index_name(self):
        return settings.ALGOLIA.get('REPLICA_INDEX_NAME')

    def init_index(self):
        """
        Initializes an index within Algolia. Initializing an index will create it if it doesn't exist.
        """
        if not self.algolia_index_name or not self.algolia_replica_index_name:
            logger.error('Could not initialize Algolia index due to missing index name.')
            return

        if not self.algolia_application_id or not self.algolia_api_key:
            logger.error(
                'Could not initialize Algolia\'s %s index due to missing Algolia settings: %s',
                self.algolia_index_name,
                ['APPLICATION_ID', 'API_KEY'],
            )
            return

        # Create SearchClient
        self._client = SearchClient.create(self.algolia_application_id, self.algolia_api_key)

        # Initialize Algolia indices
        if self.algolia_index_name:
            try:
                self.algolia_index = self._client.init_index(self.algolia_index_name)
            except AlgoliaException as exc:
                logger.exception(
                    'Could not initialize %s index in Algolia due to an exception.',
                    self.algolia_index_name,
                )
                raise exc
        if self.algolia_replica_index_name:
            try:
                self.replica_index = self._client.init_index(self.algolia_replica_index_name)
            except AlgoliaException as exc:
                logger.exception(
                    'Could not initialize %s index in Algolia due to an exception.',
                    self.algolia_replica_index_name,
                )
                raise exc

    def set_index_settings(self, index_settings, primary_index=True):
        """
        Set default settings to use for the Algolia index.

        Note: This will override manual updates to the index configuration on the
        Algolia dashboard but ensures consistent settings (configuration as code).

        Arguments:
            settings (dict): A dictionary of Algolia settings.
        """
        if not self.algolia_index:
            logger.error('Algolia index does not exist. Did you initialize it?')
            return

        try:
            if primary_index:
                self.algolia_index.set_settings(index_settings)
            else:
                self.replica_index.set_settings(index_settings)
        except AlgoliaException as exc:
            logger.exception(
                'Unable to set settings for Algolia\'s %s index due to an exception.',
                self.algolia_index_name,
            )
            raise exc

    def index_exists(self):
        """
        Returns whether the index exists in Algolia.
        """
        if not self.algolia_index or not self.replica_index:
            logger.error('Algolia index does not exist. Did you initialize it?')
            return False

        primary_exists = self.algolia_index.exists()
        replica_exists = self.replica_index.exists()
        if not primary_exists:
            logger.warning(
                'Index with name %s does not exist in Algolia.',
                self.algolia_index_name,
            )
        if not replica_exists:
            logger.warning(
                'Index with name %s does not exist in Algolia.',
                self.algolia_replica_index_name,
            )

        return primary_exists and replica_exists

    def _get_index(self, index_name=None):
        """
        Return the Algolia index for ``index_name``, defaulting to the primary index.

        ``index_name`` lets callers target an alternate index (e.g. the v2 index during
        cutover validation) without re-initializing the client.

        Raises:
            ImproperlyConfigured: if the requested index is unavailable. This is a setup
            error: callers (e.g. the incremental indexing batch tasks) should let it
            propagate so the failure surfaces loudly rather than being silently treated
            as success.
        """
        if index_name is None or index_name == self.algolia_index_name:
            if self.algolia_index is None:
                raise ImproperlyConfigured(
                    'Algolia primary index is not initialized; call init_index() first.'
                )
            return self.algolia_index
        if not self._client:
            raise ImproperlyConfigured(
                'Algolia client is not initialized; call init_index() first.'
            )
        return self._client.init_index(index_name)

    def save_objects_batch(self, algolia_objects, index_name=None):
        """
        Upsert a batch of objects into the given index without affecting other records.
        This intentionally does *not* wait for the asynchronous Algolia index job to complete.
        See ADR 0012 for details.

        Arguments:
            algolia_objects (list): Objects to save. Each must include an ``objectID``.
            index_name (str): Optional index name; defaults to the primary index.
        """
        index = self._get_index(index_name)
        try:
            return index.save_objects(algolia_objects)
        except AlgoliaException as exc:
            logger.exception(
                'Could not save objects batch in the %s Algolia index due to an exception.',
                index_name or self.algolia_index_name,
            )
            raise exc

    def delete_objects_batch(self, object_ids, index_name=None):
        """
        Delete a batch of objects by objectID from the given index.

        Arguments:
            object_ids (list): Algolia objectIDs to delete.
            index_name (str): Optional index name; defaults to the primary index.
        """
        if not object_ids:
            return None
        index = self._get_index(index_name)
        try:
            return index.delete_objects(object_ids)
        except AlgoliaException as exc:
            logger.exception(
                'Could not delete objects batch from the %s Algolia index due to an exception.',
                index_name or self.algolia_index_name,
            )
            raise exc

    def get_object_ids_for_aggregation_key(self, aggregation_key, index_name=None):
        """
        Return all Algolia objectIDs (shards) for the given ``aggregation_key``.

        Algolia object IDs for a content record are sharded as
        ``{content_type}-{uuid}-{shard_kind}-{batch_index}`` and all shards for the
        same content record share the same ``aggregation_key``, which is the
        ``{content_type}:{content_key}`` form (e.g. ``"course:edX+DemoX"``) emitted by
        the legacy object generator. Callers that have a content_key + content_type
        should construct the aggregation_key themselves before calling this.

        Used by the incremental indexing tasks to discover existing shards so
        orphaned ones can be deleted.
        """
        index = self._get_index(index_name)
        object_ids = []
        try:
            # aggregation_key values come from ContentMetadata-derived data
            # (``{content_type}:{content_key}``) and don't contain single quotes,
            # so direct interpolation into the Algolia filter DSL is safe.
            iterator = index.browse_objects({
                'attributesToRetrieve': ['objectID'],
                'filters': f"aggregation_key:'{aggregation_key}'",
            })
            for hit in iterator:
                object_ids.append(hit['objectID'])
        except AlgoliaException as exc:
            logger.exception(
                'Could not list objectIDs for aggregation_key %s in the %s Algolia index due to an exception.',
                aggregation_key,
                index_name or self.algolia_index_name,
            )
            raise exc
        return object_ids

    def get_aggregation_keys_for_catalog_query(self, catalog_query_uuid, index_name=None):
        """
        Return the set of ``aggregation_key`` values currently indexed with the given
        catalog query's facet.

        Each returned value is a ``"{content_type}:{content_key}"`` string (e.g.
        ``"course:edX+DemoX"``); shards belonging to the same content collapse to a
        single entry because they share the same ``aggregation_key``. Callers that
        want bare ``content_key``s (or just the ``content_type``) should split the
        returned strings on ``:``.

        Used by the per-catalog dispatcher to detect membership removals: any
        aggregation_key present in Algolia under this catalog query but no longer
        in the database membership needs to be reindexed so its facets reflect the
        removal.

        Arguments:
            catalog_query_uuid (str|UUID): The CatalogQuery uuid to filter by.
            index_name (str): Optional index name; defaults to the primary index.

        Returns:
            set[str]: aggregation_keys currently indexed under this catalog query.
        """
        index = self._get_index(index_name)
        aggregation_keys = set()
        try:
            # browse_objects returns an ObjectIterator that auto-paginates via cursor;
            # safe for catalog queries with 10k+ records.
            # catalog_query_uuid is a UUID string with no facet-DSL metacharacters,
            # so direct interpolation into the facetFilter is safe.
            iterator = index.browse_objects({
                'attributesToRetrieve': ['aggregation_key'],
                'facetFilters': [f'enterprise_catalog_query_uuids:{catalog_query_uuid}'],
            })
            for hit in iterator:
                aggregation_key = hit.get('aggregation_key')
                if aggregation_key:
                    aggregation_keys.add(aggregation_key)
        except AlgoliaException as exc:
            logger.exception(
                'Could not list aggregation keys for catalog query %s in the %s Algolia index due to an exception.',
                catalog_query_uuid,
                index_name or self.algolia_index_name,
            )
            raise exc
        return aggregation_keys

    def replace_all_objects(self, algolia_objects):  # pragma: no cover
        """
        Clears all objects from the index and replaces them with a new set of objects. The records are
        replaced in the index without any downtime due to an atomic reindex.

        See https://www.algolia.com/doc/api-reference/api-methods/replace-all-objects/ for more detials.

        Arguments:
            algolia_objects (list): List of objects to include in the Algolia index
        """
        if not self.index_exists():
            # index must exist to continue, nothing left to do
            return

        # The 'safe' field makes the client wait for asynchronous indexing operations to complete
        use_safe = getattr(settings, 'USE_REPLACE_ALL_OBJECTS_SAFE', True)
        try:
            self.algolia_index.replace_all_objects(algolia_objects, {
                'safe': use_safe,
            })
            logger.info('The %s Algolia index was successfully indexed.', self.algolia_index_name)
        except AlgoliaException as exc:
            logger.exception(
                'Could not index objects in the %s Algolia index due to an exception.',
                self.algolia_index_name,
            )
            raise exc

    def get_all_objects_associated_with_aggregation_key(self, aggregation_key):
        """
        Returns an array of Algolia object IDs associated with the given aggregation key.
        """
        objects = []
        if not self.index_exists():
            # index must exist to continue, nothing left to do
            return objects
        try:
            index_browse_iterator = self.algolia_index.browse_objects({
                "attributesToRetrieve": ["objectID"],
                "filters": f"aggregation_key:'{aggregation_key}'",
            })
            for hit in index_browse_iterator:
                objects.append(hit['objectID'])
        except AlgoliaException as exc:
            logger.exception(
                'Could not retrieve objects associated with aggregation key %s due to an exception.',
                aggregation_key,
            )
            raise exc
        return objects

    def remove_objects(self, object_ids):
        """
        Removes objects from the Algolia index.
        """
        if not self.index_exists():
            # index must exist to continue, nothing left to do
            return

        try:
            self.algolia_index.delete_objects(object_ids)
            logger.info(
                'The following objects were successfully removed from the %s Algolia index: %s',
                self.algolia_index_name,
                object_ids,
            )
        except AlgoliaException as exc:
            logger.exception(
                'Could not remove objects from the %s Algolia index due to an exception.',
                self.algolia_index_name,
            )
            raise exc

    def generate_secured_api_key(self, user_id, enterprise_catalog_query_uuids):
        """
        Generates a secured api key for the Algolia search API.
        The secured api key will be used to restrict the search results to only those
        that are associated with the given enterprise catalog query uuids.
        The secured api key will also be restricted to the given user id.
        Arguments:
            user_id (str): The user id to restrict the api key to.
            enterprise_catalog_query_uuids (list): The enterprise catalog query uuids to restrict the api key to.
        Returns:
            dict: A dictionary containing the secured api key and the expiration time.
            The expiration time is in ISO format.
        """
        if not self.algolia_search_api_key:
            logger.error(
                'Could not generate secured Algolia API key due to missing Algolia settings: %s',
                'SEARCH_API_KEY',
            )
            raise ImproperlyConfigured(
                'Cannot generate secured Algolia API key without the ALGOLIA.SEARCH_API_KEY in settings.'
            )

        expiration_time = getattr(settings, 'SECURED_ALGOLIA_API_KEY_EXPIRATION', 3600)  # Default to 1 hour
        valid_until_dt = localized_utcnow() + timedelta(seconds=expiration_time)
        valid_until_unix = int(valid_until_dt.timestamp())
        catalog_query_filter = ' OR '.join(
            [f'enterprise_catalog_query_uuids:{query_uuid}' for query_uuid in enterprise_catalog_query_uuids]
        )

        # Base secured API key restrictions
        restrictions = {
            'filters': catalog_query_filter,
            'validUntil': valid_until_unix,
            'userToken': user_id,
        }

        # Determine indices to restrict
        indices = []
        if self.algolia_index_name:
            indices.append(self.algolia_index_name)
        if self.algolia_replica_index_name:
            indices.append(self.algolia_replica_index_name)
        if indices:
            restrictions |= {'restrictIndices': indices}

        # Generate secured api key
        logger.info('[AlgoliaSearchClient.generate_secured_api_key] restrictions: %s', restrictions)
        try:
            secured_api_key = SearchClient.generate_secured_api_key(
                self.algolia_search_api_key,
                restrictions,
            )
        except AlgoliaException as exc:
            logger.exception('Could not generate secured Algolia API key due to an AlgoliaException.')
            raise exc

        # Return secured api key and expiration time
        iso_format = "%Y-%m-%dT%H:%M:%SZ"
        valid_until_iso = valid_until_dt.strftime(iso_format)
        return {
            'secured_api_key': secured_api_key,
            'valid_until': valid_until_iso,
        }
