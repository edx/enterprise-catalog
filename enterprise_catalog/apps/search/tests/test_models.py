"""
Tests for ``ContentMetadataIndexingState`` model behavior.
"""
from datetime import timedelta

import ddt
from django.db import IntegrityError
from django.test import TestCase

from enterprise_catalog.apps.catalog.tests.factories import (
    ContentMetadataFactory,
)
from enterprise_catalog.apps.catalog.utils import localized_utcnow
from enterprise_catalog.apps.search.models import ContentMetadataIndexingState
from enterprise_catalog.apps.search.tests.factories import (
    ContentMetadataIndexingStateFactory,
)


@ddt.ddt
class TestContentMetadataIndexingState(TestCase):
    """
    Tests for the ``ContentMetadataIndexingState`` model.
    """

    def test_get_or_create_creates_with_empty_timestamps(self):
        """
        First call creates a state with no indexing/failure/removal timestamps.
        """
        content = ContentMetadataFactory()

        state, created = ContentMetadataIndexingState.get_or_create_for_content(content)

        self.assertTrue(created)
        self.assertEqual(state.content_metadata, content)
        self.assertIsNone(state.last_indexed_at)
        self.assertIsNone(state.last_failure_at)
        self.assertIsNone(state.removed_from_index_at)
        self.assertIsNone(state.failure_reason)
        self.assertEqual(state.algolia_object_ids, [])

    def test_get_or_create_returns_existing(self):
        """
        Second call returns the existing state rather than creating a new one.
        """
        content = ContentMetadataFactory()
        existing = ContentMetadataIndexingStateFactory(content_metadata=content)

        state, created = ContentMetadataIndexingState.get_or_create_for_content(content)

        self.assertFalse(created)
        self.assertEqual(state.uuid, existing.uuid)

    def test_is_stale_when_never_indexed(self):
        """
        A state with ``last_indexed_at=None`` is always stale.
        """
        state = ContentMetadataIndexingStateFactory()
        self.assertIsNone(state.last_indexed_at)
        self.assertTrue(state.is_stale)

    @ddt.data(
        # (index_offset_seconds, expected_stale)
        # Indexed *before* content modified → stale
        (-60, True),
        # Indexed *after* content modified → fresh
        (60, False),
    )
    @ddt.unpack
    def test_is_stale_compares_modified_to_last_indexed(self, index_offset_seconds, expected_stale):
        """
        ``is_stale`` compares content's ``modified`` to ``last_indexed_at``.
        """
        content = ContentMetadataFactory()
        state = ContentMetadataIndexingStateFactory(
            content_metadata=content,
            last_indexed_at=content.modified + timedelta(seconds=index_offset_seconds),
        )
        self.assertEqual(state.is_stale, expected_stale)

    def test_mark_as_indexed(self):
        """
        ``mark_as_indexed`` stamps the time, stores object IDs, and clears failures.
        """
        state = ContentMetadataIndexingStateFactory(
            last_failure_at=localized_utcnow(),
            failure_reason='previous boom',
        )
        before = localized_utcnow()

        state.mark_as_indexed(algolia_object_ids=['course-v1-shard-0', 'course-v1-shard-1'])

        state.refresh_from_db()
        self.assertGreaterEqual(state.last_indexed_at, before)
        self.assertEqual(
            state.algolia_object_ids,
            ['course-v1-shard-0', 'course-v1-shard-1'],
        )
        self.assertIsNone(state.last_failure_at)
        self.assertIsNone(state.failure_reason)

    def test_mark_as_indexed_clears_prior_removal_timestamp(self):
        """
        A REMOVED→INDEXED resurrection clears ``removed_from_index_at`` so the
        row reflects the current state instead of carrying both stamps.
        """
        state = ContentMetadataIndexingStateFactory(
            removed_from_index_at=localized_utcnow() - timedelta(hours=1),
        )

        state.mark_as_indexed(algolia_object_ids=['shard-0'])

        state.refresh_from_db()
        self.assertIsNone(state.removed_from_index_at)
        self.assertIsNotNone(state.last_indexed_at)

    def test_mark_as_indexed_without_object_ids_preserves_existing(self):
        """
        Omitting ``algolia_object_ids`` leaves the stored IDs untouched.
        """
        state = ContentMetadataIndexingStateFactory(
            algolia_object_ids=['existing-shard'],
        )

        state.mark_as_indexed()

        state.refresh_from_db()
        self.assertEqual(state.algolia_object_ids, ['existing-shard'])
        self.assertIsNotNone(state.last_indexed_at)

    def test_mark_as_failed(self):
        """
        ``mark_as_failed`` records the reason without touching ``last_indexed_at``.
        """
        original_indexed_at = localized_utcnow() - timedelta(hours=1)
        state = ContentMetadataIndexingStateFactory(last_indexed_at=original_indexed_at)

        state.mark_as_failed(reason='algolia 503')

        state.refresh_from_db()
        self.assertIsNotNone(state.last_failure_at)
        self.assertEqual(state.failure_reason, 'algolia 503')
        # Prior successful index timestamp should not be disturbed.
        self.assertEqual(state.last_indexed_at, original_indexed_at)

    def test_mark_as_failed_stringifies_exception(self):
        """
        Exception objects passed as reason are coerced to their string form.
        """
        state = ContentMetadataIndexingStateFactory()
        err = ValueError('bad payload')

        state.mark_as_failed(reason=err)

        state.refresh_from_db()
        self.assertEqual(state.failure_reason, 'bad payload')

    def test_mark_as_removed(self):
        """
        ``mark_as_removed`` stamps the removal time.
        """
        state = ContentMetadataIndexingStateFactory()
        before = localized_utcnow()

        state.mark_as_removed()

        state.refresh_from_db()
        self.assertGreaterEqual(state.removed_from_index_at, before)

    def test_one_to_one_constraint(self):
        """
        A ContentMetadata can have at most one indexing state row.
        """
        content = ContentMetadataFactory()
        ContentMetadataIndexingStateFactory(content_metadata=content)

        with self.assertRaises(IntegrityError):
            ContentMetadataIndexingStateFactory(content_metadata=content)
