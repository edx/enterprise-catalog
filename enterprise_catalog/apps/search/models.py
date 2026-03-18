"""
Models for tracking Algolia indexing state.
"""
import logging

from django.db import models
from django.utils import timezone
from model_utils.models import TimeStampedModel

from enterprise_catalog.apps.catalog.models import ContentMetadata


LOGGER = logging.getLogger(__name__)


class ContentMetadataIndexingState(TimeStampedModel):
    """
    Tracks per-record Algolia indexing state for ContentMetadata.

    This model enables efficient staleness queries and failure tracking
    without modifying ContentMetadata itself.

    .. no_pii:
    """

    content_metadata = models.OneToOneField(
        ContentMetadata,
        on_delete=models.CASCADE,
        related_name='indexing_state',
    )
    last_indexed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When this record was last successfully indexed to Algolia.',
    )
    removed_from_index_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When this record was removed from the Algolia index.',
    )
    algolia_object_ids = models.JSONField(
        default=list,
        help_text='List of Algolia object IDs (shards) for this content.',
    )
    last_failure_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When indexing last failed for this record.',
    )
    failure_reason = models.TextField(
        null=True,
        blank=True,
        help_text='The reason for the last indexing failure.',
    )

    class Meta:
        verbose_name = 'Content Metadata Indexing State'
        verbose_name_plural = 'Content Metadata Indexing States'
        indexes = [
            models.Index(fields=['last_indexed_at']),
            models.Index(fields=['last_failure_at']),
        ]

    def __str__(self):
        return f'IndexingState for {self.content_metadata.content_key}'

    def mark_as_indexed(self, algolia_object_ids=None):
        """
        Mark this content as successfully indexed.

        Args:
            algolia_object_ids: List of Algolia object IDs created for this content.
        """
        self.last_indexed_at = timezone.now()
        self.last_failure_at = None
        self.failure_reason = None
        self.removed_from_index_at = None
        if algolia_object_ids is not None:
            self.algolia_object_ids = algolia_object_ids
        self.save()

    def mark_as_failed(self, reason):
        """
        Mark this content as having failed indexing.

        Args:
            reason: String describing the failure reason.
        """
        self.last_failure_at = timezone.now()
        self.failure_reason = reason
        self.save()

    def mark_as_removed(self):
        """
        Mark this content as removed from the index.
        """
        self.removed_from_index_at = timezone.now()
        self.algolia_object_ids = []
        self.save()

    @property
    def is_stale(self):
        """
        Check if this content needs to be re-indexed.

        A record is stale if:
        - It has never been indexed (last_indexed_at is None)
        - The content was modified after it was last indexed
        """
        if self.last_indexed_at is None:
            return True
        return self.content_metadata.modified > self.last_indexed_at

    @classmethod
    def get_or_create_for_content(cls, content_metadata):
        """
        Get or create an indexing state for the given content metadata.
        """
        state, _ = cls.objects.get_or_create(content_metadata=content_metadata)
        return state
