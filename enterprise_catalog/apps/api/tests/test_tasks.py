"""
Tests for the enterprise_catalog API celery tasks
"""

import json
import uuid
from datetime import timedelta
from unittest import mock

import ddt
from celery import states
from django.test import TestCase
from django_celery_results.models import TaskResult

from enterprise_catalog.apps.api import tasks
from enterprise_catalog.apps.api.constants import CourseMode
from enterprise_catalog.apps.api_client.discovery import CatalogQueryMetadata
from enterprise_catalog.apps.catalog.constants import (
    COURSE,
    COURSE_RUN,
    EXEC_ED_2U_COURSE_TYPE,
    LEARNER_PATHWAY,
    PROGRAM,
)
from enterprise_catalog.apps.catalog.models import CatalogQuery, ContentMetadata
from enterprise_catalog.apps.catalog.serializers import (
    DEFAULT_NORMALIZED_PRICE,
    _find_best_mode_seat,
)
from enterprise_catalog.apps.catalog.tests.factories import (
    CatalogQueryFactory,
    ContentMetadataFactory,
    EnterpriseCatalogFactory,
)
from enterprise_catalog.apps.catalog.utils import localized_utcnow


# An object that represents the output of some hard work done by a task.
COMPUTED_PRECIOUS_OBJECT = object()


@tasks.expiring_task_semaphore()
def mock_task(self, *args, **kwargs):  # pylint: disable=unused-argument
    """
    A mock task that is constrained by our expiring semaphore mechanism.
    """
    return COMPUTED_PRECIOUS_OBJECT


# An actual celery task would have a name attribute, and we use
# it in a few places, so we patch it in here.
mock_task.name = 'mock_task'


@ddt.ddt
class TestTaskResultFunctions(TestCase):
    """
    Tests for functions in tasks.py that rely upon `django-celery_results.models.TaskResult`.
    """

    def setUp(self):
        """
        Delete all TaskResult objects, make a new single result object.
        """
        super().setUp()
        TaskResult.objects.all().delete()

        self.test_args = (123, 77)
        self.test_kwargs = {'foo': 'bar'}

        self.mock_task_id = uuid.uuid4()
        self.other_task_id = uuid.uuid4()

        self.mock_task_result = TaskResult.objects.create(
            task_name=mock_task.name,
            task_args=json.dumps(repr(self.test_args)),
            task_kwargs=json.dumps(repr(self.test_kwargs)),
            status=states.SUCCESS,
            # Default to a state where the only recorded task result is for some "other" task
            task_id=self.other_task_id,
        )

    def mock_task_instance(self, *args, **kwargs):
        """
        Helper method that creates a "bound task object", which is a stand-in
        for what `self` would be in the body of a celery task that has `bind=True` specified.
        Invokes our `mock_task` with that bound object and the given args and kwargs.
        """
        bound_task_object = mock.MagicMock()
        bound_task_object.name = mock_task.name
        bound_task_object.request.id = self.mock_task_id
        bound_task_object.request.args = args
        bound_task_object.request.kwargs = kwargs
        return mock_task(bound_task_object, *args, **kwargs)

    def test_semaphore_raises_recent_run_error_for_same_args(self):
        self.mock_task_result.task_kwargs = json.dumps(repr({}))
        self.mock_task_result.save()

        with self.assertRaises(tasks.TaskRecentlyRunError):
            self.mock_task_instance(*self.test_args)

    def test_semaphore_raises_recent_run_error_for_same_kwargs(self):
        self.mock_task_result.task_args = json.dumps(repr(()))
        self.mock_task_result.save()

        with self.assertRaises(tasks.TaskRecentlyRunError):
            self.mock_task_instance(**self.test_kwargs)

    def test_task_with_result_older_than_an_hour_ignored_by_semaphore(self):
        self.mock_task_result.date_created = localized_utcnow() - timedelta(hours=4)
        self.mock_task_result.save()

        result = self.mock_task_instance(*self.test_args, **self.test_kwargs)
        assert COMPUTED_PRECIOUS_OBJECT == result

    @ddt.data(states.FAILURE, states.REVOKED)
    def test_failed_or_revoked_tasks_are_ignored_by_semaphore(self, task_state):
        self.mock_task_result.status = task_state
        self.mock_task_result.date_created = localized_utcnow() - timedelta(minutes=1)
        self.mock_task_result.save()

        result = self.mock_task_instance(*self.test_args)
        assert result == COMPUTED_PRECIOUS_OBJECT

    def test_given_task_id_is_ignored_by_semaphore(self):
        # Make our only TaskResult for a task with the same id
        # as the mock task - set status and date such that the
        # result would count as a recent equivalent task if it did _not_
        # have the same task_id as the mock task that is "running".
        self.mock_task_result.status = states.PENDING
        self.mock_task_result.date_created = localized_utcnow() - timedelta(minutes=1)
        self.mock_task_result.task_id = self.mock_task_id
        self.mock_task_result.save()

        result = self.mock_task_instance(*self.test_args, **self.test_kwargs)
        assert COMPUTED_PRECIOUS_OBJECT == result

    @ddt.data(*states.UNREADY_STATES)
    def test_unready_tasks_exist_for_unready_states(self, task_state):
        self.mock_task_result.status = task_state
        self.mock_task_result.save()

        self.assertTrue(
            tasks.unready_tasks(
                mock_task, timedelta(hours=2)
            ).exists()
        )

    @ddt.data(*states.READY_STATES)
    def test_unready_tasks_dont_exist_for_ready_states(self, task_state):
        self.mock_task_result.status = task_state
        self.mock_task_result.save()

        self.assertFalse(
            tasks.unready_tasks(
                mock_task, timedelta(hours=2)
            ).exists()
        )

    def test_unready_tasks_dont_exist_for_more_recent_delta(self):
        self.mock_task_result.status = states.PENDING
        self.mock_task_result.date_created = localized_utcnow() - timedelta(hours=1)
        self.mock_task_result.save()

        self.assertFalse(
            tasks.unready_tasks(
                mock_task, timedelta(minutes=30)
            ).exists()
        )

    def test_unready_tasks_dont_exist_for_different_task_name(self):
        other_mock_task = mock.MagicMock()
        other_mock_task.name = 'other_task_name'

        self.assertFalse(
            tasks.unready_tasks(
                other_mock_task, timedelta(hours=24)
            ).exists()
        )


