"""
Tests for the incremental_reindex_algolia management command.
"""
from io import StringIO
from unittest import mock

import ddt
from django.core.management import call_command
from django.test import TestCase, override_settings

from enterprise_catalog.apps.catalog.constants import (
    COURSE,
    LEARNER_PATHWAY,
    PROGRAM,
)


ALGOLIA_SETTINGS = {
    'APPLICATION_ID': 'test-app-id',
    'API_KEY': 'test-api-key',
    'SEARCH_API_KEY': 'test-search-key',
    'INDEX_NAME': 'test-index',
    'REPLICA_INDEX_NAME': 'test-replica-index',
}


@ddt.ddt
@override_settings(ALGOLIA=ALGOLIA_SETTINGS)
class IncrementalReindexAlgoliaCommandTests(TestCase):
    """
    Tests for the incremental_reindex_algolia management command.
    """

    @mock.patch(
        'enterprise_catalog.apps.search.management.commands.'
        'incremental_reindex_algolia.dispatch_algolia_indexing'
    )
    def test_command_runs_successfully(self, mock_task):
        """
        Test that the command runs and dispatches the task.
        """
        mock_task.apply_async.return_value.get.return_value = {
            'dispatched_tasks': 3,
            'content_types': {
                COURSE: {'total_content_keys': 100, 'batches_dispatched': 10},
                PROGRAM: {'total_content_keys': 20, 'batches_dispatched': 2},
                LEARNER_PATHWAY: {'total_content_keys': 5, 'batches_dispatched': 1},
            }
        }

        out = StringIO()
        call_command('incremental_reindex_algolia', stdout=out)

        mock_task.apply_async.assert_called_once_with(kwargs={
            'content_type': None,
            'force': False,
            'index_name': None,
            'dry_run': False,
        })

        output = out.getvalue()
        self.assertIn('Incremental reindex complete', output)
        self.assertIn('Total tasks dispatched: 3', output)

    @mock.patch(
        'enterprise_catalog.apps.search.management.commands.'
        'incremental_reindex_algolia.dispatch_algolia_indexing'
    )
    def test_command_with_content_type(self, mock_task):
        """
        Test that the command passes content_type argument correctly.
        """
        mock_task.apply_async.return_value.get.return_value = {
            'dispatched_tasks': 1,
            'content_types': {COURSE: {'total_content_keys': 50, 'batches_dispatched': 5}},
        }

        call_command('incremental_reindex_algolia', content_type='course')

        mock_task.apply_async.assert_called_once_with(kwargs={
            'content_type': COURSE,
            'force': False,
            'index_name': None,
            'dry_run': False,
        })

    @mock.patch(
        'enterprise_catalog.apps.search.management.commands.'
        'incremental_reindex_algolia.dispatch_algolia_indexing'
    )
    def test_command_with_program_content_type(self, mock_task):
        """
        Test that the command passes program content_type correctly.
        """
        mock_task.apply_async.return_value.get.return_value = {
            'dispatched_tasks': 1,
            'content_types': {PROGRAM: {'total_content_keys': 10, 'batches_dispatched': 1}},
        }

        call_command('incremental_reindex_algolia', content_type='program')

        mock_task.apply_async.assert_called_once_with(kwargs={
            'content_type': PROGRAM,
            'force': False,
            'index_name': None,
            'dry_run': False,
        })

    @mock.patch(
        'enterprise_catalog.apps.search.management.commands.'
        'incremental_reindex_algolia.dispatch_algolia_indexing'
    )
    def test_command_with_pathway_content_type(self, mock_task):
        """
        Test that the command passes learnerpathway content_type correctly.
        """
        mock_task.apply_async.return_value.get.return_value = {
            'dispatched_tasks': 1,
            'content_types': {
                LEARNER_PATHWAY: {'total_content_keys': 5, 'batches_dispatched': 1}
            },
        }

        call_command('incremental_reindex_algolia', content_type='learnerpathway')

        mock_task.apply_async.assert_called_once_with(kwargs={
            'content_type': LEARNER_PATHWAY,
            'force': False,
            'index_name': None,
            'dry_run': False,
        })

    @mock.patch(
        'enterprise_catalog.apps.search.management.commands.'
        'incremental_reindex_algolia.dispatch_algolia_indexing'
    )
    def test_command_with_index_name(self, mock_task):
        """
        Test that the command passes index_name argument correctly.
        """
        mock_task.apply_async.return_value.get.return_value = {'dispatched_tasks': 0}

        call_command('incremental_reindex_algolia', index_name='v2-test-index')

        mock_task.apply_async.assert_called_once_with(kwargs={
            'content_type': None,
            'force': False,
            'index_name': 'v2-test-index',
            'dry_run': False,
        })

    @mock.patch(
        'enterprise_catalog.apps.search.management.commands.'
        'incremental_reindex_algolia.dispatch_algolia_indexing'
    )
    def test_command_with_force_all(self, mock_task):
        """
        Test that the command passes force_all argument correctly.
        """
        mock_task.apply_async.return_value.get.return_value = {'dispatched_tasks': 0}

        call_command('incremental_reindex_algolia', force_all=True)

        mock_task.apply_async.assert_called_once_with(kwargs={
            'content_type': None,
            'force': True,
            'index_name': None,
            'dry_run': False,
        })

    @mock.patch(
        'enterprise_catalog.apps.search.management.commands.'
        'incremental_reindex_algolia.dispatch_algolia_indexing'
    )
    def test_command_with_dry_run(self, mock_task):
        """
        Test that the command passes dry_run argument correctly.
        """
        mock_task.apply_async.return_value.get.return_value = {'dispatched_tasks': 0}

        call_command('incremental_reindex_algolia', dry_run=True)

        mock_task.apply_async.assert_called_once_with(kwargs={
            'content_type': None,
            'force': False,
            'index_name': None,
            'dry_run': True,
        })

    @mock.patch(
        'enterprise_catalog.apps.search.management.commands.'
        'incremental_reindex_algolia.dispatch_algolia_indexing'
    )
    def test_command_with_no_async(self, mock_task):
        """
        Test that the command runs synchronously with --no-async.
        """
        mock_task.apply.return_value.get.return_value = {'dispatched_tasks': 0}

        call_command('incremental_reindex_algolia', no_async=True)

        # Should use apply() instead of apply_async()
        mock_task.apply.assert_called_once_with(kwargs={
            'content_type': None,
            'force': False,
            'index_name': None,
            'dry_run': False,
        })
        mock_task.apply_async.assert_not_called()

    @mock.patch(
        'enterprise_catalog.apps.search.management.commands.'
        'incremental_reindex_algolia.dispatch_algolia_indexing'
    )
    def test_command_with_all_options(self, mock_task):
        """
        Test that the command handles all options together.
        """
        mock_task.apply.return_value.get.return_value = {'dispatched_tasks': 10}

        call_command(
            'incremental_reindex_algolia',
            content_type='course',
            index_name='v2-index',
            force_all=True,
            dry_run=True,
            no_async=True,
        )

        mock_task.apply.assert_called_once_with(kwargs={
            'content_type': COURSE,
            'force': True,
            'index_name': 'v2-index',
            'dry_run': True,
        })
