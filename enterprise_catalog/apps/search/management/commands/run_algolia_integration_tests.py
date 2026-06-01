"""
Management command for running Phase 4a/4b Algolia integration tests.
"""
import concurrent.futures
import logging
import os
import sys
import time
import traceback

from algoliasearch.search_client import SearchClient
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from enterprise_catalog.apps.catalog.algolia_utils import ALGOLIA_INDEX_SETTINGS
from enterprise_catalog.apps.search.indexing_mappings import (
    invalidate_indexing_mappings_cache,
)
from tests.integration.algolia_reindexing import loader, scenarios


logger = logging.getLogger(__name__)

SCENARIOS = {
    '1': ('Force Index All (4a)', scenarios.scenario_1_force_index_all),
    '2': ('Stale Detection (4a)', scenarios.scenario_2_stale_detection),
    '3': ('Per-Catalog Dispatch (4b)', scenarios.scenario_3_per_catalog_dispatch),
    '4': ('Membership Removal (4b)', scenarios.scenario_4_membership_removal),
}


class Command(BaseCommand):
    help = 'Run integration tests for Phase 4a/4b Algolia incremental indexing'

    def add_arguments(self, parser):
        parser.add_argument(
            '--scenario',
            choices=['all', '1', '2', '3', '4'],
            default='all',
            help='Which scenario to run (default: all)'
        )
        parser.add_argument(
            '--no-cleanup',
            action='store_true',
            help='Skip DB + Algolia cleanup after the run'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Run dispatchers with dry_run=True (no Algolia writes)'
        )
        parser.add_argument(
            '--algolia-wait',
            type=int,
            default=10,
            help='Seconds to wait for Algolia indexing (default: 10)'
        )
        parser.add_argument(
            '--init-index',
            action='store_true',
            help=(
                'Apply ALGOLIA_INDEX_SETTINGS to the test index before running scenarios. '
                'Required the first time you use a fresh Algolia test index; safe to skip '
                'on subsequent runs once settings are already configured.'
            )
        )

    def handle(self, *args, **options):
        """Main command handler."""
        scenario_choice = options['scenario']
        no_cleanup = options['no_cleanup']
        dry_run = options['dry_run']
        algolia_wait = options['algolia_wait']
        init_index = options['init_index']

        self._validate_and_apply_settings()

        scenario_ids = ['1', '2', '3', '4'] if scenario_choice == 'all' else [scenario_choice]

        content_map, cq_map = self._load_fixtures()
        algolia_index = self._init_algolia_index(init_index)

        results = {}
        try:
            for scenario_id in scenario_ids:
                scenario_name, scenario_func = SCENARIOS[scenario_id]
                results[scenario_id] = self._run_scenario(
                    scenario_name, scenario_func, content_map, cq_map,
                    algolia_index, dry_run, algolia_wait,
                )
        finally:
            self._cleanup(no_cleanup, algolia_index)

        self._print_summary(results)

        failed_count = sum(1 for status, _, _ in results.values() if status == 'FAIL')
        if failed_count > 0:
            sys.exit(1)

    def _load_fixtures(self):
        """Load test fixtures and return (content_map, cq_map)."""
        try:
            content_map, cq_map, _ = loader.load_fixtures()
            self.stdout.write(self.style.SUCCESS('✓ Loaded fixtures'))
            return content_map, cq_map
        except Exception as exc:
            raise CommandError(f'Failed to load fixtures: {exc}') from exc

    def _init_algolia_index(self, init_index):
        """Initialize and return the Algolia index handle."""
        try:
            algolia_app_id = settings.ALGOLIA['APPLICATION_ID']
            algolia_api_key = settings.ALGOLIA['API_KEY']
            algolia_index_name = settings.ALGOLIA['INDEX_NAME']

            algolia_client = SearchClient.create(algolia_app_id, algolia_api_key)
            algolia_index = algolia_client.init_index(algolia_index_name)

            if init_index:
                # Strip 'replicas' — that key belongs to the production replica index,
                # causing a 400 if sent to a fresh test index.
                test_index_settings = {k: v for k, v in ALGOLIA_INDEX_SETTINGS.items() if k != 'replicas'}
                response = algolia_index.set_settings(test_index_settings)
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(response.wait)
                    try:
                        future.result(timeout=30)
                    except concurrent.futures.TimeoutError:
                        self.stdout.write(
                            self.style.WARNING('⚠ settings wait timed out (index likely already configured)')
                        )
                self.stdout.write(
                    self.style.SUCCESS(f'✓ Connected to Algolia index: {algolia_index_name} (settings applied)')
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS(f'✓ Connected to Algolia index: {algolia_index_name}')
                )
            return algolia_index
        except Exception as exc:
            raise CommandError(f'Failed to connect to Algolia: {exc}') from exc

    def _run_scenario(self, scenario_name, scenario_func, content_map, cq_map, algolia_index, dry_run, algolia_wait):
        """Run one scenario and return a (status, duration, exc_or_None) tuple."""
        self.stdout.write(f'\n{self.style.HTTP_INFO("Running")} {scenario_name}...')
        invalidate_indexing_mappings_cache()
        start_time = time.time()
        try:
            if dry_run:
                self.stdout.write(self.style.WARNING('  [DRY-RUN MODE]'))
            scenario_func(content_map, cq_map, algolia_index, wait_seconds=algolia_wait, dry_run=dry_run)
            duration = time.time() - start_time
            self.stdout.write(self.style.SUCCESS(f'  ✓ PASS ({duration:.1f}s)'))
            return ('PASS', duration, None)
        except Exception as exc:  # pylint: disable=broad-except
            duration = time.time() - start_time
            self.stdout.write(self.style.ERROR(f'  ✗ FAIL ({duration:.1f}s)'))
            self.stdout.write(self.style.ERROR(f'    {str(exc)}'))
            self.stdout.write(self.style.ERROR(traceback.format_exc()))
            return ('FAIL', duration, exc)

    def _cleanup(self, no_cleanup, algolia_index):
        """Remove DB fixtures and Algolia objects unless --no-cleanup was passed."""
        if not no_cleanup:
            self.stdout.write('\nCleaning up...')
            try:
                loader.cleanup_algolia_objects(algolia_index)
                loader.cleanup_db_fixtures()
                self.stdout.write(self.style.SUCCESS('✓ Cleanup completed'))
            except Exception as exc:  # pylint: disable=broad-except
                self.stdout.write(self.style.WARNING(f'⚠ Cleanup warning: {exc}'))
        else:
            self.stdout.write(self.style.WARNING('⚠ Cleanup skipped (--no-cleanup)'))

    def _validate_and_apply_settings(self):
        """Validate env vars and apply runtime settings."""
        required_env_vars = [
            'ALGOLIA_APP_ID',
            'ALGOLIA_API_KEY',
            'ALGOLIA_INDEX_NAME',
            'ALGOLIA_REPLICA_INDEX_NAME',
        ]

        missing_vars = [var for var in required_env_vars if not os.environ.get(var)]
        if missing_vars:
            raise CommandError(
                f'Missing required environment variables: {", ".join(missing_vars)}'
            )

        index_name = os.environ['ALGOLIA_INDEX_NAME']
        if 'prod' in index_name.lower():
            raise CommandError(
                f'Refusing to run integration tests against a production index: "{index_name}". '
                'Use a dedicated test index (name must not contain "prod").'
            )

        settings.ALGOLIA = {
            'APPLICATION_ID': os.environ['ALGOLIA_APP_ID'],
            'API_KEY': os.environ['ALGOLIA_API_KEY'],
            'INDEX_NAME': index_name,
            'REPLICA_INDEX_NAME': os.environ['ALGOLIA_REPLICA_INDEX_NAME'],
        }

        settings.CELERY_TASK_ALWAYS_EAGER = True
        settings.CELERY_TASK_EAGER_PROPAGATES = True
        # Make Algolia save/delete ops synchronous so assertions don't race
        # against async index propagation.  Production code defaults to False.
        settings.ALGOLIA_WAIT_FOR_TASKS = True

        # Suppress verbose per-course indexability logging that fires during
        # get_indexing_mappings() recomputation.  That function scans ALL
        # ContentMetadata to build mappings, logging one line per non-indexable
        # course.  Our fixture content is still processed correctly; this noise
        # is purely cosmetic but obscures real test output.
        logging.getLogger('enterprise_catalog.apps.catalog.algolia_utils').setLevel(logging.WARNING)

        self.stdout.write(
            self.style.SUCCESS(
                f'✓ Applied runtime settings (Algolia: {settings.ALGOLIA["INDEX_NAME"]})'
            )
        )

    def _print_summary(self, results):
        """Print summary table of scenario results."""
        self.stdout.write('\n' + '=' * 70)
        self.stdout.write('SUMMARY')
        self.stdout.write('=' * 70)

        header = f'{"Scenario":<30} {"Status":<10} {"Duration":<12}'
        self.stdout.write(header)
        self.stdout.write('-' * 70)

        scenario_names = {
            '1': 'Force Index All (4a)',
            '2': 'Stale Detection (4a)',
            '3': 'Per-Catalog Dispatch (4b)',
            '4': 'Membership Removal (4b)',
        }

        for scenario_id in sorted(results.keys(), key=int):
            status, duration, _ = results[scenario_id]
            scenario_name = scenario_names[scenario_id]
            duration_str = f'{duration:.1f}s'
            style_fn = self.style.SUCCESS if status == 'PASS' else self.style.ERROR
            self.stdout.write(style_fn(f'{scenario_name:<30} {status:<10} {duration_str:<12}'))

        self.stdout.write('=' * 70)
