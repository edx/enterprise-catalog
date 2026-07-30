from django.test import TestCase

from enterprise_catalog.apps.api.v1 import export_utils
from enterprise_catalog.apps.catalog import algolia_utils


class ExportUtilsTests(TestCase):
    """
    Tests for the Enterprise Catalog API export utils
    """

    def test_retrieve_available_fields(self):
        """
        Test the export isn't retrieving fields which are not indexed
        """
        # assert that ALGOLIA_ATTRIBUTES_TO_RETRIEVE is a SUBSET of ALGOLIA_FIELDS
        assert set(export_utils.ALGOLIA_ATTRIBUTES_TO_RETRIEVE) <= set(algolia_utils.ALGOLIA_FIELDS)

    def test_fetch_catalog_types_handles_missing_query_titles(self):
        """
        Regression test: a hit with no `enterprise_catalog_query_titles` field (e.g. content not
        associated with any CatalogQuery) must not raise a TypeError.
        """
        assert export_utils.fetch_catalog_types({}) == []

    def test_fetch_catalog_types_filters_to_known_types(self):
        hit = {'enterprise_catalog_query_titles': ['Subscription', 'Some Other Title']}
        assert export_utils.fetch_catalog_types(hit) == ['Subscription']

    def test_merge_sharded_hits_collapses_duplicate_aggregation_keys(self):
        """
        Regression test: Algolia shards large per-record fields across multiple physical
        records sharing the same `aggregation_key`, each populating only one of those fields.
        `search()` collapses these via the index's `distinct` setting; `browse_objects()` does
        not, so `_merge_sharded_hits` must do it instead, or exports would show duplicate rows.
        """
        shards = [
            {
                'objectID': 'course-uuid-customer-uuids-0',
                'aggregation_key': 'course:edX+DemoX',
                'title': 'Demo Course',
                'enterprise_customer_uuids': ['customer-1'],
            },
            {
                'objectID': 'course-uuid-catalog-uuids-0',
                'aggregation_key': 'course:edX+DemoX',
                'title': 'Demo Course',
                'enterprise_catalog_uuids': ['catalog-1'],
            },
            {
                'objectID': 'course-uuid-catalog-query-uuids-0',
                'aggregation_key': 'course:edX+DemoX',
                'title': 'Demo Course',
                'enterprise_catalog_query_titles': ['Subscription'],
            },
            {
                'objectID': 'other-course-uuid-0',
                'aggregation_key': 'course:edX+OtherX',
                'title': 'Other Course',
                'enterprise_catalog_query_titles': ['Business'],
            },
        ]

        merged = export_utils._merge_sharded_hits(shards)  # pylint: disable=protected-access

        assert len(merged) == 2
        demo_course = next(hit for hit in merged if hit['aggregation_key'] == 'course:edX+DemoX')
        assert demo_course['enterprise_customer_uuids'] == ['customer-1']
        assert demo_course['enterprise_catalog_uuids'] == ['catalog-1']
        assert demo_course['enterprise_catalog_query_titles'] == ['Subscription']

    def test_merge_sharded_hits_ignores_empty_field_values(self):
        """
        Regression test: a later shard with a falsy value (e.g. an empty list) for a field
        must not clobber a non-empty value already collected from an earlier shard.
        """
        shards = [
            {
                'objectID': 'course-uuid-0',
                'aggregation_key': 'course:edX+DemoX',
                'enterprise_customer_uuids': ['customer-1'],
            },
            {
                'objectID': 'course-uuid-1',
                'aggregation_key': 'course:edX+DemoX',
                'enterprise_customer_uuids': [],
            },
        ]

        merged = export_utils._merge_sharded_hits(shards)  # pylint: disable=protected-access

        assert len(merged) == 1
        assert merged[0]['enterprise_customer_uuids'] == ['customer-1']

    def test_merge_sharded_hits_unions_list_values_shared_across_shards(self):
        """
        Regression test: when two shards of the same `aggregation_key` both populate the same
        list-valued field (rather than each shard owning a distinct field), the values must be
        unioned together rather than one shard's value replacing the other's.
        """
        shards = [
            {
                'objectID': 'course-uuid-0',
                'aggregation_key': 'course:edX+DemoX',
                'enterprise_customer_uuids': ['customer-1', 'customer-2'],
            },
            {
                'objectID': 'course-uuid-1',
                'aggregation_key': 'course:edX+DemoX',
                'enterprise_customer_uuids': ['customer-2', 'customer-3'],
            },
        ]

        merged = export_utils._merge_sharded_hits(shards)  # pylint: disable=protected-access

        assert len(merged) == 1
        assert merged[0]['enterprise_customer_uuids'] == ['customer-1', 'customer-2', 'customer-3']
