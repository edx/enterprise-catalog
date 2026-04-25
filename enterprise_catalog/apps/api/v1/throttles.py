"""
Throttle classes for v1 API endpoints.

Rates are configured in ``REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']`` keyed by
each class's ``scope``. We override ``get_rate`` to read live from DRF's
``api_settings`` so that ``override_settings`` works in tests — DRF's default
implementation caches ``THROTTLE_RATES`` as a class attribute at import time.
"""
from django.core.exceptions import ImproperlyConfigured
from rest_framework.settings import api_settings
from rest_framework.throttling import UserRateThrottle


class NonStaffUserRateThrottleMixin:
    """
    Skip rate limiting for staff users and read the rate live from settings so
    ``override_settings(REST_FRAMEWORK=...)`` takes effect in tests.
    """
    def get_rate(self):
        try:
            return api_settings.DEFAULT_THROTTLE_RATES[self.scope]
        except KeyError as exc:  # pragma: no cover
            raise ImproperlyConfigured(
                f"No default throttle rate set for '{self.scope}' scope"
            ) from exc

    def allow_request(self, request, view):
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated and user.is_staff:
            return True
        return super().allow_request(request, view)


class GetContentMetadataHourlyThrottle(NonStaffUserRateThrottleMixin, UserRateThrottle):
    scope = 'get_content_metadata_hour'


class GetContentMetadataMinuteThrottle(NonStaffUserRateThrottleMixin, UserRateThrottle):
    scope = 'get_content_metadata_minute'
