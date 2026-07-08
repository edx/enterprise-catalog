"""
Management command for manual or scheduled incremental Algolia reindexing.

Invokes ``dispatch_algolia_indexing`` (Phase 4a) with configurable parameters
and prints a summary of dispatched tasks.
"""
import logging

from django.conf import settings
from django.core.management.base import BaseCommand

from enterprise_catalog.apps.api_client.algolia import AlgoliaSearchClient
from enterprise_catalog.apps.catalog.algolia_utils import (
    ALGOLIA_INDEX_SETTINGS,
    ALGOLIA_REPLICA_INDEX_SETTINGS,
    new_search_client_or_error,
)
from enterprise_catalog.apps.catalog.constants import (
    COURSE,
    LEARNER_PATHWAY,
    PROGRAM,
    VIDEO,
)
from enterprise_catalog.apps.search.models import (
    IncrementalReindexAlgoliaConfig,
)
from enterprise_catalog.apps.search.tasks import dispatch_algolia_indexing


logger = logging.getLogger(__name__)

_ALL_CONTENT_TYPES = [COURSE, PROGRAM, LEARNER_PATHWAY, VIDEO]


class Command(BaseCommand):
    help = 'Run incremental Algolia reindexing via the Phase 4a dispatcher'

    def add_arguments(self, parser):
        parser.add_argument(
            '--content-type',
            dest='content_types',
            choices=_ALL_CONTENT_TYPES,
            nargs='+',
            metavar='TYPE',
            help=(
                f'Limit reindexing to one or more content types: '
                f'{", ".join(_ALL_CONTENT_TYPES)}. Defaults to all types.'
            ),
        )
        parser.add_argument(
            '--index-name',
            dest='index_name',
            default=None,
            help='Target Algolia index name.',
        )
        parser.add_argument(
            '--replica-name',
            dest='replica_index_name',
            default=None,
            help=(
                'Algolia replica index name. '
                'Defaults to the index name suffixed with "_repl".'
            ),
        )
        parser.add_argument(
            '--force-all',
            dest='force_all',
            action='store_true',
            default=False,
            help='Reindex all indexable content regardless of staleness.',
        )
        parser.add_argument(
            '--dry-run',
            dest='dry_run',
            action='store_true',
            default=False,
            help='Log what would be dispatched without issuing any Algolia writes.',
        )
        parser.add_argument(
            '--no-async',
            dest='no_async',
            action='store_true',
            default=False,
            help='Run the dispatcher synchronously (blocks until complete; useful for debugging).',
        )

    def handle(self, *args, **options):
        config_overrides = IncrementalReindexAlgoliaConfig.current_options()
        if config_overrides:
            self.stdout.write(
                self.style.WARNING(
                    f'Config model override active: {config_overrides}'
                )
            )
        options.update(config_overrides)

        content_types = options['content_types']  # None means all types
        index_name = options['index_name'] or settings.ALGOLIA['INCREMENTAL_INDEX_NAME']
        replica_index_name = options['replica_index_name'] or f'{index_name}_repl'
        force_all = options['force_all']
        dry_run = options['dry_run']
        no_async = options['no_async']

        if dry_run:
            self.stdout.write(self.style.WARNING('[DRY-RUN] No Algolia writes will be made.'))
        else:
            self.stdout.write('Configuring Algolia index settings...')
            sdk_client = new_search_client_or_error()
            algolia_client = AlgoliaSearchClient()
            algolia_client.algolia_index = sdk_client.init_index(index_name)
            algolia_client.replica_index = sdk_client.init_index(replica_index_name)
            primary_settings = {**ALGOLIA_INDEX_SETTINGS, 'replicas': [f'virtual({replica_index_name})']}
            algolia_client.set_index_settings(primary_settings)
            algolia_client.set_index_settings(ALGOLIA_REPLICA_INDEX_SETTINGS, primary_index=False)

        self.stdout.write(f'Content types: {", ".join(content_types) if content_types else "all"}')
        self.stdout.write(f'Target index:  {index_name or "(not configured)"}')
        self.stdout.write(f'Force all:     {force_all}')
        self.stdout.write('')

        task_kwargs = {
            'force': force_all,
            'dry_run': dry_run,
            'index_name': index_name,
            'content_types': content_types,
            'use_apply': no_async,
        }

        if no_async:
            self.stdout.write('Running synchronously...')
            result = dispatch_algolia_indexing.apply(kwargs=task_kwargs).get()
        else:
            self.stdout.write('Dispatching via Celery...')
            result = dispatch_algolia_indexing.apply_async(kwargs=task_kwargs).get()

        self._print_summary(result)

    def _print_summary(self, result):
        """Print the dispatcher summary returned by dispatch_algolia_indexing."""
        if not result:
            self.stdout.write(self.style.WARNING('No summary returned from dispatcher.'))
            return

        dispatched = result.get('dispatched', {})

        self.stdout.write('')
        self.stdout.write('=' * 60)
        self.stdout.write('DISPATCH SUMMARY')
        self.stdout.write('=' * 60)
        self.stdout.write(f'{"Content Type":<20} {"Records":<10} {"Batches":<10}')
        self.stdout.write('-' * 60)

        total_records = 0
        total_batches = 0
        for content_type in _ALL_CONTENT_TYPES:
            counts = dispatched.get(content_type, {})
            records = counts.get('records', 0)
            batches = counts.get('batches', 0)
            total_records += records
            total_batches += batches
            self.stdout.write(f'{content_type:<20} {records:<10} {batches:<10}')

        self.stdout.write('-' * 60)
        self.stdout.write(f'{"TOTAL":<20} {total_records:<10} {total_batches:<10}')
        self.stdout.write('=' * 60)

        if result.get('dry_run'):
            self.stdout.write(self.style.WARNING('(dry-run — nothing was indexed)'))
        elif total_records == 0:
            self.stdout.write(self.style.SUCCESS('Nothing to index — all content is up to date.'))
        else:
            self.stdout.write(self.style.SUCCESS(f'Dispatched {total_records} records in {total_batches} batches.'))
