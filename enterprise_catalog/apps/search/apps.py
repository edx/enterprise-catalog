"""
Django app configuration for the search app.
"""
from django.apps import AppConfig


class SearchConfig(AppConfig):
    """
    Configuration for the search app.
    """
    name = 'enterprise_catalog.apps.search'
    default_auto_field = 'django.db.models.BigAutoField'
