"""
Management command for inspecting the catalog-content association status.

Allows operators to quickly answer questions like:
  - "How many content items are in catalog X?"
  - "What types of content does catalog X contain?"
  - "Which catalogs does enterprise Y have?"

Usage examples:
  ./manage.py catalog_content_status --catalog-uuid <uuid>
  ./manage.py catalog_content_status --enterprise-uuid <uuid>
  ./manage.py catalog_content_status --catalog-uuid <uuid> --show-content-keys
"""
import logging

from django.core.management.base import BaseCommand
from django.db.models import Count

from enterprise_catalog.apps.catalog.models import (
    ContentMetadata,
    EnterpriseCatalog,
)


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Inspect catalog-content association status for a given catalog or enterprise. '
        'Useful for debugging sync issues and verifying catalog membership.'
    )

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            '--catalog-uuid',
            dest='catalog_uuid',
            help='UUID of a specific EnterpriseCatalog to inspect.',
        )
        group.add_argument(
            '--enterprise-uuid',
            dest='enterprise_uuid',
            help='UUID of an enterprise to list all catalogs and their content counts.',
        )
        parser.add_argument(
            '--show-content-keys',
            dest='show_content_keys',
            action='store_true',
            default=False,
            help=(
                'Print the content_key of every associated ContentMetadata record. '
                'Only valid with --catalog-uuid. May produce very long output for large catalogs.'
            ),
        )

    def handle(self, *args, **options):
        catalog_uuid = options.get('catalog_uuid')
        enterprise_uuid = options.get('enterprise_uuid')
        show_content_keys = options.get('show_content_keys', False)

        if catalog_uuid:
            self._handle_single_catalog(catalog_uuid, show_content_keys)
        else:
            self._handle_enterprise(enterprise_uuid)

    def _handle_single_catalog(self, catalog_uuid, show_content_keys):
        """Print detailed content status for a single catalog."""
        try:
            catalog = EnterpriseCatalog.objects.select_related('catalog_query').get(uuid=catalog_uuid)
        except EnterpriseCatalog.DoesNotExist:
            self.stderr.write(self.style.ERROR(f'No EnterpriseCatalog found with uuid={catalog_uuid}'))
            return

        self.stdout.write(self.style.SUCCESS(f'\nCatalog: {catalog.title}'))
        self.stdout.write(f'  UUID:              {catalog.uuid}')
        self.stdout.write(f'  Enterprise UUID:   {catalog.enterprise_uuid}')
        self.stdout.write(f'  Enterprise Name:   {catalog.enterprise_name}')

        if not catalog.catalog_query:
            self.stderr.write(self.style.WARNING('  CatalogQuery:      (none — catalog has no associated query)'))
            return

        query = catalog.catalog_query
        self.stdout.write(f'  CatalogQuery UUID: {query.uuid}')
        self.stdout.write(f'  CatalogQuery:      {query.short_str_for_listings()}')
        self.stdout.write(f'  Query modified:    {query.modified}')

        # Aggregate content counts by type
        content_qs = query.contentmetadata_set.all()
        total_count = content_qs.count()
        by_type = (
            content_qs
            .values('content_type')
            .annotate(count=Count('id'))
            .order_by('content_type')
        )

        self.stdout.write(f'\n  Total content items: {total_count}')
        self.stdout.write('  Breakdown by content_type:')
        for row in by_type:
            self.stdout.write(f'    {row["content_type"]:20s}  {row["count"]}')

        if show_content_keys:
            self.stdout.write('\n  Content keys:')
            for cm in content_qs.only('content_key', 'content_type').order_by('content_type', 'content_key'):
                self.stdout.write(f'    [{cm.content_type}]  {cm.content_key}')

        self.stdout.write('')

    def _handle_enterprise(self, enterprise_uuid):
        """Print a summary of all catalogs and their content counts for an enterprise."""
        catalogs = (
            EnterpriseCatalog.objects
            .filter(enterprise_uuid=enterprise_uuid)
            .select_related('catalog_query')
            .annotate(content_count=Count('catalog_query__contentmetadata', distinct=True))
            .order_by('title')
        )

        if not catalogs.exists():
            self.stderr.write(
                self.style.WARNING(f'No catalogs found for enterprise_uuid={enterprise_uuid}')
            )
            return

        self.stdout.write(self.style.SUCCESS(f'\nCatalogs for enterprise {enterprise_uuid}:\n'))
        self.stdout.write(f'  {"Title":<40}  {"Catalog UUID":<40}  {"# Content"}')
        self.stdout.write('  ' + '-' * 85)
        for catalog in catalogs:
            self.stdout.write(
                f'  {catalog.title:<40}  {str(catalog.uuid):<40}  {catalog.content_count}'
            )
        self.stdout.write('')
