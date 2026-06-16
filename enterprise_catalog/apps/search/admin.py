"""
Admin registrations for the search app.
"""
from config_models.admin import ConfigurationModelAdmin
from django.contrib import admin
from djangoql.admin import DjangoQLSearchMixin

from enterprise_catalog.apps.search.models import (
    ContentMetadataIndexingState,
    IncrementalReindexAlgoliaConfig,
)


@admin.register(IncrementalReindexAlgoliaConfig)
class IncrementalReindexAlgoliaConfigAdmin(ConfigurationModelAdmin):
    """
    Django admin for IncrementalReindexAlgoliaConfig.

    Uses ConfigurationModelAdmin which provides the standard config-model
    UI: always inserts a new row on save, shows the change history, and
    renders the is_active annotation in list view.
    """
    list_display = (
        'change_date',
        'changed_by',
        'enabled',
        'force_all',
        'dry_run',
        'no_async',
        'index_name',
        'content_types',
    )


@admin.register(ContentMetadataIndexingState)
class ContentMetadataIndexingStateAdmin(DjangoQLSearchMixin, admin.ModelAdmin):
    """
    Django admin for ContentMetadataIndexingState.
    """
    list_display = (
        'uuid',
        'content_metadata',
        'last_indexed_at',
        'last_failure_at',
        'removed_from_index_at',
        'modified',
    )
    list_filter = ('last_indexed_at', 'last_failure_at', 'removed_from_index_at')
    search_fields = ('content_metadata__content_key', 'uuid', 'failure_reason')
    autocomplete_fields = ('content_metadata',)
    readonly_fields = ('uuid', 'created', 'modified')
