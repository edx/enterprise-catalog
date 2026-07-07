from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from enterprise_catalog.apps.api.tasks import TaskRecentlyRunError
from enterprise_catalog.apps.catalog.models import (
    ContentMetadata,
    EnterpriseCatalog,
)
from enterprise_catalog.apps.catalog.tests.factories import (
    CatalogQueryFactory,
    ContentMetadataFactory,
    EnterpriseCatalogFactory,
)


class UpdateContentMetadataCommandTests(TestCase):
    command_name = 'update_content_metadata'

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.catalog_query_a = CatalogQueryFactory()
        cls.catalog_query_b = CatalogQueryFactory()
        cls.enterprise_catalog_a = EnterpriseCatalogFactory(catalog_query=cls.catalog_query_a)
        cls.enterprise_catalog_b = EnterpriseCatalogFactory(catalog_query=cls.catalog_query_b)

        ContentMetadataFactory.create_batch(3)

    def setUp(self):
        super().setUp()
        self.command_config_mock = mock.patch('enterprise_catalog.apps.catalog.models.CatalogUpdateCommandConfig')
        mock_config = self.command_config_mock.start()
        mock_config.current_config.return_value = {
            'force': False,
            'no_async': False,
        }

    def tearDown(self):
        super().tearDown()
        # clean up any stale test objects
        ContentMetadata.objects.all().delete()
        EnterpriseCatalog.objects.all().delete()

        self.command_config_mock.stop()

    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.dispatch_algolia_indexing')
    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.update_content_metadata.fetch_missing_course_metadata_task')
    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.update_content_metadata.fetch_missing_pathway_metadata_task')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.group')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.update_catalog_metadata_task')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.update_full_content_metadata_task')
    def test_update_content_metadata_for_all_queries(
        self, mock_full_metadata_task, mock_catalog_task, mock_group, mock_fetch_missing_pathway,
        mock_fetch_missing_course, mock_dispatch
    ):
        """
        Verify that the job creates an update task for every catalog query
        """
        call_command(self.command_name)
        assert mock_fetch_missing_pathway.si.call_args._get_call_arguments()[1] == {"force": False, "dry_run": False}
        assert mock_fetch_missing_course.si.call_args._get_call_arguments()[1] == {"force": False, "dry_run": False}

        mock_group.assert_called_once_with([
            mock_catalog_task.s(catalog_query_id=self.catalog_query_a, force=False, dry_run=False),
            mock_catalog_task.s(catalog_query_id=self.catalog_query_b, force=False, dry_run=False),
        ])
        mock_full_metadata_task.si.assert_called_once_with(force=False, dry_run=False)

    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.dispatch_algolia_indexing')
    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.update_content_metadata.fetch_missing_course_metadata_task')
    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.update_content_metadata.fetch_missing_pathway_metadata_task')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.group')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.update_catalog_metadata_task')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.update_full_content_metadata_task')
    def test_update_content_metadata_for_filtered_queries(
        self, mock_full_metadata_task, mock_catalog_task, mock_group, mock_fetch_missing_pathway,
        mock_fetch_missing_course, mock_dispatch
    ):
        """
        Verify that the job creates an update task for every catalog query that is used by
        at least one enterprise catalog.
        """
        # Create another catalog query that isn't used by any catalog, so shouldn't be updated
        CatalogQueryFactory()

        call_command(self.command_name)

        mock_fetch_missing_pathway.si.assert_called()
        mock_fetch_missing_course.si.assert_called()
        mock_group.assert_called_once_with([
            mock_catalog_task.s(catalog_query_id=self.catalog_query_a, force=False, dry_run=False),
            mock_catalog_task.s(catalog_query_id=self.catalog_query_b, force=False, dry_run=False),
        ])
        mock_full_metadata_task.si.assert_called_once_with(force=False, dry_run=False)

    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.dispatch_algolia_indexing')
    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.update_content_metadata.fetch_missing_course_metadata_task')
    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.update_content_metadata.fetch_missing_pathway_metadata_task')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.group')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.update_catalog_metadata_task')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.update_full_content_metadata_task')
    def test_force_update_content_metadata(
        self, mock_full_metadata_task, mock_catalog_task, mock_group, mock_fetch_missing_pathway,
        mock_fetch_missing_course, mock_dispatch
    ):
        """
        Verify that the job creates an update task for every catalog query
        """
        call_command(self.command_name, force=True)
        assert mock_fetch_missing_pathway.si.call_args._get_call_arguments()[1] == {"force": True, "dry_run": False}
        mock_group.assert_called_once_with([
            mock_catalog_task.s(catalog_query_id=self.catalog_query_a, force=True, dry_run=False),
            mock_catalog_task.s(catalog_query_id=self.catalog_query_b, force=True, dry_run=False),
        ])
        mock_full_metadata_task.si.assert_called_once_with(force=True, dry_run=False)

    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.dispatch_algolia_indexing')
    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.update_content_metadata.fetch_missing_course_metadata_task')
    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.update_content_metadata.fetch_missing_pathway_metadata_task')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.group')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.update_catalog_metadata_task')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.update_full_content_metadata_task')
    def test_update_content_metadata_no_async(
        self, mock_full_metadata_task, mock_catalog_task, mock_group, mock_fetch_missing_pathway,
        mock_fetch_missing_course, mock_dispatch
    ):
        """
        Verify that the tasks are executed synchronously when --no-async flag is set
        """
        call_command(self.command_name, force=True, no_async=True)
        mock_fetch_missing_pathway.apply.assert_called_once_with(kwargs={"force": True, "dry_run": False})
        mock_fetch_missing_course.apply.assert_called_once_with(kwargs={"force": True, "dry_run": False})
        mock_group.assert_called_once_with([
            mock_catalog_task.s(catalog_query_id=self.catalog_query_a, force=True, dry_run=False),
            mock_catalog_task.s(catalog_query_id=self.catalog_query_b, force=True, dry_run=False),
        ])
        mock_full_metadata_task.apply.assert_called_once_with(kwargs={"force": True, "dry_run": False})

    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.update_content_metadata.fetch_missing_course_metadata_task')
    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.update_content_metadata.fetch_missing_pathway_metadata_task')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.group')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.update_catalog_metadata_task')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.update_full_content_metadata_task')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.dispatch_algolia_indexing')
    def test_dispatch_algolia_indexing_async(
        self, mock_dispatch, mock_full_metadata_task, mock_catalog_task, mock_group,
        mock_fetch_missing_pathway, mock_fetch_missing_course
    ):
        """
        Verify that dispatch_algolia_indexing is called with force=False (async)
        """
        call_command(self.command_name)
        mock_dispatch.si.assert_called_once_with(force=False, dry_run=False, use_apply=False)

    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.update_content_metadata.fetch_missing_course_metadata_task')
    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.update_content_metadata.fetch_missing_pathway_metadata_task')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.group')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.update_catalog_metadata_task')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.update_full_content_metadata_task')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.dispatch_algolia_indexing')
    def test_dispatch_algolia_indexing_no_async(
        self, mock_dispatch, mock_full_metadata_task, mock_catalog_task, mock_group,
        mock_fetch_missing_pathway, mock_fetch_missing_course
    ):
        """
        Verify that dispatch_algolia_indexing is called with use_apply=True (no-async)
        """
        call_command(self.command_name, no_async=True)
        mock_dispatch.apply.assert_called_once_with(kwargs={'force': False, 'dry_run': False, 'use_apply': True})

    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.update_content_metadata.fetch_missing_course_metadata_task')
    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.update_content_metadata.fetch_missing_pathway_metadata_task')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.group')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.update_catalog_metadata_task')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.update_full_content_metadata_task')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.dispatch_algolia_indexing')
    def test_incremental_indexing_recently_run_error_swallowed(
        self, mock_dispatch, mock_full_metadata_task, mock_catalog_task, mock_group,
        mock_fetch_missing_pathway, mock_fetch_missing_course
    ):
        """
        Verify TaskRecentlyRunError from dispatch_algolia_indexing is swallowed (dedup guard).
        """
        mock_dispatch.si.return_value.apply_async.return_value.get.side_effect = TaskRecentlyRunError('dedup')
        call_command(self.command_name)  # must not raise

    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.update_content_metadata.fetch_missing_course_metadata_task')
    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.update_content_metadata.fetch_missing_pathway_metadata_task')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.group')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.update_catalog_metadata_task')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.update_full_content_metadata_task')
    @mock.patch('enterprise_catalog.apps.catalog.management.commands.update_content_metadata.dispatch_algolia_indexing')
    def test_incremental_indexing_other_exception_propagates(
        self, mock_dispatch, mock_full_metadata_task, mock_catalog_task, mock_group,
        mock_fetch_missing_pathway, mock_fetch_missing_course
    ):
        """
        Verify non-TaskRecentlyRunError exceptions from dispatch_algolia_indexing are re-raised.
        """
        mock_dispatch.si.return_value.apply_async.return_value.get.side_effect = RuntimeError('boom')
        with self.assertRaises(RuntimeError):
            call_command(self.command_name)
