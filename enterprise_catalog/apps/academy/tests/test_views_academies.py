import uuid

from rest_framework import status
from rest_framework.reverse import reverse

from enterprise_catalog.apps.academy.tests.factories import AcademyFactory
from enterprise_catalog.apps.api.v1.tests.mixins import APITestMixin
from enterprise_catalog.apps.catalog.constants import (
    SYSTEM_ENTERPRISE_PROVISIONING_ADMIN_ROLE,
)
from enterprise_catalog.apps.catalog.tests.factories import (
    EnterpriseCatalogFactory,
)


class AcademiesAssociateCatalogTests(APITestMixin):
    """Tests for the AcademiesReadOnlyViewSet.associate_catalog action."""

    def setUp(self):
        super().setUp()
        # Use a staff user for all tests; provisioning admin access is granted via JWT cookie
        self.set_up_staff_user()

    def _url(self, academy_uuid):
        return reverse('api:v1:academies-associate-catalog', kwargs={'uuid': academy_uuid})

    def test_missing_enterprise_catalog_uuid_returns_400(self):
        """Missing `enterprise_catalog_uuid` in body returns 400."""
        self.remove_role_assignments()
        self.set_up_invalid_jwt_role()
        # Grant provisioning admin via JWT to bypass role-assignment checks
        self.set_jwt_cookie([(SYSTEM_ENTERPRISE_PROVISIONING_ADMIN_ROLE, '*')])

        academy = AcademyFactory()
        academy.enterprise_catalogs.clear()
        url = self._url(academy.uuid)
        response = self.client.post(url, data={})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthorized_without_provisioning_admin_returns_403(self):
        """Users without provisioning-admin permission get 403."""
        # staff user without provisioning-admin JWT
        academy = AcademyFactory()
        academy.enterprise_catalogs.clear()
        url = self._url(academy.uuid)
        response = self.client.post(url, data={'enterprise_catalog_uuid': str(uuid.uuid4())})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_associate_with_nonexistent_catalog_returns_404(self):
        """Referencing a non-existent EnterpriseCatalog returns 404."""
        self.remove_role_assignments()
        self.set_up_invalid_jwt_role()
        self.set_jwt_cookie([(SYSTEM_ENTERPRISE_PROVISIONING_ADMIN_ROLE, '*')])

        academy = AcademyFactory()
        academy.enterprise_catalogs.clear()
        url = self._url(academy.uuid)
        fake_uuid = str(uuid.uuid4())
        response = self.client.post(url, data={'enterprise_catalog_uuid': fake_uuid})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_successfully_associates_catalog_and_is_idempotent(self):
        """Provisioning-admins can associate an EnterpriseCatalog with an Academy."""
        self.remove_role_assignments()
        self.set_up_invalid_jwt_role()
        self.set_jwt_cookie([(SYSTEM_ENTERPRISE_PROVISIONING_ADMIN_ROLE, '*')])

        academy = AcademyFactory()
        academy.enterprise_catalogs.clear()
        catalog = EnterpriseCatalogFactory()

        url = self._url(academy.uuid)
        response = self.client.post(url, data={'enterprise_catalog_uuid': str(catalog.uuid)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # verify association created
        self.assertTrue(academy.enterprise_catalogs.filter(uuid=catalog.uuid).exists())

        # calling again is idempotent and should still return 200
        response2 = self.client.post(url, data={'enterprise_catalog_uuid': str(catalog.uuid)})
        self.assertEqual(response2.status_code, status.HTTP_200_OK)

    def test_invalid_enterprise_catalog_uuid_returns_400(self):
        """Invalid `enterprise_catalog_uuid` in body returns 400."""
        self.remove_role_assignments()
        self.set_up_invalid_jwt_role()
        self.set_jwt_cookie([(SYSTEM_ENTERPRISE_PROVISIONING_ADMIN_ROLE, '*')])
        academy = AcademyFactory()
        url = self._url(academy.uuid)
        response = self.client.post(url, data={'enterprise_catalog_uuid': 'not-a-uuid'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_nonexistent_academy_uuid_returns_404(self):
        """Providing a non-existent academy UUID in the path returns 404."""
        self.remove_role_assignments()
        self.set_up_invalid_jwt_role()
        self.set_jwt_cookie([(SYSTEM_ENTERPRISE_PROVISIONING_ADMIN_ROLE, '*')])

        # Use an existing EnterpriseCatalog UUID in the body, but a non-existent
        # academy UUID in the path — the view's get_object() should return 404.
        catalog = EnterpriseCatalogFactory()
        fake_academy_uuid = str(uuid.uuid4())
        url = self._url(fake_academy_uuid)
        response = self.client.post(url, data={'enterprise_catalog_uuid': str(catalog.uuid)})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
