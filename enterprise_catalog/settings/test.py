import os

from enterprise_catalog.settings.base import *
import tempfile

LMS_BASE_URL = 'https://edx.test.lms'
DISCOVERY_SERVICE_API_URL = 'https://edx.test.discovery/'
DISCOVERY_SERVICE_URL = 'https://edx.test.discovery/'
ENTERPRISE_LEARNER_PORTAL_BASE_URL = 'https://edx.test.learnerportal'
ECOMMERCE_BASE_URL = 'https://edx.test.ecommerce/'
LICENSE_MANAGER_BASE_URL = 'https://edx.test.licensemanager/'
STUDIO_BASE_URL = 'https://edx.test.cms'

# IN-MEMORY TEST DATABASE
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
        'USER': '',
        'PASSWORD': '',
        'HOST': '',
        'PORT': '',
    },
}
# END IN-MEMORY TEST DATABASE

# CELERY
CELERY_TASK_ALWAYS_EAGER = True
# END CELERY

results_dir = tempfile.TemporaryDirectory()
CELERY_RESULT_BACKEND = f'file://{results_dir.name}'

# A faster (but less secure) password hasher like MD5 makes UserFactory faster, shaving ~80% off
# test runtimes compared with the more secure PBKDF2-based hasher used in production.
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

# Disable API throttling by default in tests; individual tests can re-enable
# specific rates via ``override_settings``. DRF treats a ``None`` rate as no limit.
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    'DEFAULT_THROTTLE_RATES': {
        'get_content_metadata_hour': None,
        'get_content_metadata_minute': None,
    },
}
