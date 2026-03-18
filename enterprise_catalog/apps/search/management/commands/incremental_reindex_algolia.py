"""
Management command for incremental Algolia reindexing.
"""
import logging

from django.core.management.base import BaseCommand

from enterprise_catalog.apps.catalog.constants import (
    COURSE,
    LEARNER_PATHWAY,
    PROGRAM,
)
from enterprise_catalog.apps.search.tasks import dispatch_algolia_indexing


logger = logging.getLogger(__name__)

# Map CLI content type names to constants
CONTENT_TYPE_CHOICES = {
    'course': COURSE,
    'program': PROGRAM,
    'learnerpathway': LEARNER_PATHWAY,
}


class Command(BaseCommand):
    help = (
        'Incrementally reindex content in Algolia. Unlike the full reindex_algolia '
        'command, this only indexes content that has changed since the last indexing run.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--content-type',
            dest='content_type',
            choices=list(CONTENT_TYPE_CHOICES.keys()),
            default=None,
            help=(
                'Content type to reindex (course, program, learnerpathway). '
                'If not specified, all content types will be reindexed.'
            ),
        )
        parser.add_argument(
            '--index-name',
            dest='index_name',
            default=None,
            help=(
                'Custom Algolia index name to use for indexing. '
                'Useful for testing against a v2 index before cutover.'
            ),
        )
        parser.add_argument(
            '--force-all',
            dest='force_all',
            action='store_true',
            default=False,
            help=(
                'Force reindexing of all content regardless of staleness. '
                'Useful for initial population of a new index.'
            ),
        )
        parser.add_argument(
            '--dry-run',
            dest='dry_run',
            action='store_true',
            default=False,
            help=(
                'Log what would be indexed without actually dispatching tasks.'
            ),
        )
        parser.add_argument(
            '--no-async',
            dest='no_async',
            action='store_true',
            default=False,
            help='Run the task synchronously (without celery).',
        )

    def handle(self, *args, **options):
        """
        Run the incremental reindex by dispatching the Algolia indexing task.
        """
        content_type_name = options.get('content_type')
        content_type = CONTENT_TYPE_CHOICES.get(content_type_name) if content_type_name else None
        index_name = options.get('index_name')
        force_all = options.get('force_all', False)
        dry_run = options.get('dry_run', False)
        no_async = options.get('no_async', False)

        self.stdout.write(
            f'Starting incremental Algolia reindex '
            f'(content_type={content_type_name or "all"}, '
            f'index_name={index_name or "default"}, '
            f'force_all={force_all}, '
            f'dry_run={dry_run})'
        )

        task_kwargs = {
            'content_type': content_type,
            'force': force_all,
            'index_name': index_name,
            'dry_run': dry_run,
        }

        try:
            if no_async:
                self.stdout.write('Running dispatch_algolia_indexing synchronously...')
                result = dispatch_algolia_indexing.apply(kwargs=task_kwargs).get()
            else:
                self.stdout.write('Dispatching dispatch_algolia_indexing task...')
                result = dispatch_algolia_indexing.apply_async(kwargs=task_kwargs).get()

            self.stdout.write(self.style.SUCCESS(
                f'Incremental reindex complete. Results: {result}'
            ))

            # Print summary
            if result:
                self.stdout.write(f"Total tasks dispatched: {result.get('dispatched_tasks', 0)}")
                for ctype, stats in result.get('content_types', {}).items():
                    self.stdout.write(
                        f"  {ctype}: {stats.get('total_content_keys', 0)} content keys, "
                        f"{stats.get('batches_dispatched', 0)} batches"
                    )

        except Exception as exc:  # pylint: disable=broad-exception-caught
            # Handle TaskRecentlyRunError gracefully
            if type(exc).__name__ == 'TaskRecentlyRunError':
                self.stdout.write(self.style.WARNING(
                    'dispatch_algolia_indexing was recently run and was skipped. '
                    'Use --force-all to override.'
                ))
            else:
                self.stdout.write(self.style.ERROR(
                    f'Error during incremental reindex: {exc}'
                ))
                raise
