"""
Tests for populate_spanish_translations management command and related management utilities.
"""
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from enterprise_catalog.apps.catalog.management.utils import (
    iter_queryset_in_batches,
)
from enterprise_catalog.apps.catalog.models import (
    ContentMetadata,
    ContentTranslation,
)
from enterprise_catalog.apps.catalog.tests.factories import (
    ContentMetadataFactory,
)
from enterprise_catalog.apps.catalog.utils import compute_source_hash


class PopulateSpanishTranslationsCommandTests(TestCase):
    """
    Tests for the populate_spanish_translations management command.
    """

    def setUp(self):
        """Set up test data."""
        self.content1 = ContentMetadataFactory(
            content_key='course-1',
            content_type='course',
            json_metadata={
                'title': 'Introduction to Python',
                'short_description': 'Learn Python basics',
                'full_description': 'A comprehensive course on Python fundamentals',
            }
        )
        self.content2 = ContentMetadataFactory(
            content_key='course-2',
            content_type='course',
            json_metadata={
                'title': 'Advanced JavaScript',
                'short_description': 'Master JavaScript',
            }
        )

    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.'
        'populate_spanish_translations.translate_object_fields'
    )
    def test_command_creates_translations(self, mock_translate):
        """Test that command creates new translations."""
        mock_translate.return_value = {
            'title': 'Título traducido',
            'short_description': 'Descripción corta',
        }

        # Run command
        call_command('populate_spanish_translations', all=True)

        # Check translations were created
        self.assertEqual(ContentTranslation.objects.count(), 2)

        translation1 = ContentTranslation.objects.get(content_metadata=self.content1)
        self.assertEqual(translation1.language_code, 'es')
        self.assertEqual(translation1.title, 'Título traducido')
        self.assertIsNotNone(translation1.source_hash)

    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.'
        'populate_spanish_translations.translate_object_fields'
    )
    def test_command_skips_existing_translations(self, mock_translate):
        """Test that command skips translations with matching hash."""
        mock_translate.return_value = {'title': 'Translated'}

        # Create existing translation with correct hash
        source_hash = compute_source_hash(self.content1)
        ContentTranslation.objects.create(
            content_metadata=self.content1,
            language_code='es',
            title='Existing Translation',
            source_hash=source_hash
        )

        # Run command
        call_command('populate_spanish_translations', all=True)

        # Should not update existing translation
        translation = ContentTranslation.objects.get(content_metadata=self.content1)
        self.assertEqual(translation.title, 'Existing Translation')

        # Should create translation for content2
        self.assertEqual(ContentTranslation.objects.count(), 2)

    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.'
        'populate_spanish_translations.translate_object_fields'
    )
    def test_command_force_retranslate(self, mock_translate):
        """Test that --force flag re-translates existing content."""
        mock_translate.return_value = {'title': 'New Translation'}

        # Create existing translation
        ContentTranslation.objects.create(
            content_metadata=self.content1,
            language_code='es',
            title='Old Translation',
            source_hash='old_hash'
        )

        # Run command with force
        call_command('populate_spanish_translations', force=True, all=True)

        # Should update existing translation
        translation = ContentTranslation.objects.get(content_metadata=self.content1)
        self.assertEqual(translation.title, 'New Translation')

    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.'
        'populate_spanish_translations.translate_object_fields'
    )
    def test_command_content_keys_filter(self, mock_translate):
        """Test filtering by content keys."""
        mock_translate.return_value = {'title': 'Translated'}

        # Run command for only content1
        call_command('populate_spanish_translations', content_keys=['course-1'], all=True)

        # Should only create translation for content1
        self.assertEqual(ContentTranslation.objects.count(), 1)
        self.assertTrue(
            ContentTranslation.objects.filter(content_metadata=self.content1).exists()
        )
        self.assertFalse(
            ContentTranslation.objects.filter(content_metadata=self.content2).exists()
        )

    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.'
        'populate_spanish_translations.translate_object_fields'
    )
    def test_command_dry_run(self, mock_translate):
        """Test that --dry-run doesn't save translations."""
        mock_translate.return_value = {'title': 'Translated'}

        # Run command in dry-run mode
        call_command('populate_spanish_translations', dry_run=True, all=True)

        # No translations should be saved
        self.assertEqual(ContentTranslation.objects.count(), 0)

        # But translate should still be called
        self.assertTrue(mock_translate.called)

    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.'
        'populate_spanish_translations.translate_object_fields'
    )
    def test_command_updates_stale_translations(self, mock_translate):
        """Test that command updates translations when content changes."""
        mock_translate.return_value = {'title': 'Updated Translation'}

        # Create translation with old hash
        ContentTranslation.objects.create(
            content_metadata=self.content1,
            language_code='es',
            title='Old Translation',
            source_hash='outdated_hash'  # Different from current content
        )

        # Run command (without force)
        call_command('populate_spanish_translations', all=True)

        # Should update the translation because hash doesn't match
        translation = ContentTranslation.objects.get(content_metadata=self.content1)
        self.assertEqual(translation.title, 'Updated Translation')

    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.'
        'populate_spanish_translations.translate_object_fields'
    )
    def test_command_batch_processing(self, mock_translate):
        """Test command processes in batches."""
        # Create more content
        for i in range(5):
            ContentMetadataFactory(
                content_key=f'course-{i + 3}',
                json_metadata={'title': f'Course {i + 3}'}
            )

        mock_translate.return_value = {'title': 'Translated'}

        # Run with small batch size
        call_command('populate_spanish_translations', batch_size=2, all=True)

        # All content should be translated
        self.assertEqual(ContentTranslation.objects.count(), 7)  # 2 original + 5 new

    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.'
        'populate_spanish_translations.translate_object_fields'
    )
    def test_command_handles_translation_errors(self, mock_translate):
        """Test command continues after translation errors."""
        # First call succeeds, second fails, third succeeds
        mock_translate.side_effect = [
            {'title': 'Success 1'},
            Exception('Translation API error'),
            {'title': 'Success 2'},
        ]

        # Create third content
        ContentMetadataFactory(content_key='course-3', json_metadata={'title': 'Course 3'})

        # Run command - should not crash
        call_command('populate_spanish_translations', all=True)

        # Should have created 2 translations (skipped the one that errored)
        self.assertEqual(ContentTranslation.objects.count(), 2)

    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.'
        'populate_spanish_translations.translate_object_fields'
    )
    def test_command_translates_all_fields(self, mock_translate):
        """Test that command translates all relevant fields."""
        mock_translate.return_value = {
            'title': 'Título',
            'short_description': 'Descripción corta',
            'full_description': 'Descripción completa',
            'subtitle': 'Subtítulo',
        }

        # Run command
        call_command('populate_spanish_translations', all=True)

        translation = ContentTranslation.objects.first()
        self.assertEqual(translation.title, 'Título')
        self.assertEqual(translation.short_description, 'Descripción corta')
        self.assertEqual(translation.full_description, 'Descripción completa')
        self.assertEqual(translation.subtitle, 'Subtítulo')

    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.'
        'populate_spanish_translations.translate_object_fields'
    )
    def test_command_truncates_title_to_field_limit(self, mock_translate):
        """Test that overlong translated titles are truncated to avoid DB DataError."""
        max_title_length = ContentTranslation._meta.get_field('title').max_length
        too_long_title = 't' * (max_title_length + 50)
        mock_translate.return_value = {
            'title': too_long_title,
            'subtitle': 'Subtítulo',
        }

        call_command('populate_spanish_translations', all=True)

        translation = ContentTranslation.objects.get(content_metadata=self.content1)
        self.assertEqual(len(translation.title), max_title_length)
        self.assertEqual(translation.title, too_long_title[:max_title_length])

    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.'
        'populate_spanish_translations.translate_object_fields'
    )
    def test_command_truncates_subtitle_to_field_limit(self, mock_translate):
        """Test that overlong translated subtitles are truncated to avoid DB DataError."""
        max_subtitle_length = ContentTranslation._meta.get_field('subtitle').max_length
        too_long_subtitle = 's' * (max_subtitle_length + 50)
        mock_translate.return_value = {
            'title': 'Título',
            'subtitle': too_long_subtitle,
        }

        call_command('populate_spanish_translations', all=True)

        translation = ContentTranslation.objects.get(content_metadata=self.content1)
        self.assertEqual(len(translation.subtitle), max_subtitle_length)
        self.assertEqual(translation.subtitle, too_long_subtitle[:max_subtitle_length])

    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.'
        'populate_spanish_translations._should_index_course'
    )
    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.'
        'populate_spanish_translations.translate_object_fields'
    )
    def test_command_skips_non_indexable_content(self, mock_translate, mock_should_index):
        """Test that content not eligible for indexing is skipped."""
        mock_should_index.return_value = False
        mock_translate.return_value = {'title': 'Translated'}

        # Run command (without all=True)
        call_command('populate_spanish_translations')

        # Should check if it should be indexed
        mock_should_index.assert_called()
        # Should NOT translate
        mock_translate.assert_not_called()
        # Should NOT create translation
        self.assertEqual(ContentTranslation.objects.count(), 0)

    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.'
        'populate_spanish_translations.get_advertised_course_run'
    )
    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.'
        'populate_spanish_translations._should_index_course'
    )
    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.'
        'populate_spanish_translations.translate_object_fields'
    )
    def test_command_skips_archived_content(self, mock_translate, mock_should_index, mock_get_run):
        """Test that content with an archived advertised run is skipped."""
        mock_should_index.return_value = True
        mock_get_run.return_value = {'availability': 'Archived'}
        mock_translate.return_value = {'title': 'Translated'}

        # Run command (without all=True)
        call_command('populate_spanish_translations')

        # Should check if it should be indexed
        mock_should_index.assert_called()
        # Should NOT translate
        mock_translate.assert_not_called()
        # Should NOT create translation
        self.assertEqual(ContentTranslation.objects.count(), 0)

    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.'
        'populate_spanish_translations._should_index_course'
    )
    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.'
        'populate_spanish_translations.translate_object_fields'
    )
    def test_command_all_flag_bypasses_optimization(self, mock_translate, mock_should_index):
        """Test that the --all flag bypasses the indexing check."""
        mock_should_index.return_value = False
        mock_translate.return_value = {'title': 'Translated'}

        # Run command with all=True
        call_command('populate_spanish_translations', all=True)

        # Should NOT even call the indexing check
        mock_should_index.assert_not_called()
        # Should translate anyway
        mock_translate.assert_called()
        # Should create translation
        self.assertEqual(ContentTranslation.objects.count(), 2)

    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.'
        'populate_spanish_translations.translate_object_fields'
    )
    def test_command_missing_only_processes_only_missing(self, mock_translate):
        """Test that --missing-only only translates records without an es row."""
        mock_translate.return_value = {'title': 'Translated'}

        existing_hash = compute_source_hash(self.content1)
        ContentTranslation.objects.create(
            content_metadata=self.content1,
            language_code='es',
            title='Already Translated',
            source_hash=existing_hash,
        )

        call_command('populate_spanish_translations', missing_only=True, all=True)

        self.assertEqual(ContentTranslation.objects.count(), 2)
        self.assertEqual(
            ContentTranslation.objects.get(content_metadata=self.content1).title,
            'Already Translated'
        )
        self.assertTrue(ContentTranslation.objects.filter(content_metadata=self.content2).exists())
        self.assertEqual(mock_translate.call_count, 1)

    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.'
        'populate_spanish_translations.translate_object_fields'
    )
    def test_command_missing_only_with_content_keys_respects_filter(self, mock_translate):
        """Test that --missing-only combined with content key filter only processes missing keyed rows."""
        mock_translate.return_value = {'title': 'Translated'}

        existing_hash = compute_source_hash(self.content1)
        ContentTranslation.objects.create(
            content_metadata=self.content1,
            language_code='es',
            title='Already Translated',
            source_hash=existing_hash,
        )

        call_command(
            'populate_spanish_translations',
            missing_only=True,
            all=True,
            content_keys=['course-1'],
        )

        self.assertEqual(ContentTranslation.objects.count(), 1)
        mock_translate.assert_not_called()

    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.'
        'populate_spanish_translations.logger'
    )
    @mock.patch(
        'enterprise_catalog.apps.catalog.management.commands.'
        'populate_spanish_translations.translate_object_fields'
    )
    def test_command_logging(self, mock_translate, mock_logger):
        """Test that --missing-only produces the correct log output."""
        mock_translate.return_value = {'title': 'Translated'}

        existing_hash = compute_source_hash(self.content1)
        ContentTranslation.objects.create(
            content_metadata=self.content1,
            language_code='es',
            title='Already Translated',
            source_hash=existing_hash,
        )

        call_command('populate_spanish_translations', missing_only=True, all=True)

        # Verify the [MISSING_ONLY] log message was called
        mock_logger.info.assert_any_call(
            '[MISSING_ONLY] Found %s content items missing %s translations',
            1,
            'es',
        )


