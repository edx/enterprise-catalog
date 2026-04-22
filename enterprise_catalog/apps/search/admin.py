"""
Admin registrations for the search app.
"""
from django.contrib import admin
from djangoql.admin import DjangoQLSearchMixin

from enterprise_catalog.apps.search.models import ContentMetadataIndexingState


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
