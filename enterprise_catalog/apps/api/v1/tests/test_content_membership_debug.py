"""
Tests for the admin-only content-catalog-membership debug endpoint.
"""
from datetime import timedelta

import ddt
from django.urls import reverse
from rest_framework import status

from enterprise_catalog.apps.api.v1.tests.mixins import APITestMixin
from enterprise_catalog.apps.catalog.constants import COURSE, COURSE_RUN
from enterprise_catalog.apps.catalog.tests.factories import (
    ContentMetadataFactory,
    EnterpriseCatalogFactory,
    RestrictedCourseMetadataFactory,
    RestrictedRunAllowedForRestrictedCourseFactory,
)
from enterprise_catalog.apps.catalog.utils import localized_utcnow
from enterprise_catalog.apps.search.tests.factories import (
    ContentMetadataIndexingStateFactory,
)


@ddt.ddt
class ContentMembershipDebugViewTests(APITestMixin):
    """
    Tests for ``ContentMembershipDebugView``.
    """

    def setUp(self):
        super().setUp()
        self.course = ContentMetadataFactory(content_type=COURSE, content_key='edX+DebugX')
        self.catalog = EnterpriseCatalogFactory(enterprise_uuid=self.enterprise_uuid)
        self.other_catalog = EnterpriseCatalogFactory(enterprise_uuid=self.enterprise_uuid)

    def _url(self, content_key=None, enterprise_uuid=None):
        return reverse(
            'api:v1:content-membership-debug',
            kwargs={
                'enterprise_uuid': str(enterprise_uuid or self.enterprise_uuid),
                'content_key': content_key or self.course.content_key,
            },
        )

    def _block_for(self, payload, catalog):
        """
        Pull one catalog's block out of the payload by uuid; the response has no defined order.
        """
        return next(
            block for block in payload['catalogs']
            if block['catalog_uuid'] == str(catalog.uuid)
        )

    def test_unauthenticated_is_rejected(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_catalog_learner_is_forbidden(self):
        """
        Membership internals are staff-only, even for a user with catalog access.
        """
        self.set_up_catalog_learner()
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_is_allowed(self):
        self.set_up_staff()
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @ddt.data('not-a-uuid', '12345')
    def test_malformed_enterprise_uuid_returns_400(self, bad_uuid):
        self.set_up_staff()
        response = self.client.get(self._url(enterprise_uuid=bad_uuid))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reports_only_the_catalogs_that_contain_the_content(self):
        """
        Requirement 1: which catalogs, if any, contain this content key for the customer.
        """
        self.set_up_staff()
        self.add_metadata_to_catalog(self.catalog, [self.course])

        payload = self.client.get(self._url()).json()

        self.assertEqual(payload['catalogs_containing_content'], [str(self.catalog.uuid)])
        blocks = {block['catalog_uuid']: block for block in payload['catalogs']}
        self.assertEqual(len(blocks), 2)
        self.assertTrue(blocks[str(self.catalog.uuid)]['contains_content'])
        self.assertFalse(blocks[str(self.other_catalog.uuid)]['contains_content'])
        self.assertEqual(
            blocks[str(self.catalog.uuid)]['matched_content_keys'],
            [self.course.content_key],
        )

    def test_restricted_run_only_membership_is_flagged(self):
        """
        Requirement 2: membership that depends on restricted course run inclusion.

        The run is associated with the catalog query but mapped to a restricted course,
        so it is excluded by default and only visible with ``include_restricted=True``.
        """
        self.set_up_staff()
        restricted_run = ContentMetadataFactory(
            content_type=COURSE_RUN,
            content_key='course-v1:edX+DebugX+3T2024',
            parent_content_key=self.course.content_key,
        )
        restricted_course = RestrictedCourseMetadataFactory(
            content_key=self.course.content_key,
            unrestricted_parent=self.course,
            catalog_query=self.catalog.catalog_query,
        )
        RestrictedRunAllowedForRestrictedCourseFactory(
            course=restricted_course,
            run=restricted_run,
        )
        self.add_metadata_to_catalog(self.catalog, [restricted_run])

        payload = self.client.get(self._url(content_key=restricted_run.content_key)).json()
        block = self._block_for(payload, self.catalog)

        self.assertFalse(block['contains_content'])
        self.assertTrue(block['contains_content_including_restricted'])
        self.assertTrue(block['restricted_only'])
        self.assertEqual(
            payload['catalogs_containing_content_including_restricted'],
            [str(self.catalog.uuid)],
        )
        self.assertEqual(payload['catalogs_containing_content'], [])

    def test_reports_membership_proxy_timestamps(self):
        """
        Requirement 3: no true membership timestamp exists in the schema, so the endpoint
        returns the two closest stand-ins under their own names.
        """
        self.set_up_staff()
        self.add_metadata_to_catalog(self.catalog, [self.course])

        block = self._block_for(self.client.get(self._url()).json(), self.catalog)

        self.assertIsNotNone(block['catalog_query_modified'])
        self.assertIsNotNone(block['enterprise_catalog_modified'])

    def test_reports_content_modified_time(self):
        """
        Requirement 4: when the content was last updated.
        """
        self.set_up_staff()
        payload = self.client.get(self._url()).json()

        self.assertTrue(payload['content']['exists'])
        self.assertEqual(payload['content']['content_type'], COURSE)
        self.assertIsNotNone(payload['content']['modified'])

    def test_unknown_content_key_returns_200_not_404(self):
        """
        Content that was never synced from Discovery is a finding, not an error.
        """
        self.set_up_staff()
        response = self.client.get(self._url(content_key='edX+NeverSynced'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = response.json()
        # Same key set as the exists=True case, all null.
        self.assertFalse(payload['content']['exists'])
        self.assertIsNone(payload['content']['modified'])
        self.assertIsNone(payload['content']['content_type'])
        self.assertIsNone(payload['content']['content_uuid'])
        self.assertFalse(payload['algolia']['indexing_state_exists'])
        self.assertIsNone(payload['algolia']['last_indexed_at'])

    def test_algolia_indexing_state_is_reported(self):
        """
        Requirement 5: when the content was last indexed in Algolia.
        """
        self.set_up_staff()
        self.add_metadata_to_catalog(self.catalog, [self.course])
        indexed_at = localized_utcnow() + timedelta(days=1)
        ContentMetadataIndexingStateFactory(
            content_metadata=self.course,
            last_indexed_at=indexed_at,
            algolia_object_ids=['course-abc123-catalog-uuids-0'],
        )

        payload = self.client.get(self._url()).json()

        self.assertTrue(payload['algolia']['indexing_state_exists'])
        self.assertIsNotNone(payload['algolia']['last_indexed_at'])
        self.assertEqual(
            payload['algolia']['algolia_object_ids'],
            ['course-abc123-catalog-uuids-0'],
        )
        # Indexed after the catalog query last changed.
        self.assertTrue(self._block_for(payload, self.catalog)['indexed_after_last_membership_change'])

    def test_index_older_than_membership_change_is_flagged(self):
        """
        An index run predating the last catalog query change means the index is behind.
        """
        self.set_up_staff()
        self.add_metadata_to_catalog(self.catalog, [self.course])
        ContentMetadataIndexingStateFactory(
            content_metadata=self.course,
            last_indexed_at=self.catalog.catalog_query.modified - timedelta(days=1),
        )

        payload = self.client.get(self._url()).json()

        self.assertFalse(self._block_for(payload, self.catalog)['indexed_after_last_membership_change'])