class IterQuerysetInBatchesTests(TestCase):
    """
    Unit tests for the ``iter_queryset_in_batches`` management utility.

    This helper lives in ``management/utils.py`` so it can be reused by any
    future management command that needs PK-pivot batch iteration over an
    arbitrary queryset (including those with ``exclude(Exists(...))`` or other
    complex predicates that cannot be expressed as a plain ``Q()`` filter).
    """

    def _make_content(self, count):
        """Create *count* ContentMetadata objects and return the queryset, ordered by pk."""
        for i in range(count):
            ContentMetadataFactory(content_key=f'batch-test-course-{i}')
        return ContentMetadata.objects.filter(content_key__startswith='batch-test-course-').order_by('pk')

    def test_yields_all_records_single_batch(self):
        """All records fit in one batch when batch_size >= total count."""
        qs = self._make_content(3)
        batches = list(iter_queryset_in_batches(qs, batch_size=10))
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]), 3)

    def test_yields_correct_number_of_batches(self):
        """Records are split across the expected number of batches."""
        qs = self._make_content(5)
        batches = list(iter_queryset_in_batches(qs, batch_size=2))
        # 5 records / batch_size 2 → 3 batches (2, 2, 1)
        self.assertEqual(len(batches), 3)
        self.assertEqual(len(batches[0]), 2)
        self.assertEqual(len(batches[1]), 2)
        self.assertEqual(len(batches[2]), 1)

    def test_all_records_are_yielded_exactly_once(self):
        """Every record appears in exactly one batch, no duplicates or omissions."""
        qs = self._make_content(7)
        expected_pks = set(qs.values_list('pk', flat=True))
        seen_pks = set()
        for batch in iter_queryset_in_batches(qs, batch_size=3):
            for obj in batch:
                self.assertNotIn(obj.pk, seen_pks, 'Duplicate record detected across batches')
                seen_pks.add(obj.pk)
        self.assertEqual(seen_pks, expected_pks)

    def test_empty_queryset_yields_nothing(self):
        """An empty queryset produces no batches."""
        qs = ContentMetadata.objects.none()
        batches = list(iter_queryset_in_batches(qs, batch_size=10))
        self.assertEqual(batches, [])

    def test_preserves_queryset_predicates(self):
        """Only records matching the queryset's filter are returned."""
        self._make_content(4)
        qs = ContentMetadata.objects.filter(
            content_key__in=['batch-test-course-0', 'batch-test-course-1']
        )
        all_yielded = [obj for batch in iter_queryset_in_batches(qs, batch_size=10) for obj in batch]
        self.assertEqual(len(all_yielded), 2)
        yielded_keys = {obj.content_key for obj in all_yielded}
        self.assertEqual(yielded_keys, {'batch-test-course-0', 'batch-test-course-1'})

    def test_batch_size_one(self):
        """Each batch contains exactly one record when batch_size=1."""
        qs = self._make_content(3)
        batches = list(iter_queryset_in_batches(qs, batch_size=1))
        self.assertEqual(len(batches), 3)
        for batch in batches:
            self.assertEqual(len(batch), 1)

    def test_records_in_pk_order(self):
        """Records within each batch and across batches are in ascending PK order."""
        qs = self._make_content(6)
        all_pks = [obj.pk for batch in iter_queryset_in_batches(qs, batch_size=2) for obj in batch]
        self.assertEqual(all_pks, sorted(all_pks))

    def test_zero_batch_size_raises_value_error(self):
        """A zero batch size is rejected instead of silently yielding nothing."""
        qs = self._make_content(1)

        with self.assertRaisesMessage(ValueError, 'batch_size must be a positive integer'):
            list(iter_queryset_in_batches(qs, batch_size=0))

    def test_negative_batch_size_raises_value_error(self):
        """A negative batch size is rejected before queryset slicing occurs."""
        qs = self._make_content(1)

        with self.assertRaisesMessage(ValueError, 'batch_size must be a positive integer'):
            list(iter_queryset_in_batches(qs, batch_size=-1))

    def test_non_integer_batch_size_raises_value_error(self):
        """A non-integer batch size is rejected early."""
        qs = self._make_content(1)

        with self.assertRaisesMessage(ValueError, 'batch_size must be a positive integer'):
            list(iter_queryset_in_batches(qs, batch_size='10'))
