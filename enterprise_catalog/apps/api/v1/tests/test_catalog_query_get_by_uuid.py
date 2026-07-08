import uuid as uuid_lib

import ddt
from django.urls import reverse
from rest_framework import status

from enterprise_catalog.apps.api.v1.tests.mixins import APITestMixin
from enterprise_catalog.apps.catalog.tests.factories import CatalogQueryFactory


@ddt.ddt
class TestCatalogQueryGetByUuidAction(APITestMixin):
    """
    Tests for GET /api/v1/catalog-queries/<uuid>/
    """

    def setUp(self):
        """Set up test fixtures."""
        super().setUp()
        self.set_up_staff()
        self.catalog_query = CatalogQueryFactory(
            title='Subscription Query',
            content_filter={'partner': 'edx', 'content_type': 'course'},
        )
        self.url = self._get_by_uuid_url(self.catalog_query.uuid)

    def _get_by_uuid_url(self, query_uuid):
        return reverse(
            'api:v1:get-query-by-uuid',
            kwargs={'uuid': str(query_uuid)},
        )

    def _assert_method_not_allowed(self, method_name):
        request = getattr(self.client, method_name)
        if method_name in {'post', 'put'}:
            response = request(self.url, data={'title': 'hacked'})
        else:
            response = request(self.url)
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    # ─────────────────────────────────────────────────────────
    # Line 35-52: Success case
    # ─────────────────────────────────────────────────────────
    def test_get_by_uuid_success(self):
        """
        GET /api/v1/catalog-queries/<uuid>/ returns 200
        with the correct CatalogQuery data.
        """
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['uuid'], str(self.catalog_query.uuid))
        self.assertEqual(response.data['id'], self.catalog_query.id)
        self.assertEqual(response.data['title'], 'Subscription Query')
        self.assertEqual(
            response.data['content_filter'],
            {'partner': 'edx', 'content_type': 'course'},
        )

    # ─────────────────────────────────────────────────────────
    # Line 54-67: 404 for non-existent UUID
    # ─────────────────────────────────────────────────────────
    def test_get_by_uuid_not_found(self):
        """
        GET /api/v1/catalog-queries/<uuid>/ returns 404.
        """
        non_existent_uuid = str(uuid_lib.uuid4())
        url = self._get_by_uuid_url(non_existent_uuid)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # ─────────────────────────────────────────────────────────
    # Line 69-82: Malformed UUID returns 404 (regex mismatch)
    # ─────────────────────────────────────────────────────────
    def test_get_by_uuid_malformed_uuid(self):
        """
        GET /api/v1/catalog-queries/<uuid>/ returns 404 for an invalid UUID.
        Note: the <uuid:uuid> route won't match, so this request falls through to the pk-based route and returns 404.
        """
        response = self.client.get('/api/v1/catalog-queries/not-a-valid-uuid/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # ─────────────────────────────────────────────────────────
    # Line 84-100: Existing PK-based endpoint still works
    # ─────────────────────────────────────────────────────────
    def test_existing_pk_endpoint_unchanged(self):
        """
        GET /api/v1/catalog-queries/{pk}/ still works as before,
        confirming no breaking change to v1 clients.
        """
        pk_url = reverse(
            'api:v1:catalog-queries-detail',
            kwargs={'pk': self.catalog_query.pk},
        )
        response = self.client.get(pk_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['id'], self.catalog_query.id)
        self.assertEqual(response.data['uuid'], str(self.catalog_query.uuid))

    # ─────────────────────────────────────────────────────────
    # Line 120-137: Multiple queries, correct one returned
    # ─────────────────────────────────────────────────────────
    def test_get_by_uuid_returns_correct_instance(self):
        """
        When multiple CatalogQuery objects exist, the endpoint
        returns the one matching the requested UUID.
        """
        other_query = CatalogQueryFactory(
            title='A La Carte Query',
            content_filter={'partner': 'edx', 'content_type': 'program'},
        )

        # Fetch the original query by UUID
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['uuid'], str(self.catalog_query.uuid))
        self.assertNotEqual(response.data['uuid'], str(other_query.uuid))

        # Fetch the other query by UUID
        other_url = reverse(
            'api:v1:get-query-by-uuid',
            kwargs={'uuid': str(other_query.uuid)},
        )
        other_response = self.client.get(other_url)
        self.assertEqual(other_response.status_code, status.HTTP_200_OK)
        self.assertEqual(other_response.data['uuid'], str(other_query.uuid))
        self.assertEqual(other_response.data['title'], 'A La Carte Query')

    # ─────────────────────────────────────────────────────────
    # Line 139-151: Unauthenticated request returns 401/403
    # ─────────────────────────────────────────────────────────
    def test_get_by_uuid_unauthenticated(self):
        """
        Unauthenticated requests to the UUID endpoint are rejected.
        """
        self.client.logout()
        response = self.client.get(self.url)

        self.assertIn(
            response.status_code,
            [status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN],
        )

    # ─────────────────────────────────────────────────────────
    # Line 153-166: POST, PUT, DELETE methods
    # ─────────────────────────────────────────────────────────
    @ddt.data('post', 'put', 'delete')
    def test_get_by_uuid_disallows_methods(self, method_name):
        """
        POST, PUT, and DELETE /api/v1/catalog-queries/<uuid>/ return 405.
        """
        self._assert_method_not_allowed(method_name)
