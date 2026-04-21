"""
Models for the search app.

Tracks per-record Algolia indexing state so that incremental indexing can detect
stale records, retry failures, and clean up orphaned index shards.
"""
import uuid

from django.db import models
from model_utils.models import TimeStampedModel

from enterprise_catalog.apps.catalog.models import ContentMetadata
from enterprise_catalog.apps.catalog.utils import localized_utcnow


class ContentMetadataIndexingState(TimeStampedModel):
    """
    Tracks per-record Algolia indexing state for a ContentMetadata.

    A record exists at most once per ContentMetadata. It records when the
    content was last successfully indexed, the Algolia object IDs produced
    (so orphaned shards can be deleted), and the most recent failure (if any).

    .. no_pii:
    """
    uuid = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    content_metadata = models.OneToOneField(
        ContentMetadata,
        on_delete=models.CASCADE,
        related_name='indexing_state',
    )
    last_indexed_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text='When this content was last successfully written to Algolia.',
    )
    removed_from_index_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text='When this content was last removed from Algolia.',
    )
    algolia_object_ids = models.JSONField(
        default=list,
        blank=True,
        help_text='Algolia object IDs (shards) produced for this content on last index.',
    )
    last_failure_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text='When the most recent indexing failure occurred, if any.',
    )
    failure_reason = models.TextField(
        null=True,
        blank=True,
        help_text='Reason for the most recent indexing failure, if any.',
    )

    class Meta:
        verbose_name = 'Content Metadata Indexing State'
        verbose_name_plural = 'Content Metadata Indexing States'

    def __str__(self):
        return f'<ContentMetadataIndexingState for {self.content_metadata.content_key}>'

    @property
    def is_stale(self):
        """
        True when the ContentMetadata has been modified since it was last indexed
        (or was never indexed at all).
        """
        if self.last_indexed_at is None:
            return True
        return self.content_metadata.modified > self.last_indexed_at

    def mark_as_indexed(self, algolia_object_ids=None, indexed_at=None):
        """
        Record a successful index operation.

        Clears any prior failure state, stores the Algolia object IDs produced,
        and stamps ``last_indexed_at``.
        """
        self.last_indexed_at = indexed_at or localized_utcnow()
        if algolia_object_ids is not None:
            self.algolia_object_ids = list(algolia_object_ids)
        self.last_failure_at = None
        self.failure_reason = None
        self.save(update_fields=[
            'last_indexed_at',
            'algolia_object_ids',
            'last_failure_at',
            'failure_reason',
            'modified',
        ])

    def mark_as_failed(self, reason, failed_at=None):
        """
        Record a failed index operation. Does not modify ``last_indexed_at``.
        """
        self.last_failure_at = failed_at or localized_utcnow()
        self.failure_reason = str(reason) if reason is not None else None
        self.save(update_fields=[
            'last_failure_at',
            'failure_reason',
            'modified',
        ])

    def mark_as_removed(self, removed_at=None):
        """
        Record that the content was removed from the index.
        """
        self.removed_from_index_at = removed_at or localized_utcnow()
        self.save(update_fields=[
            'removed_from_index_at',
            'modified',
        ])

    @classmethod
    def get_or_create_for_content(cls, content_metadata):
        """
        Return the indexing state for ``content_metadata``, creating one with
        empty timestamps on first access.
        """
        return cls.objects.get_or_create(content_metadata=content_metadata)
