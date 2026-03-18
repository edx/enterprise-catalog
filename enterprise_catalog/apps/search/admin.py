"""
Admin configuration for the search app.
"""
from django.contrib import admin

from enterprise_catalog.apps.search.models import ContentMetadataIndexingState


@admin.register(ContentMetadataIndexingState)
class ContentMetadataIndexingStateAdmin(admin.ModelAdmin):
    """
    Admin for ContentMetadataIndexingState.
    """
    list_display = (
        'content_metadata',
        'last_indexed_at',
        'last_failure_at',
        'removed_from_index_at',
        'created',
        'modified',
    )
    list_filter = (
        'last_failure_at',
        'removed_from_index_at',
    )
    search_fields = (
        'content_metadata__content_key',
    )
    readonly_fields = (
        'created',
        'modified',
        'content_metadata',
    )
    raw_id_fields = (
        'content_metadata',
    )