class UpdateCatalogMetadataTaskTests(TestCase):
    """
    Tests for the `update_catalog_metadata_task`.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.catalog_query = CatalogQueryFactory()

    @mock.patch('enterprise_catalog.apps.api.tasks.update_contentmetadata_from_discovery')
    def test_update_catalog_metadata(self, mock_update_data_from_discovery):
        """
        Assert update_catalog_metadata_task is called with correct catalog_query_id
        """
        tasks.update_catalog_metadata_task.apply(args=(self.catalog_query.id, False, False))
        mock_update_data_from_discovery.assert_called_with(self.catalog_query, False)

    @mock.patch('enterprise_catalog.apps.api.tasks.update_contentmetadata_from_discovery')
    def test_update_catalog_metadata_no_catalog_query(self, mock_update_data_from_discovery):
        """
        Assert that discovery is not called if a bad catalog query id is passed
        """
        bad_id = 412
        tasks.update_catalog_metadata_task.apply(args=(bad_id,))
        mock_update_data_from_discovery.assert_not_called()


class FetchMissingCourseMetadataTaskTests(TestCase):
    """
    Tests for the `fetch_missing_course_metadata_task`.
    """
    @mock.patch('enterprise_catalog.apps.api.tasks.update_contentmetadata_from_discovery')
    def test_fetch_missing_course_metadata_task(self, mock_update_data_from_discovery):
        """
        Validate the fetch_missing_course_metadata_task gathers correct data of missing courses and calls
        update_contentmetadata_from_discovery with correct arguments.
        """
        test_course = 'course:edX+testX'
        course_content_metadata = ContentMetadataFactory.create(content_type=COURSE)
        ContentMetadataFactory.create(content_type=PROGRAM, _json_metadata={
            'courses': [
                course_content_metadata.json_metadata,
                {
                    'key': test_course,
                },
            ]
        })

        tasks.fetch_missing_course_metadata_task.apply()

        assert CatalogQuery.objects.filter().count() == 1
        catalog_query = CatalogQuery.objects.first()
        assert catalog_query.content_filter['status'] == 'published'
        assert catalog_query.content_filter['content_type'] == 'course'
        assert catalog_query.content_filter['key'] == [test_course]

        mock_update_data_from_discovery.assert_called_with(catalog_query, False)


@ddt.ddt
class FetchMissingPathwayMetadataTaskTests(TestCase):
    """
    Tests for the `fetch_missing_pathway_metadata_task`.
    """
    @ddt.data(True, False)
    @mock.patch.object(CatalogQueryMetadata, '_get_catalog_query_metadata')
    def test_fetch_missing_pathway_metadata_task(self, visible_via_association, mock_get_catalog_query_metadata):
        """
        Validate the fetch_missing_pathway_metadata_task creates correct Data and its associations.

        1. Validate it creates all the Learner Pathways
        2. Validate it creates missing course and programs associated with Pathways
        3. Validates correct association has been build between pathways ContentMetadata and its associated Course and
        Program ContentMetadata
        """
        test_pathway = 'e246705d-9044-4bc9-8c8d-ebb0c3d0a9ad'
        test_course = 'edX+DemoX'
        test_program = 'dcc9d1cf-a068-48c4-841d-934a0fcd2bfb'

        assert ContentMetadata.objects.count() == 0

        all_pathways_discovery_result = [
            {
                "aggregation_key": f"learnerpathway:{test_pathway}",
                "content_type": "learnerpathway",
                "uuid": test_pathway,
                "name": "Full stack developer",
                "visible_via_association": visible_via_association,
                "status": "active",
                "steps": [
                    {
                        "uuid": "63d708a7-8512-427e-8ae1-6ee8fa685360",
                        "min_requirement": 1,
                        "courses": [],
                        "programs": [
                            {
                                "uuid": test_program,
                                "title": "edX Demonstration Program",
                                "content_type": "program"
                            }
                        ]
                    },
                    {
                        "uuid": "4a169c83-46f6-4a5a-8e58-5ccb76518f3d",
                        "min_requirement": 1,
                        "courses": [
                            {
                                "key": test_course,
                                "title": "Demonstration Course",
                                "content_type": "course"
                            }
                        ],
                        "programs": []
                    }
                ]
            }
        ]
        missing_programs_discovery_result = [
            {
                "aggregation_key": f"program:{test_program}",
                "uuid": test_program,
                "title": "edX Demonstration Program",
                "content_type": "program"
            }
        ]
        missing_courses_discovery_result = [
            {
                "aggregation_key": f"course:{test_course}",
                "key": test_course,
                "title": "Demonstration Course",
                "content_type": "course"
            }
        ]
        mock_get_catalog_query_metadata.side_effect = [
            all_pathways_discovery_result,
            missing_programs_discovery_result,
            missing_courses_discovery_result,
        ]

        tasks.fetch_missing_pathway_metadata_task.apply()

        assert ContentMetadata.objects.count() == 3
        learner_pathway = ContentMetadata.objects.get(content_key=test_pathway)
        program = ContentMetadata.objects.get(content_key=test_program)
        course = ContentMetadata.objects.get(content_key=test_course)
        associated_content_metadata = learner_pathway.associated_content_metadata.all()
        if visible_via_association:
            assert list(associated_content_metadata) == [program, course]
        else:
            assert not associated_content_metadata

        queries = CatalogQuery.objects.all()
        assert queries.count() == 3
        pathways_query = queries[0]
        assert pathways_query.content_filter['content_type'] == LEARNER_PATHWAY

        program_catalog_query = queries[1]
        assert program_catalog_query.content_filter['status'] == 'published'
        assert program_catalog_query.content_filter['content_type'] == 'program'
        assert program_catalog_query.content_filter['key'] == [test_program]

        course_catalog_query = queries[2]
        assert course_catalog_query.content_filter['status'] == 'published'
        assert course_catalog_query.content_filter['content_type'] == 'course'
        assert course_catalog_query.content_filter['key'] == [test_course]


@ddt.ddt
class UpdateFullContentMetadataTaskTests(TestCase):
    """
    Tests for the `update_full_content_metadata_task`.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.enterprise_catalog = EnterpriseCatalogFactory()
        cls.catalog_query = cls.enterprise_catalog.catalog_query

    @ddt.data(
        # Test that it doesn't crash on empty input.
        {
            'seats': [],
            'expected_seat': None,
        },
        # Test that the best seat type is selected (verified > professional).
        {
            'seats': [
                {'type': CourseMode.PROFESSIONAL, 'sku': 'SKU-1'},
                {'type': CourseMode.VERIFIED, 'sku': 'SKU-2'},
            ],
            'expected_seat': {'type': CourseMode.VERIFIED, 'sku': 'SKU-2'},
        },
        # Test that even if one non-"best" seat type is present, the best one is still selected.
        {
            'seats': [
                {'type': CourseMode.PAID_EXECUTIVE_EDUCATION, 'sku': 'SKU-1'},
                {'type': CourseMode.PROFESSIONAL, 'sku': 'SKU-2'},
                {'type': CourseMode.VERIFIED, 'sku': 'SKU-3'},
            ],
            'expected_seat': {'type': CourseMode.VERIFIED, 'sku': 'SKU-3'},
        },
        # Test that even if no "best" seat types are present, one is still selected.
        {
            'seats': [
                {'type': CourseMode.PAID_EXECUTIVE_EDUCATION, 'sku': 'SKU-1'},
            ],
            'expected_seat': {'type': CourseMode.PAID_EXECUTIVE_EDUCATION, 'sku': 'SKU-1'},
        },
    )
    @ddt.unpack
    def test_find_best_mode_seat(self, seats, expected_seat):
        """
        Test the behavior of _find_best_mode_seat().
        """
        assert _find_best_mode_seat(seats) == expected_seat

    # pylint: disable=unused-argument, too-many-statements
    @mock.patch('enterprise_catalog.apps.api.tasks.task_recently_run', return_value=False)
    @mock.patch('enterprise_catalog.apps.api.tasks.partition_course_keys_for_indexing')
    @mock.patch('enterprise_catalog.apps.api_client.base_oauth.OAuthAPIClient')
    def test_update_full_metadata(self, mock_oauth_client, mock_partition_course_keys, mock_task_recently_run):
        """
        Assert that full course metadata is merged with original json_metadata for all ContentMetadata records.
        """
        program_key = '02f5edeb-6604-4131-bf45-acd8df91e1f9'
        program_data = {'uuid': program_key, 'full_program_only_field': 'test_1'}
        course_key_1 = 'edX+fakeX'
        course_data_1 = {'key': course_key_1, 'full_course_only_field': 'test_1', 'programs': []}
        course_key_2 = 'edX+testX'
        course_run_2_uuid = str(uuid.uuid4())
        course_data_2 = {
            'key': course_key_2,
            'full_course_only_field': 'test_2',
            'programs': [program_data],
            'advertised_course_run_uuid': course_run_2_uuid,
            'course_runs': [{
                'uuid': course_run_2_uuid,
                'key': f'course-v1:{course_key_2}+1',
                'start': '2023-03-01T00:00:00Z',
                'end': '2023-03-01T00:00:00Z',
                'first_enrollable_paid_seat_price': None,  # should cause fallback to DEFAULT_NORMALIZED_PRICE
                'seats': [
                    {
                        'type': CourseMode.VERIFIED,
                        'upgrade_deadline': '2023-03-15T00:00:00Z',
                    },
                ],
                'enrollment_start': '2023-02-01T00:00:00Z',
            }],
        }

        course_key_3 = 'edX+fooX'
        course_run_3_uuid = str(uuid.uuid4())
        course_data_3 = {
            'key': course_key_3,
            'programs': [],
            'course_runs': [{
                'key': f'course-v1:{course_key_3}+1',
                'uuid': course_run_3_uuid,
                # The task should copy these dates into net-new top level fields.
                'start': '2023-03-01T00:00:00Z',
                'end': '2023-03-01T00:00:00Z',
                'first_enrollable_paid_seat_price': 90,
                'seats': [
                    {
                        'type': CourseMode.VERIFIED,
                        'upgrade_deadline': '2023-02-01T00:00:00Z',
                    },
                    {
                        "type": str(CourseMode.PROFESSIONAL),
                        "upgrade_deadline": '2022-02-01T00:00:00Z',
                    },
                ],
                'enrollment_start': '2023-02-01T00:00:00Z',
            }],
            'advertised_course_run_uuid': course_run_3_uuid,
        }
        course_key_4 = 'edX+superDuperFakeX'
        course_data_4 = {'key': course_key_4, 'full_course_only_field': 'test_4', 'programs': [], 'course_runs': []}

        non_course_key = 'course-runX'

        mock_oauth_client.return_value.get.return_value.status_code = 200

        # Mock out the data that should be returned from discovery's /api/v1/courses and /api/v1/programs endpoints
        mock_oauth_client.return_value.get.return_value.json.side_effect = [
            # first call will be /api/v1/courses
            {'results': [course_data_1, course_data_2, course_data_3, course_data_4]},
            # second call will be to /api/v1/programs
            {'results': [program_data]},
            {'results': []},
        ]
        mock_partition_course_keys.return_value = ([], [],)

        metadata_1 = ContentMetadataFactory(content_type=COURSE, content_key=course_key_1)
        metadata_1.catalog_queries.set([self.catalog_query])
        metadata_2 = ContentMetadataFactory(content_type=COURSE, content_key=course_key_2)
        metadata_2.catalog_queries.set([self.catalog_query])
        metadata_3 = ContentMetadataFactory(content_type=COURSE, content_key=course_key_3)
        metadata_3.catalog_queries.set([self.catalog_query])
        # Create a metadata record without an advertised course run to test
        # the normalized metadata serializer.
        metadata_4 = ContentMetadataFactory(content_type=COURSE, content_key=course_key_4)
        metadata_4.catalog_queries.set([self.catalog_query])
        metadata_4._json_metadata['advertised_course_run_uuid'] = None  # pylint: disable=protected-access
        metadata_4.save()
        non_course_metadata = ContentMetadataFactory(content_type=COURSE_RUN, content_key=non_course_key)
        non_course_metadata.catalog_queries.set([self.catalog_query])

        assert metadata_1.json_metadata != course_data_1
        assert metadata_2.json_metadata != course_data_2
        assert metadata_3.json_metadata != course_data_3
        assert metadata_4.json_metadata != course_data_4

        tasks.update_full_content_metadata_task.apply().get()

        actual_course_keys_args = mock_partition_course_keys.call_args_list[0][0][0]
        self.assertEqual(set(actual_course_keys_args), {metadata_1, metadata_2, metadata_3, metadata_4})

        metadata_1 = ContentMetadata.objects.get(content_key=course_key_1)
        metadata_2 = ContentMetadata.objects.get(content_key=course_key_2)
        metadata_3 = ContentMetadata.objects.get(content_key=course_key_3)
        metadata_4 = ContentMetadata.objects.get(content_key=course_key_4)

        assert metadata_1.json_metadata['aggregation_key'] == f'course:{course_key_1}'
        assert metadata_1.json_metadata['full_course_only_field'] == 'test_1'
        assert metadata_1.json_metadata['programs'] == []

        assert metadata_2.json_metadata['aggregation_key'] == f'course:{course_key_2}'
        assert metadata_2.json_metadata['full_course_only_field'] == 'test_2'
        assert metadata_2.json_metadata['normalized_metadata']['content_price'] == DEFAULT_NORMALIZED_PRICE
        assert metadata_2.json_metadata['normalized_metadata']['start_date'] == '2023-03-01T00:00:00Z'
        assert metadata_2.json_metadata['normalized_metadata']['end_date'] == '2023-03-01T00:00:00Z'
        assert metadata_2.json_metadata['normalized_metadata']['enroll_by_date'] == '2023-03-15T00:00:00Z'
        assert metadata_2.json_metadata['normalized_metadata']['enroll_start_date'] == '2023-02-01T00:00:00Z'
        expected_normalized_metadata_by_run = {
            course_run['key']: {
                'start_date': course_run['start'],
                'end_date': course_run['end'],
                'enroll_by_date': course_run['seats'][0]['upgrade_deadline'],
                'enroll_start_date': course_run['enrollment_start'],
                'content_price': course_run['first_enrollable_paid_seat_price'] or DEFAULT_NORMALIZED_PRICE,
            }
            for course_run in metadata_2.json_metadata['course_runs']
        }
        assert metadata_2.json_metadata['normalized_metadata_by_run'] == expected_normalized_metadata_by_run
        assert set(program_data.items()).issubset(set(metadata_2.json_metadata['programs'][0].items()))

        assert metadata_3.json_metadata['aggregation_key'] == f'course:{course_key_3}'
        assert metadata_3.json_metadata['normalized_metadata']['start_date'] == '2023-03-01T00:00:00Z'
        assert metadata_3.json_metadata['normalized_metadata']['end_date'] == '2023-03-01T00:00:00Z'
        assert metadata_3.json_metadata['normalized_metadata']['enroll_by_date'] == '2023-02-01T00:00:00Z'
        assert metadata_3.json_metadata['normalized_metadata']['content_price'] == 90
        expected_normalized_metadata_by_run = {
            course_run['key']: {
                'start_date': course_run['start'],
                'end_date': course_run['end'],
                'enroll_by_date': course_run['seats'][0]['upgrade_deadline'],
                'enroll_start_date': course_run['enrollment_start'],
                'content_price': course_run['first_enrollable_paid_seat_price'] or DEFAULT_NORMALIZED_PRICE,
            }
            for course_run in metadata_3.json_metadata['course_runs']
        }
        assert metadata_3.json_metadata['normalized_metadata_by_run'] == expected_normalized_metadata_by_run

        assert metadata_4.json_metadata['aggregation_key'] == f'course:{course_key_4}'
        assert metadata_4.json_metadata['full_course_only_field'] == 'test_4'
        assert metadata_4.json_metadata['programs'] == []
        assert metadata_4.json_metadata['normalized_metadata'] == {
            'start_date': None,
            'end_date': None,
            'enroll_by_date': None,
            'enroll_start_date': None,
            'content_price': DEFAULT_NORMALIZED_PRICE,
        }

        # make sure course associated program metadata has been created and linked correctly
        assert ContentMetadata.objects.filter(content_key=program_key).exists()
        assert metadata_2.associated_content_metadata.filter(content_key=program_key).exists()
        assert not metadata_1.associated_content_metadata.filter(content_key=program_key).exists()

    # pylint: disable=unused-argument
    @mock.patch('enterprise_catalog.apps.api.tasks.task_recently_run', return_value=False)
    @mock.patch('enterprise_catalog.apps.api.tasks.partition_program_keys_for_indexing')
    @mock.patch('enterprise_catalog.apps.api_client.base_oauth.OAuthAPIClient')
    def test_update_full_metadata_program(self, mock_oauth_client, mock_partition_program_keys, mock_task_recently_run):
        """
        Assert that full program metadata is merged with original json_metadata for all ContentMetadata records.
        """
        program_key_1 = '02f5edeb-6604-4131-bf45-acd8df91e1f9'
        program_data_1 = {'uuid': program_key_1, 'full_program_only_field': 'test_1'}
        program_key_2 = 'be810df3-a059-42a7-b11f-d9bfb2877b15'
        program_data_2 = {'uuid': program_key_2, 'full_program_only_field': 'test_2'}

        # Mock out the data that should be returned from discovery's /api/v1/programs endpoint
        mock_oauth_client.return_value.get.return_value.json.return_value = {
            'results': [program_data_1, program_data_2],
        }
        mock_partition_program_keys.return_value = ([], [],)

        metadata_1 = ContentMetadataFactory(content_type=PROGRAM, content_key=program_key_1)
        metadata_1.catalog_queries.set([self.catalog_query])
        metadata_2 = ContentMetadataFactory(content_type=PROGRAM, content_key=program_key_2)
        metadata_2.catalog_queries.set([self.catalog_query])

        assert metadata_1.json_metadata != program_data_1
        assert metadata_2.json_metadata != program_data_2

        tasks.update_full_content_metadata_task.apply().get()

        actual_program_keys_args = mock_partition_program_keys.call_args_list[0][0][0]
        self.assertEqual(set(actual_program_keys_args), {metadata_1, metadata_2})

        metadata_1 = ContentMetadata.objects.get(content_key='02f5edeb-6604-4131-bf45-acd8df91e1f9')
        metadata_2 = ContentMetadata.objects.get(content_key='be810df3-a059-42a7-b11f-d9bfb2877b15')

        # add aggregation_key and uuid to program objects since they should now exist
        # after merging the original json_metadata with the course metadata
        program_data_1.update(metadata_1.json_metadata)
        program_data_2.update(metadata_2.json_metadata)
        program_data_1.update({'aggregation_key': 'program:02f5edeb-6604-4131-bf45-acd8df91e1f9'})
        program_data_2.update({'aggregation_key': 'program:be810df3-a059-42a7-b11f-d9bfb2877b15'})

        assert metadata_1.json_metadata == program_data_1
        assert metadata_2.json_metadata == program_data_2

    @mock.patch('enterprise_catalog.apps.api.tasks.partition_program_keys_for_indexing')
    @mock.patch('enterprise_catalog.apps.api_client.base_oauth.OAuthAPIClient')
    def test_update_full_metadata_program_dry_run(self, mock_oauth_client, mock_partition_program_keys):
        """
        Assert that during dry run full program metadata is not merged with original json_metadata
        """
        program_key_1 = '02f5edeb-6604-4131-bf45-acd8df91e1f9'
        program_data_1 = {'uuid': program_key_1, 'full_program_only_field': 'test_1'}
        program_key_2 = 'be810df3-a059-42a7-b11f-d9bfb2877b15'
        program_data_2 = {'uuid': program_key_2, 'full_program_only_field': 'test_2'}

        # Mock out the data that should be returned from discovery's /api/v1/programs endpoint
        mock_oauth_client.return_value.get.return_value.json.return_value = {
            'results': [program_data_1, program_data_2],
        }
        mock_partition_program_keys.return_value = ([], [],)

        metadata_1 = ContentMetadataFactory(content_type=PROGRAM, content_key=program_key_1)
        metadata_1.catalog_queries.set([self.catalog_query])
        metadata_2 = ContentMetadataFactory(content_type=PROGRAM, content_key=program_key_2)
        metadata_2.catalog_queries.set([self.catalog_query])

        assert metadata_1.json_metadata != program_data_1
        assert metadata_2.json_metadata != program_data_2

        tasks.update_full_content_metadata_task.apply(kwargs={'dry_run': True}).get()

        actual_program_keys_args = mock_partition_program_keys.call_args_list[0][0][0]
        self.assertEqual(set(actual_program_keys_args), {metadata_1, metadata_2})

        metadata_1 = ContentMetadata.objects.get(content_key='02f5edeb-6604-4131-bf45-acd8df91e1f9')
        metadata_2 = ContentMetadata.objects.get(content_key='be810df3-a059-42a7-b11f-d9bfb2877b15')

        # Validate original json_metadata still in place after dry run
        program_data_1.update(metadata_1.json_metadata)
        program_data_2.update(metadata_2.json_metadata)
        program_data_1.update({'aggregation_key': 'program:02f5edeb-6604-4131-bf45-acd8df91e1f9'})
        program_data_2.update({'aggregation_key': 'program:be810df3-a059-42a7-b11f-d9bfb2877b15'})

        assert metadata_1.json_metadata != program_data_1
        assert metadata_2.json_metadata != program_data_2

    # pylint: disable=unused-argument
    @mock.patch('enterprise_catalog.apps.api.tasks.task_recently_run', return_value=False)
    @mock.patch('enterprise_catalog.apps.api.tasks.partition_program_keys_for_indexing')
    @mock.patch('enterprise_catalog.apps.api_client.base_oauth.OAuthAPIClient')
    def test_update_full_metadata_exec_ed(self, mock_oauth_client, mock_partition_course_keys, mock_task_recently_run):
        """
        Assert that all the fields are correctly updated in ContentMetadata records that represent Exec Ed courses.

        Check both things:
        * Make sure the field normalization step caused the creation of expected net-new fields.
        * Make sure the start/end dates are copied from the additional_metadata into the course run dict of the course.
        """
        course_key = 'edX+testX'
        course_run_key = 'course-v1:edX+testX+1'
        course_run_uuid = str(uuid.uuid4())

        # Simulate a course data in the response from /api/v1/courses/
        course_data = {
            'aggregation_key': f'course:{course_key}',
            'key': course_key,
            'course_type': EXEC_ED_2U_COURSE_TYPE,
            'course_runs': [{
                'key': course_run_key,
                'uuid': course_run_uuid,
                # start/end dates are the same across course types
                'start': '2023-03-01T00:00:00Z',
                'end': '2023-04-09T23:59:59Z',
                # `enrollment_end` in place of seat.upgrade_deadline for Exec Ed courses
                'enrollment_end': '2023-02-01T00:00:00Z',
            }],
            'programs': [],
            'entitlements': [
                {
                    'price': 2900,
                    'mode': 'paid-executive-education',
                },
            ],
            'advertised_course_run_uuid': course_run_uuid,

            # Intentionally exclude net-new fields that we will assert are added by the
            # update_full_content_metadata_task.
            #
            # 'normalized_metadata': {
            #     'start_date': '2023-03-01T00:00:00Z',
            #     'end_date': '2023-04-09T23:59:59Z'
            #     'enroll_by_date': '2023-02-01T00:00:00Z',
            #     'content_price': 2900,
            # }
        }

        mock_oauth_client.return_value.get.return_value.status_code = 200

        # Mock out the data that should be returned from discovery's /api/v1/courses endpoint
        mock_oauth_client.return_value.get.return_value.json.side_effect = [
            {'results': [course_data]},
            {'results': []},
        ]
        mock_partition_course_keys.return_value = ([], [],)

        # Simulate a pre-existing ContentMetadata object freshly seeded using the response from /api/v1/search/all/
        course_metadata = ContentMetadataFactory.create(
            content_type=COURSE, content_key=course_key, _json_metadata={
                'aggregation_key': 'course:edX+testX',
                'key': 'edX+testX',
                'course_type': EXEC_ED_2U_COURSE_TYPE,
                'course_runs': [{
                    'key': course_run_key,
                    # start/end dates are the same across course types
                    'start': '2023-03-01T00:00:00Z',
                    'end': '2023-04-09T23:59:59Z',
                    # `enrollment_end` in place of seat.upgrade_deadline for Exec Ed courses
                    'enrollment_end': '2023-02-01T00:00:00Z',
                }],
                'programs': [],

                # `advertised_course_run_uuid` is ONLY in the output of /api/v1/courses/, not /api/v1/search/all/
                # 'advertised_course_run_uuid': course_run_uuid,

                # Intentionally exclude net-new fields that we will assert are added by the
                # update_full_content_metadata_task.
                #
                # 'normalized_metadata': {
                #     'start_date': '2023-03-01T00:00:00Z',
                #     'end_date': '2023-04-09T23:59:59Z'
                #     'enroll_by_date': '2023-02-01T00:00:00Z',
                #     'content_price': 2900,
                # }
            }
        )

        course_metadata.catalog_queries.set([self.catalog_query])

        tasks.update_full_content_metadata_task.apply().get()

        assert ContentMetadata.objects.count() == 1

        # Make sure the field normalization step caused the creation of expected net-new fields.
        course_cm = ContentMetadata.objects.get(content_key=course_key)
        assert course_cm.content_type == COURSE

        assert course_cm.json_metadata['normalized_metadata']['start_date'] == '2023-03-01T00:00:00Z'
        assert course_cm.json_metadata['normalized_metadata']['end_date'] == '2023-04-09T23:59:59Z'
        assert course_cm.json_metadata['normalized_metadata']['enroll_by_date'] == '2023-02-01T00:00:00Z'
        assert course_cm.json_metadata['normalized_metadata']['content_price'] == 2900

        normalized_metadata_by_run = course_cm.json_metadata['normalized_metadata_by_run']
        assert normalized_metadata_by_run[course_run_key]['start_date'] == '2023-03-01T00:00:00Z'
        assert normalized_metadata_by_run[course_run_key]['end_date'] == '2023-04-09T23:59:59Z'
        assert normalized_metadata_by_run[course_run_key]['enroll_by_date'] == '2023-02-01T00:00:00Z'
        assert normalized_metadata_by_run[course_run_key]['content_price'] == 2900

        # Make sure the start/end dates are copied from the additional_metadata into the course run dict of the course.
        # This checks that the dummy 2022 dates are overwritten.
        course_run_json = course_cm.json_metadata.get('course_runs')[0]
        assert course_run_json['uuid'] == course_run_uuid
        assert course_run_json['start'] == '2023-03-01T00:00:00Z'
        assert course_run_json['end'] == '2023-04-09T23:59:59Z'
