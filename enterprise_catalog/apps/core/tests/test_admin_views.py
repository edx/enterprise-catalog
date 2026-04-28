"""Tests for the superuser-only admin settings view."""
import ddt
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse


User = get_user_model()


@ddt.ddt
class AdminSettingsViewTests(TestCase):
    """Verify access control and redaction for the settings admin view."""

    URL = reverse('admin-settings')

    def _login_as(self, user_flags):
        """Log in a user with the given flags, or stay anonymous if None."""
        if user_flags is None:
            return
        user = User.objects.create_user(username='u', password='pw', **user_flags)
        self.client.force_login(user)

    @ddt.data(
        # (user_flags, allowed)
        (None, False),
        ({'is_staff': False, 'is_superuser': False}, False),
        ({'is_staff': True, 'is_superuser': False}, False),
        ({'is_staff': False, 'is_superuser': True}, False),  # admin login requires is_staff
        ({'is_staff': True, 'is_superuser': True}, True),
    )
    @ddt.unpack
    def test_access_control(self, user_flags, allowed):
        self._login_as(user_flags)
        response = self.client.get(self.URL)
        if allowed:
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'INSTALLED_APPS')
        else:
            self.assertEqual(response.status_code, 302)
            self.assertIn('/login/', response.url)

    @ddt.data(
        ('MY_SECRET_KEY', 'supersecret-value'),
        ('MY_API_TOKEN', 'token-value'),
        ('ALGOLIA_INDEX_NAME', 'public-index-name'),
        ('BRAZE_API_URL', 'https://braze.example/'),
        ('SOMETHING_CLIENT_ID', 'client-id-value'),
    )
    @ddt.unpack
    def test_sensitive_values_redacted(self, setting_name, setting_value):
        user = User.objects.create_user(
            username='su', password='pw', is_staff=True, is_superuser=True,
        )
        self.client.force_login(user)
        with override_settings(**{setting_name: setting_value}):
            response = self.client.get(self.URL)
        body = response.content.decode()
        self.assertNotIn(setting_value, body)
        self.assertIn('*' * 20, body)

    def test_admin_index_shows_link_for_superuser(self):
        user = User.objects.create_user(
            username='su', password='pw', is_staff=True, is_superuser=True,
        )
        self.client.force_login(user)
        response = self.client.get(reverse('admin:index'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.URL)
