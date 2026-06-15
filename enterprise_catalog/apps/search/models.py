"""
Models for the search app.

Tracks per-record Algolia indexing state so that incremental indexing can detect
stale records, retry failures, and clean up orphaned index shards.
"""
import uuid

from config_models.models import ConfigurationModel
from django.db import models
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel

from enterprise_catalog.apps.catalog.constants import (
    COURSE,
    LEARNER_PATHWAY,
    PROGRAM,
    VIDEO,
)
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

        Clears any prior failure state and any prior ``removed_from_index_at``
        timestamp (so REMOVED→INDEXED transitions don't leave a stale removal
        timestamp on the row), stores the Algolia object IDs produced, and
        stamps ``last_indexed_at``.
        """
        self.last_indexed_at = indexed_at or localized_utcnow()
        if algolia_object_ids is not None:
            self.algolia_object_ids = list(algolia_object_ids)
        self.last_failure_at = None
        self.failure_reason = None
        self.removed_from_index_at = None
        self.save(update_fields=[
            'last_indexed_at',
            'algolia_object_ids',
            'last_failure_at',
            'failure_reason',
            'removed_from_index_at',
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


_ALL_CONTENT_TYPES = [COURSE, PROGRAM, LEARNER_PATHWAY, VIDEO]


class IncrementalReindexAlgoliaConfig(ConfigurationModel):
    """
    DB-driven configuration for the ``incremental_reindex_algolia`` management command.

    When the most recent row has ``enabled=True``, the values stored here take
    precedence over whatever flags are passed on the command line.  This lets
    operators schedule a one-off ``--force-all`` run (or toggle ``--dry-run``)
    without touching the cron arguments or triggering a deploy.

    Flip ``enabled`` back to ``False`` on the next row to return to CLI-driven
    defaults.

    .. no_pii:
    """

    force_all = models.BooleanField(
        default=False,
        verbose_name=_('Force all'),
        help_text=_(
            'Reindex all indexable content regardless of staleness. '
            'Equivalent to --force-all on the command line.'
        ),
    )
    dry_run = models.BooleanField(
        default=False,
        verbose_name=_('Dry run'),
        help_text=_(
            'Log what would be dispatched without issuing any Algolia writes. '
            'Equivalent to --dry-run on the command line.'
        ),
    )
    no_async = models.BooleanField(
        default=False,
        verbose_name=_('No async'),
        help_text=_(
            'Run the dispatcher synchronously (blocks until complete). '
            'Equivalent to --no-async on the command line.'
        ),
    )
    index_name = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name=_('Index name'),
        help_text=_(
            'Target Algolia index name. Leave blank to use the command-line value or the default.'
        ),
    )
    replica_index_name = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name=_('Replica index name'),
        help_text=_(
            'Algolia replica index name. Leave blank to use the command-line value or the default.'
        ),
    )
    content_types = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name=_('Content types'),
        help_text=(
            f'Comma-separated subset of content types to reindex: '
            f'{", ".join(_ALL_CONTENT_TYPES)}. '
            f'Leave blank to reindex all types.'
        ),
    )

    class Meta:
        verbose_name = 'Incremental Reindex Algolia Config'
        verbose_name_plural = 'Incremental Reindex Algolia Configs'

    @classmethod
    def current_options(cls):
        """
        Return a dict of command options from the current configuration row.

        When the current row is disabled (or no row exists), returns an empty
        dict so the caller falls back to command-line arguments unchanged.

        Non-empty string fields only override their corresponding option when
        they carry a value; blank means "leave the CLI value alone."
        """
        config = cls.current()
        if not config.enabled:
            return {}

        opts = {
            'force_all': config.force_all,
            'dry_run': config.dry_run,
            'no_async': config.no_async,
        }
        if config.index_name:
            opts['index_name'] = config.index_name
        if config.replica_index_name:
            opts['replica_index_name'] = config.replica_index_name
        if config.content_types:
            parsed = [t.strip() for t in config.content_types.split(',') if t.strip()]
            valid = [t for t in parsed if t in _ALL_CONTENT_TYPES]
            if valid:
                opts['content_types'] = valid
        return opts
