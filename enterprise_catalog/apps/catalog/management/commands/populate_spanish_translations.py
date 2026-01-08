"""
Management command to pre-populate Spanish translations for content metadata.
"""
import logging

from django.core.management.base import BaseCommand
from django.db.models import Q

from enterprise_catalog.apps.catalog.algolia_utils import (
    _should_index_course,
    _should_index_program,
)
from enterprise_catalog.apps.catalog.constants import COURSE, PROGRAM
from enterprise_catalog.apps.catalog.content_metadata_utils import (
    get_advertised_course_run,
)
from enterprise_catalog.apps.catalog.models import (
    ContentMetadata,
    ContentTranslation,
)
from enterprise_catalog.apps.catalog.translation_utils import (
    translate_object_fields,
)
from enterprise_catalog.apps.catalog.utils import (
    batch_by_pk,
    compute_source_hash,
)


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """
    Management command to pre-populate Spanish translations for content metadata.

    This command translates content metadata fields to Spanish and stores them
    in the ContentTranslation model for faster Algolia indexing.

    Note: This command only supports Spanish translation at this time.

    Example usage:
        # Populate all translations
        ./manage.py populate_spanish_translations

        # Populate specific content
        ./manage.py populate_spanish_translations --content-keys "course-v1:edX+DemoX"

        # Force re-translation
        ./manage.py populate_spanish_translations --force

        # Process in smaller batches
        ./manage.py populate_spanish_translations --batch-size 50
    """
    help = 'Pre-populate Spanish translations for content metadata'

    def add_arguments(self, parser):
        parser.add_argument(
            '--content-keys',
            nargs='+',
            help='Specific content keys to translate'
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force re-translation even if translation exists and hash matches'
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=100,
            help='Number of items to process in each batch (default: 100)'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Run without actually saving translations'
        )
        parser.add_argument(
            '--all',
            action='store_true',
            help='Translate all content, even if it would not be indexed in Algolia'
        )

    def handle(self, *args, **options):
        """
        Main command handler.
        """
        content_keys = options.get('content_keys')
        force = options.get('force')
        batch_size = options.get('batch_size')
        language_code = 'es'  # Only Spanish is supported at this time
        dry_run = options.get('dry_run')
        all_content = options.get('all')

        # Build queryset
        queryset = ContentMetadata.objects.all()
        extra_filter = Q()
        if content_keys:
            extra_filter = Q(content_key__in=content_keys)
            queryset = queryset.filter(extra_filter)

        total_count = queryset.count()
        logger.info(
            'Starting translation for %s content items to %s',
            total_count,
            language_code
        )

        if dry_run:
            logger.warning('DRY RUN MODE - No changes will be saved')

        processed_count = 0
        created_count = 0
        updated_count = 0
        skipped_count = 0
        error_count = 0

        # Process in batches
        # Process in batches using efficient keyset pagination
        for batch in batch_by_pk(ContentMetadata, extra_filter=extra_filter, batch_size=batch_size):

            logger.info('Processing batch of %s items...', len(batch))

            for content in batch:
                try:
                    result = self._process_content(
                        content,
                        language_code,
                        force,
                        dry_run,
                        all_content
                    )

                    if result == 'created':
                        created_count += 1
                    elif result == 'updated':
                        updated_count += 1
                    elif result == 'skipped':
                        skipped_count += 1

                    processed_count += 1

                    if processed_count % 10 == 0:
                        logger.info(
                            'Progress: %s/%s (Created: %s, Updated: %s, Skipped: %s, Errors: %s)',
                            processed_count,
                            total_count,
                            created_count,
                            updated_count,
                            skipped_count,
                            error_count
                        )

                except Exception as exc:  # pylint: disable=broad-except
                    error_count += 1
                    logger.error(
                        'Error processing content %s: %s',
                        content.content_key,
                        exc,
                        exc_info=True
                    )

        # Final summary
        logger.info(
            'Translation Complete! Processed: %s | Created: %s | Updated: %s | Skipped: %s | Errors: %s',
            processed_count,
            created_count,
            updated_count,
            skipped_count,
            error_count
        )

    def _process_content(self, content, language_code, force, dry_run, all_content):
        """
        Process a single content metadata item.

        Args:
            content: ContentMetadata instance
            language_code: Target language code
            force: Whether to force re-translation
            dry_run: Whether to skip saving

        Returns:
            str: 'created', 'updated', or 'skipped'
        """
        # Optimization: Skip if content would not be indexed in Algolia
        if not all_content:
            should_index = True
            if content.content_type == COURSE:
                should_index = _should_index_course(content)
                # Additional check: skip if the advertised course run is "Archived"
                if should_index:
                    advertised_run = get_advertised_course_run(content.json_metadata)
                    if advertised_run and advertised_run.get('availability') == 'Archived':
                        logger.info(
                            f'Skipping {content.content_key} - advertised course run is Archived'
                        )
                        return 'skipped'
            elif content.content_type == PROGRAM:
                should_index = _should_index_program(content)

            if not should_index:
                logger.info(
                    f'Skipping {content.content_key} - not eligible for Algolia indexing'
                )
                return 'skipped'

        # Compute source hash
        source_hash = compute_source_hash(content)

        # Check if translation exists
        try:
            translation = ContentTranslation.objects.get(
                content_metadata=content,
                language_code=language_code
            )
            exists = True
        except ContentTranslation.DoesNotExist:
            translation = ContentTranslation(
                content_metadata=content,
                language_code=language_code
            )
            exists = False

        # Skip if translation exists, hash matches, and not forcing
        if exists and translation.source_hash == source_hash and not force:
            logger.debug(
                f'Skipping {content.content_key} - translation up to date'
            )
            return 'skipped'

        # Translate fields
        fields_to_translate = [
            'title', 'short_description', 'full_description', 'subtitle'
        ]

        translated_data = translate_object_fields(
            content.json_metadata,
            fields_to_translate
        )

        # Update translation fields
        translation.title = translated_data.get('title')
        translation.short_description = translated_data.get('short_description')
        translation.full_description = translated_data.get('full_description')
        translation.subtitle = translated_data.get('subtitle')
        translation.source_hash = source_hash

        # Save if not dry run
        if not dry_run:
            translation.save()

        result = 'created' if not exists else 'updated'
        logger.info(
            f'{result.capitalize()} translation for {content.content_key} ({language_code})'
        )

        return result
