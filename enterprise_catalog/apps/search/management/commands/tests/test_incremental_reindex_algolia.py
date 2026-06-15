"""
Unit tests for the incremental_reindex_algolia management command.
"""
from io import StringIO
from unittest import mock

import ddt
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from enterprise_catalog.apps.catalog.constants import (
    COURSE,
    LEARNER_PATHWAY,
    PROGRAM,
)


_CMD = 'enterprise_catalog.apps.search.management.commands.incremental_reindex_algolia'
TASK_PATH = f'{_CMD}.dispatch_algolia_indexing'
NEW_SDK_CLIENT_PATH = f'{_CMD}.new_search_client_or_error'
ALGOLIA_CLIENT_PATH = f'{_CMD}.AlgoliaSearchClient'

_SAMPLE_SUMMARY = {
    'force': False,
    'dry_run': False,
    'batch_size': 10,
    'index_name': None,
    'dispatched': {
        COURSE: {'records': 5, 'batches': 1},
        PROGRAM: {'records': 2, 'batches': 1},
        LEARNER_PATHWAY: {'records': 0, 'batches': 0},
    },
}


@ddt.ddt
class IncrementalReindexAlgoliaCommandTests(TestCase):
    command_name = 'incremental_reindex_algolia'

    def setUp(self):
        super().setUp()
        patcher_sdk = mock.patch(NEW_SDK_CLIENT_PATH)
        patcher_cls = mock.patch(ALGOLIA_CLIENT_PATH)
        self.mock_new_sdk_client = patcher_sdk.start()
        self.mock_algolia_cls = patcher_cls.start()
        self.addCleanup(patcher_sdk.stop)
        self.addCleanup(patcher_cls.stop)

    def _call(self, *args, **kwargs):
        out = StringIO()
        call_command(self.command_name, *args, stdout=out, **kwargs)
        return out.getvalue()

    # ------------------------------------------------------------------
    # Default (no-arg) invocation
    # ------------------------------------------------------------------

    @mock.patch(TASK_PATH)
    def test_default_invocation_uses_apply_async(self, mock_task):
        mock_task.apply_async.return_value.get.return_value = _SAMPLE_SUMMARY
        self._call()
        mock_task.apply_async.assert_called_once_with(
            kwargs={
                'force': False,
                'dry_run': False,
                'index_name': None,
                'content_types': None,
                'use_apply': False,
            }
        )
        mock_task.apply.assert_not_called()

    # ------------------------------------------------------------------
    # --no-async
    # ------------------------------------------------------------------

    @mock.patch(TASK_PATH)
    def test_no_async_uses_apply(self, mock_task):
        mock_task.apply.return_value.get.return_value = _SAMPLE_SUMMARY
        self._call('--no-async')
        mock_task.apply.assert_called_once_with(
            kwargs={
                'force': False,
                'dry_run': False,
                'index_name': None,
                'content_types': None,
                'use_apply': True,
            }
        )
        mock_task.apply_async.assert_not_called()

    # ------------------------------------------------------------------
    # --force-all
    # ------------------------------------------------------------------

    @mock.patch(TASK_PATH)
    def test_force_all_passes_force_true(self, mock_task):
        mock_task.apply_async.return_value.get.return_value = _SAMPLE_SUMMARY
        self._call('--force-all')
        _, kwargs = mock_task.apply_async.call_args
        assert kwargs['kwargs']['force'] is True

    # ------------------------------------------------------------------
    # --dry-run
    # ------------------------------------------------------------------

    @mock.patch(TASK_PATH)
    def test_dry_run_passes_dry_run_true(self, mock_task):
        dry_run_summary = {**_SAMPLE_SUMMARY, 'dry_run': True}
        mock_task.apply_async.return_value.get.return_value = dry_run_summary
        output = self._call('--dry-run')
        _, kwargs = mock_task.apply_async.call_args
        assert kwargs['kwargs']['dry_run'] is True
        assert 'DRY-RUN' in output

    @mock.patch(TASK_PATH)
    def test_dry_run_summary_note(self, mock_task):
        dry_run_summary = {**_SAMPLE_SUMMARY, 'dry_run': True}
        mock_task.apply_async.return_value.get.return_value = dry_run_summary
        output = self._call('--dry-run')
        assert 'dry-run' in output.lower()

    # ------------------------------------------------------------------
    # --index-name
    # ------------------------------------------------------------------

    @mock.patch(TASK_PATH)
    def test_index_name_passed_through(self, mock_task):
        mock_task.apply_async.return_value.get.return_value = _SAMPLE_SUMMARY
        self._call('--index-name', 'enterprise_catalog_v2')
        _, kwargs = mock_task.apply_async.call_args
        assert kwargs['kwargs']['index_name'] == 'enterprise_catalog_v2'

    @mock.patch(TASK_PATH)
    def test_index_name_always_shown_in_output(self, mock_task):
        mock_task.apply_async.return_value.get.return_value = _SAMPLE_SUMMARY
        output = self._call('--index-name', 'enterprise_catalog_v2')
        assert 'enterprise_catalog_v2' in output

    @override_settings(ALGOLIA={'INDEX_NAME': 'enterprise_catalog'})
    @mock.patch(TASK_PATH)
    def test_resolved_index_shown_when_no_index_name_flag(self, mock_task):
        """The primary index name from settings is printed even without --index-name."""
        mock_task.apply_async.return_value.get.return_value = _SAMPLE_SUMMARY
        output = self._call('--index-name', 'other_index')
        assert 'other_index' in output

    # ------------------------------------------------------------------
    # --content-type
    # ------------------------------------------------------------------

    @mock.patch(TASK_PATH)
    def test_single_content_type(self, mock_task):
        mock_task.apply_async.return_value.get.return_value = _SAMPLE_SUMMARY
        self._call('--content-type', COURSE)
        _, kwargs = mock_task.apply_async.call_args
        assert kwargs['kwargs']['content_types'] == [COURSE]

    @mock.patch(TASK_PATH)
    def test_multiple_content_types(self, mock_task):
        mock_task.apply_async.return_value.get.return_value = _SAMPLE_SUMMARY
        self._call('--content-type', COURSE, PROGRAM)
        _, kwargs = mock_task.apply_async.call_args
        assert set(kwargs['kwargs']['content_types']) == {COURSE, PROGRAM}

    @mock.patch(TASK_PATH)
    def test_no_content_type_passes_none(self, mock_task):
        mock_task.apply_async.return_value.get.return_value = _SAMPLE_SUMMARY
        self._call()
        _, kwargs = mock_task.apply_async.call_args
        assert kwargs['kwargs']['content_types'] is None

    @ddt.data(COURSE, PROGRAM, LEARNER_PATHWAY)
    @mock.patch(TASK_PATH)
    def test_valid_content_type_accepted(self, content_type, mock_task):
        mock_task.apply_async.return_value.get.return_value = _SAMPLE_SUMMARY
        self._call('--content-type', content_type)
        _, kwargs = mock_task.apply_async.call_args
        assert kwargs['kwargs']['content_types'] == [content_type]

    # ------------------------------------------------------------------
    # Index configuration
    # ------------------------------------------------------------------

    @mock.patch(TASK_PATH)
    def test_configure_index_called_before_dispatch(self, mock_task):
        mock_task.apply_async.return_value.get.return_value = _SAMPLE_SUMMARY
        self._call('--index-name', 'enterprise_catalog_v2')
        sdk = self.mock_new_sdk_client.return_value
        algolia_instance = self.mock_algolia_cls.return_value
        # The primary handle is initialized directly; the replica is configured by name via
        # set_index_settings(index_name=...), which initializes it lazily inside the client.
        sdk.init_index.assert_any_call('enterprise_catalog_v2')
        # set_index_settings called twice: primary (with replicas overridden) then replica
        assert algolia_instance.set_index_settings.call_count == 2
        primary_call_kwargs = algolia_instance.set_index_settings.call_args_list[0]
        primary_settings = primary_call_kwargs[0][0]
        assert primary_settings['replicas'] == ['virtual(enterprise_catalog_v2_repl)']
        replica_call = algolia_instance.set_index_settings.call_args_list[1]
        assert replica_call[1].get('index_name') == 'enterprise_catalog_v2_repl'

    @mock.patch(TASK_PATH)
    def test_explicit_replica_name_used(self, mock_task):
        mock_task.apply_async.return_value.get.return_value = _SAMPLE_SUMMARY
        self._call('--index-name', 'enterprise_catalog_v2', '--replica-name', 'my_replica')
        sdk = self.mock_new_sdk_client.return_value
        sdk.init_index.assert_any_call('enterprise_catalog_v2')
        algolia_instance = self.mock_algolia_cls.return_value
        primary_settings = algolia_instance.set_index_settings.call_args_list[0][0][0]
        assert primary_settings['replicas'] == ['virtual(my_replica)']
        # The replica is configured by name (which initializes it lazily inside the client).
        replica_call = algolia_instance.set_index_settings.call_args_list[1]
        assert replica_call[1].get('index_name') == 'my_replica'

    @mock.patch(TASK_PATH)
    def test_configure_index_skipped_on_dry_run(self, mock_task):
        mock_task.apply_async.return_value.get.return_value = {**_SAMPLE_SUMMARY, 'dry_run': True}
        self._call('--index-name', 'enterprise_catalog_v2', '--dry-run')
        self.mock_new_sdk_client.assert_not_called()
        self.mock_algolia_cls.assert_not_called()

    # ------------------------------------------------------------------
    # Summary output
    # ------------------------------------------------------------------

    @mock.patch(TASK_PATH)
    def test_summary_shows_record_counts(self, mock_task):
        mock_task.apply_async.return_value.get.return_value = _SAMPLE_SUMMARY
        output = self._call()
        assert 'DISPATCH SUMMARY' in output
        assert COURSE in output
        assert PROGRAM in output
        assert LEARNER_PATHWAY in output
        assert '5' in output  # course record count
        assert '2' in output  # program record count

    @mock.patch(TASK_PATH)
    def test_summary_shows_nothing_to_index_when_zero_records(self, mock_task):
        empty_summary = {
            'force': False,
            'dry_run': False,
            'batch_size': 10,
            'index_name': None,
            'dispatched': {
                COURSE: {'records': 0, 'batches': 0},
                PROGRAM: {'records': 0, 'batches': 0},
                LEARNER_PATHWAY: {'records': 0, 'batches': 0},
            },
        }
        mock_task.apply_async.return_value.get.return_value = empty_summary
        output = self._call()
        assert 'up to date' in output

    @mock.patch(TASK_PATH)
    def test_summary_warns_when_no_result_returned(self, mock_task):
        mock_task.apply_async.return_value.get.return_value = None
        output = self._call()
        assert 'No summary' in output

    # ------------------------------------------------------------------
    # Primary-index guard (remove at cutover)
    # ------------------------------------------------------------------

    @override_settings(ALGOLIA={'INDEX_NAME': 'enterprise_catalog'})
    def test_guard_blocks_default_invocation_against_primary_index(self):
        """No --index-name defaults to the primary index, which must be blocked."""
        with self.assertRaises(CommandError) as ctx:
            self._call()
        assert 'enterprise_catalog' in str(ctx.exception)

    @override_settings(ALGOLIA={'INDEX_NAME': 'enterprise_catalog'})
    def test_guard_blocks_explicit_primary_index_name(self):
        """--index-name matching the primary index is also blocked."""
        with self.assertRaises(CommandError):
            self._call('--index-name', 'enterprise_catalog')

    @override_settings(ALGOLIA={'INDEX_NAME': 'enterprise_catalog'})
    @mock.patch(TASK_PATH)
    def test_guard_allows_non_primary_index_name(self, mock_task):
        """--index-name pointing to a different index bypasses the guard."""
        mock_task.apply_async.return_value.get.return_value = _SAMPLE_SUMMARY
        output = self._call('--index-name', 'enterprise_catalog_v2')
        assert 'enterprise_catalog_v2' in output
