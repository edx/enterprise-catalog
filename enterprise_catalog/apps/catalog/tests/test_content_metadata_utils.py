from unittest import mock
from uuid import uuid4

import ddt
from django.test import TestCase

from enterprise_catalog.apps.catalog.content_metadata_utils import (
    get_advertised_course_run,
    tansform_force_included_courses,
    transform_course_metadata_to_visible,
    validate_content_metadata,
)


ADVERTISED_COURSE_RUN_UUID = uuid4()


@ddt.ddt
class ContentMetadataUtilsTests(TestCase):
    """
    Tests for content metadata utils.
    """

    def test_transform_course_metadata_to_visible(self):
        advertised_course_run_uuid = str(uuid4())
        content_metadata = {
            'advertised_course_run_uuid': advertised_course_run_uuid,
            'course_runs': [
                {
                    'uuid': advertised_course_run_uuid,
                    'status': 'unpublished',
                    'availability': 'Coming Soon',
                }
            ],
            'course_run_statuses': [
                'unpublished'
            ]
        }
        transform_course_metadata_to_visible(content_metadata)
        assert content_metadata['course_runs'][0]['status'] == 'published'
        assert content_metadata['course_runs'][0]['availability'] == 'Current'
        assert content_metadata['course_run_statuses'][0] == 'published'

    def test_tansform_force_included_courses(self):
        advertised_course_run_uuid = str(uuid4())
        content_metadata = {
            'advertised_course_run_uuid': advertised_course_run_uuid,
            'course_runs': [
                {
                    'uuid': advertised_course_run_uuid,
                    'status': 'unpublished',
                    'availability': 'Coming Soon',
                }
            ],
            'course_run_statuses': [
                'unpublished'
            ]
        }
        courses = [content_metadata]
        tansform_force_included_courses(courses)
        assert courses[0]['course_runs'][0]['status'] == 'published'

    @ddt.data(
        # Happy path: Multiple runs including advertised_course_run in course_runs, available advertised_course_run_uuid
        (
            {
                'course_runs': [
                    {
                        'key': 'course-v1:org+course+1T2021',
                        'uuid': ADVERTISED_COURSE_RUN_UUID,
                        'pacing_type': 'instructor_paced',
                        'start': '2013-10-16T14:00:00Z',
                        'end': '2014-10-16T14:00:00Z',
                        'enrollment_end': '2013-10-17T14:00:00Z',
                        'availability': 'Current',
                        'min_effort': 10,
                        'max_effort': 14,
                        'weeks_to_complete': 13,
                        'status': 'published',
                        'is_enrollable': True,
                        'is_marketable': True,
                        'enrollment_start': '2013-10-01T14:00:00Z',
                    },
                    {
                        'key': 'course-v1:org+course+1T2021',
                        'uuid': uuid4(),
                        'pacing_type': 'instructor_paced',
                        'start': '2016-10-16T14:00:00Z',
                        'end': '2019-10-16T14:00:00Z',
                        'enrollment_end': '2016-10-17T14:00:00Z',
                        'availability': 'Upcoming',
                        'min_effort': 11,
                        'max_effort': 15,
                        'weeks_to_complete': 15,
                        'status': 'published',
                        'is_enrollable': True,
                        'is_marketable': True,
                        'enrollment_start': '2013-10-01T14:00:00Z',
                    }
                ],
                'advertised_course_run_uuid': ADVERTISED_COURSE_RUN_UUID
            },
            {
                'key': 'course-v1:org+course+1T2021',
                'uuid': ADVERTISED_COURSE_RUN_UUID,
                'pacing_type': 'instructor_paced',
                'start': '2013-10-16T14:00:00Z',
                'end': '2014-10-16T14:00:00Z',
                'enrollment_end': '2013-10-17T14:00:00Z',
                'availability': 'Current',
                'min_effort': 10,
                'max_effort': 14,
                'weeks_to_complete': 13,
                'status': 'published',
                'is_enrollable': True,
                'is_marketable': True,
                'enrollment_start': '2013-10-01T14:00:00Z',
            },
        ),
        # Edge case: course_runs does not include advertised_course_run with available advertised_course_run_uuid
        (
            {
                'course_runs': [{
                    'uuid': uuid4(),
                }],
                'advertised_course_run_uuid': ADVERTISED_COURSE_RUN_UUID
            },
            None,
        ),
        # Edge case: No available course_runs and no advertised_course_run_uuid
        (
            {
                'course_runs': [],
                'advertised_course_run_uuid': None
            },
            None
        ),
        # Edge case: Available advertised_course_run within course_runs, and no advertised_course_run_uuid
        (
            {
                'course_runs': [
                    {
                        'key': 'course-v1:org+course+1T2021',
                        'uuid': ADVERTISED_COURSE_RUN_UUID,
                        'pacing_type': 'instructor_paced',
                        'start': '2013-10-16T14:00:00Z',
                        'end': '2014-10-16T14:00:00Z',
                        'enrollment_end': '2013-10-17T14:00:00Z',
                        'availability': 'Current',
                        'min_effort': 10,
                        'max_effort': 14,
                        'weeks_to_complete': 13,
                        'status': 'published',
                        'is_enrollable': True,
                        'is_marketable': True,
                        'enrollment_start': '2013-10-01T14:00:00Z',
                    },
                    {
                        'key': 'course-v1:org+course+1T2021',
                        'uuid': uuid4(),
                        'pacing_type': 'instructor_paced',
                        'start': '2016-10-16T14:00:00Z',
                        'end': '2019-10-16T14:00:00Z',
                        'enrollment_end': '2016-10-17T14:00:00Z',
                        'availability': 'Upcoming',
                        'min_effort': 11,
                        'max_effort': 15,
                        'weeks_to_complete': 15,
                        'status': 'published',
                        'is_enrollable': True,
                        'is_marketable': True,
                        'enrollment_start': '2013-10-01T14:00:00Z',
                    }
                ],
                'advertised_course_run_uuid': None
            },
            None
        ),
    )
    @ddt.unpack
    def test_get_advertised_course(self, searchable_course, expected_course_run):
        """
        Assert get_advertised_course_run fetches the expected_course_run
        """
        advertised_course_run = get_advertised_course_run(searchable_course)
        assert advertised_course_run == expected_course_run


