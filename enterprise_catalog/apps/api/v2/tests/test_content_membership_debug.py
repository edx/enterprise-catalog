"""
v2 tests for the content-catalog-membership debug endpoint.

v2 routes the same view as v1 rather than subclassing it, because the response already
reports the restricted and unrestricted membership answers side by side and there is no
``include_restricted`` flag for a v2 subclass to flip. These tests pin that down: the v2
route exists, enforces the same staff-only permission, and returns a payload identical to
v1's for the same fixtures. They deliberately do not re-run the v1 behaviour suite --
edx-lint's ``test-inherits-tests`` forbids sharing test methods across classes, and the
two routes resolve to the same view object anyway.
"""
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


class ContentMembershipDebugViewV2Tests(APITestMixin):
    """
    Tests for the v2 route of ``ContentMembershipDebugView``.
    """

    def setUp(self):
        super().setUp()
        self.course = ContentMetadataFactory(content_type=COURSE, content_key='edX+DebugX')
        self.catalog = EnterpriseCatalogFactory(enterprise_uuid=self.enterprise_uuid)
        self.add_metadata_to_catalog(self.catalog, [self.course])

    def _url(self, version, content_key=None):
        return reverse(
            f'api:{version}:content-membership-debug',
            kwargs={
                'enterprise_uuid': str(self.enterprise_uuid),
                'content_key': content_key or self.course.content_key,
            },
        )

    @staticmethod
    def _normalized(payload):
        """
        Sort the catalogs list, which the view returns in no defined order.
        """
        payload['catalogs'] = sorted(payload['catalogs'], key=lambda block: block['catalog_uuid'])
        return payload

    def test_v2_route_exists_for_staff(self):
        self.set_up_staff()
        response = self.client.get(self._url('v2'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_v2_route_is_staff_only(self):
        self.set_up_catalog_learner()
        response = self.client.get(self._url('v2'))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_v2_payload_matches_v1(self):
        """
        The two routes resolve to the same view; if that ever changes, this fails.
        """
        self.set_up_staff()
        v1_payload = self._normalized(self.client.get(self._url('v1')).json())
        v2_payload = self._normalized(self.client.get(self._url('v2')).json())
        self.assertEqual(v1_payload, v2_payload)

    def test_v2_payload_matches_v1_for_restricted_content(self):
        """
        Restricted-run membership is the one place v1 and v2 semantics normally diverge, so
        pin it explicitly: both versions report both answers, and report them identically.
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

        v1_payload = self._normalized(self.client.get(self._url('v1', restricted_run.content_key)).json())
        v2_payload = self._normalized(self.client.get(self._url('v2', restricted_run.content_key)).json())

        self.assertEqual(v1_payload, v2_payload)
        # Sanity check that the fixture actually exercised the restricted path.
        block = v2_payload['catalogs'][0]
        self.assertFalse(block['contains_content'])
        self.assertTrue(block['contains_content_including_restricted'])
