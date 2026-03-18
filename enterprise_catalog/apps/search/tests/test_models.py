"""
Tests for search app models.
"""
from datetime import timedelta

import ddt
from django.test import TestCase
from django.utils import timezone

from enterprise_catalog.apps.catalog.constants import COURSE
from enterprise_catalog.apps.catalog.tests import factories
from enterprise_catalog.apps.search.models import ContentMetadataIndexingState


@ddt.ddt
class ContentMetadataIndexingStateTests(TestCase):
    """
    Tests for ContentMetadataIndexingState model.
    """

    def test_create_indexing_state(self):
        """
        Test creating an indexing state for content metadata.
        """
        content = factories.ContentMetadataFactory(
            content_key='edX+TestCourse',
            content_type=COURSE,
        )
        state = ContentMetadataIndexingState.objects.create(
            content_metadata=content,
        )
        self.assertIsNone(state.last_indexed_at)
        self.assertIsNone(state.last_failure_at)
        self.assertIsNone(state.removed_from_index_at)
        self.assertEqual(state.algolia_object_ids, [])
        self.assertIsNone(state.failure_reason)

    def test_get_or_create_for_content(self):
        """
        Test get_or_create_for_content helper method.
        """
        content = factories.ContentMetadataFactory(
            content_key='edX+TestCourse2',
            content_type=COURSE,
        )
        # First call creates the state
        state1 = ContentMetadataIndexingState.get_or_create_for_content(content)
        self.assertEqual(state1.content_metadata, content)

        # Second call returns the same state
        state2 = ContentMetadataIndexingState.get_or_create_for_content(content)
        self.assertEqual(state1.pk, state2.pk)

    def test_mark_as_indexed(self):
        """
        Test marking content as successfully indexed.
        """
        content = factories.ContentMetadataFactory(
            content_key='edX+TestCourse3',
            content_type=COURSE,
        )
        state = ContentMetadataIndexingState.objects.create(
            content_metadata=content,
            last_failure_at=timezone.now(),
            failure_reason='Previous failure',
        )

        algolia_ids = ['obj-1', 'obj-2', 'obj-3']
        state.mark_as_indexed(algolia_object_ids=algolia_ids)

        state.refresh_from_db()
        self.assertIsNotNone(state.last_indexed_at)
        self.assertIsNone(state.last_failure_at)
        self.assertIsNone(state.failure_reason)
        self.assertIsNone(state.removed_from_index_at)
        self.assertEqual(state.algolia_object_ids, algolia_ids)

    def test_mark_as_failed(self):
        """
        Test marking content as having failed indexing.
        """
        content = factories.ContentMetadataFactory(
            content_key='edX+TestCourse4',
            content_type=COURSE,
        )
        state = ContentMetadataIndexingState.objects.create(
            content_metadata=content,
        )

        state.mark_as_failed('Connection timeout')

        state.refresh_from_db()
        self.assertIsNotNone(state.last_failure_at)
        self.assertEqual(state.failure_reason, 'Connection timeout')

    def test_mark_as_removed(self):
        """
        Test marking content as removed from the index.
        """
        content = factories.ContentMetadataFactory(
            content_key='edX+TestCourse5',
            content_type=COURSE,
        )
        state = ContentMetadataIndexingState.objects.create(
            content_metadata=content,
            algolia_object_ids=['obj-1', 'obj-2'],
        )

        state.mark_as_removed()

        state.refresh_from_db()
        self.assertIsNotNone(state.removed_from_index_at)
        self.assertEqual(state.algolia_object_ids, [])

    @ddt.data(
        # (last_indexed_at_offset, content_modified_offset, expected_is_stale)
        # Never indexed - should be stale
        (None, timedelta(hours=-1), True),
        # Indexed before content was modified - should be stale
        (timedelta(hours=-2), timedelta(hours=-1), True),
        # Indexed after content was modified - should not be stale
        (timedelta(hours=-1), timedelta(hours=-2), False),
        # Indexed at same time as content modified - should not be stale
        (timedelta(hours=-1), timedelta(hours=-1), False),
    )
    @ddt.unpack
    def test_is_stale(self, last_indexed_offset, content_modified_offset, expected_is_stale):
        """
        Test the is_stale property for various scenarios.
        """
        now = timezone.now()
        content = factories.ContentMetadataFactory(
            content_key=f'edX+TestCourse-stale-{last_indexed_offset}-{content_modified_offset}',
            content_type=COURSE,
            modified=now + content_modified_offset,
        )

        last_indexed_at = None
        if last_indexed_offset is not None:
            last_indexed_at = now + last_indexed_offset

        state = ContentMetadataIndexingState.objects.create(
            content_metadata=content,
            last_indexed_at=last_indexed_at,
        )

        self.assertEqual(state.is_stale, expected_is_stale)

    def test_str_representation(self):
        """
        Test the string representation of the model.
        """
        content = factories.ContentMetadataFactory(
            content_key='edX+StrTest',
            content_type=COURSE,
        )
        state = ContentMetadataIndexingState.objects.create(
            content_metadata=content,
        )
        self.assertEqual(str(state), 'IndexingState for edX+StrTest')

    def test_one_to_one_relationship(self):
        """
        Test that the one-to-one relationship works correctly.
        """
        content = factories.ContentMetadataFactory(
            content_key='edX+OneToOneTest',
            content_type=COURSE,
        )
        state = ContentMetadataIndexingState.objects.create(
            content_metadata=content,
        )

        # Access indexing_state from content
        self.assertEqual(content.indexing_state, state)

        # Access content_metadata from state
        self.assertEqual(state.content_metadata, content)

    def test_cascade_delete(self):
        """
        Test that deleting ContentMetadata cascades to delete IndexingState.
        """
        content = factories.ContentMetadataFactory(
            content_key='edX+CascadeTest',
            content_type=COURSE,
        )
        state = ContentMetadataIndexingState.objects.create(
            content_metadata=content,
        )
        state_pk = state.pk

        # Delete the content
        content.delete()

        # State should be deleted too
        self.assertFalse(
            ContentMetadataIndexingState.objects.filter(pk=state_pk).exists()
        )