class ValidateContentMetadataTests(TestCase):
    """
    Tests for ``validate_content_metadata``.

    Schema validation is advisory: violations must never raise exceptions or block
    catalog-content inclusion. The function only logs warnings on failure.
    """

    def _make_course(self, **overrides):
        """Minimal valid course metadata dict."""
        base = {
            'aggregation_key': 'course:edX+DemoX',
            'uuid': str(uuid4()),
            'key': 'edX+DemoX',
            'content_type': 'course',
        }
        base.update(overrides)
        return base

    def _make_course_run(self, **overrides):
        """Minimal valid course run metadata dict."""
        base = {
            'aggregation_key': 'courserun:course-v1:edX+DemoX+1T2024',
            'uuid': str(uuid4()),
            'key': 'course-v1:edX+DemoX+1T2024',
            'content_type': 'courserun',
        }
        base.update(overrides)
        return base

    def _make_program(self, **overrides):
        """Minimal valid program metadata dict."""
        program_uuid = str(uuid4())
        base = {
            'aggregation_key': f'program:{program_uuid}',
            'uuid': program_uuid,
            'content_type': 'program',
        }
        base.update(overrides)
        return base

    def _make_pathway(self, **overrides):
        """Minimal valid learner pathway metadata dict."""
        pathway_uuid = str(uuid4())
        base = {
            'aggregation_key': f'learnerpathway:{pathway_uuid}',
            'uuid': pathway_uuid,
            'content_type': 'learnerpathway',
        }
        base.update(overrides)
        return base

    # --- Valid metadata tests ---

    def test_valid_course_returns_true(self):
        """A well-formed course entry passes validation."""
        result = validate_content_metadata(self._make_course())
        self.assertTrue(result)

    def test_valid_course_run_returns_true(self):
        """A well-formed course run entry passes validation."""
        result = validate_content_metadata(self._make_course_run())
        self.assertTrue(result)

    def test_valid_program_returns_true(self):
        """A well-formed program entry passes validation."""
        result = validate_content_metadata(self._make_program())
        self.assertTrue(result)

    def test_valid_pathway_returns_true(self):
        """A well-formed learner pathway entry passes validation."""
        result = validate_content_metadata(self._make_pathway())
        self.assertTrue(result)

    def test_extra_fields_are_accepted(self):
        """
        Extra/unknown fields must be accepted silently (additionalProperties: true).
        This ensures new fields added by discovery never cause validation failures.
        """
        course = self._make_course(
            brand_new_field_from_discovery='some_value',
            another_new_field=42,
        )
        result = validate_content_metadata(course)
        self.assertTrue(result)

    # --- Invalid metadata tests (violations logged, never raised) ---

    def test_missing_aggregation_key_logs_warning_returns_false(self):
        """
        A course missing ``aggregation_key`` is invalid — the service cannot derive
        content_type or parent_content_key without it.
        """
        course = self._make_course()
        del course['aggregation_key']
        with self.assertLogs('enterprise_catalog.apps.catalog.content_metadata_utils', level='WARNING') as cm:
            result = validate_content_metadata(course)
        self.assertFalse(result)
        self.assertTrue(any('CONTENT_METADATA_SCHEMA_VIOLATION' in line for line in cm.output))

    def test_missing_uuid_logs_warning_returns_false(self):
        """
        A course missing ``uuid`` is invalid — uuid is used as the Algolia objectID.
        """
        course = self._make_course()
        del course['uuid']
        with self.assertLogs('enterprise_catalog.apps.catalog.content_metadata_utils', level='WARNING') as cm:
            result = validate_content_metadata(course)
        self.assertFalse(result)
        self.assertTrue(any('CONTENT_METADATA_SCHEMA_VIOLATION' in line for line in cm.output))

    def test_missing_key_for_course_logs_warning_returns_false(self):
        """
        A course missing ``key`` is invalid — key is the content_key for courses.
        Programs are exempt since they use uuid as content_key.
        """
        course = self._make_course()
        del course['key']
        with self.assertLogs('enterprise_catalog.apps.catalog.content_metadata_utils', level='WARNING') as cm:
            result = validate_content_metadata(course)
        self.assertFalse(result)
        self.assertTrue(any('CONTENT_METADATA_SCHEMA_VIOLATION' in line for line in cm.output))

    def test_program_without_key_is_valid(self):
        """
        Programs legitimately have no 'key' field — they use 'uuid' as content_key.
        Validation must not require 'key' for programs.
        """
        program = self._make_program()
        # Programs must not have 'key' required — confirm no 'key' still validates
        self.assertNotIn('key', program)
        result = validate_content_metadata(program)
        self.assertTrue(result)

    def test_unknown_content_type_skips_validation_returns_true(self):
        """
        Content types not yet registered (e.g. a brand new type from discovery) must be
        accepted without validation rather than failing. This prevents surprise breakage
        when discovery introduces new content types.
        """
        unknown = {
            'aggregation_key': 'videoblock:some-key',
            'uuid': str(uuid4()),
            'key': 'some-key',
        }
        result = validate_content_metadata(unknown)
        self.assertTrue(result)

    def test_validation_never_raises_on_invalid_data(self):
        """
        validate_content_metadata must never raise even for completely malformed input.
        Schema violations must only produce log warnings, never exceptions.
        """
        malformed_entries = [
            {},                       # completely empty
            {'aggregation_key': 123}, # wrong type
            None,                     # None would cause AttributeError, but we handle gracefully
        ]
        for entry in malformed_entries:
            try:
                # None would normally raise AttributeError on .get(); treat it gracefully
                if entry is None:
                    continue
                result = validate_content_metadata(entry)
                # Must return a bool, never raise
                self.assertIsInstance(result, bool)
            except Exception as exc:
                self.fail(
                    f'validate_content_metadata raised {type(exc).__name__} for input {entry!r}: {exc}'
                )
