"""
Celery tasks for incremental Algolia indexing.
"""
import logging

from celery import shared_task
from django.conf import settings

from enterprise_catalog.apps.api.tasks import (
    LoggedTaskWithRetry,
    _get_algolia_products_for_batch,
    _precalculate_content_mappings,
)
from enterprise_catalog.apps.api_client.algolia import AlgoliaSearchClient
from enterprise_catalog.apps.catalog.algolia_utils import (
    configure_algolia_index,
)
from enterprise_catalog.apps.catalog.constants import (
    COURSE,
    LEARNER_PATHWAY,
    PROGRAM,
)
from enterprise_catalog.apps.catalog.models import ContentMetadata
from enterprise_catalog.apps.search.models import ContentMetadataIndexingState


logger = logging.getLogger(__name__)

# Default batch size for indexing tasks
INDEXING_BATCH_SIZE = getattr(settings, 'ALGOLIA_INDEXING_BATCH_SIZE', 10)


def _get_algolia_client():
    """
    Get an initialized Algolia client.

    Returns:
        AlgoliaSearchClient instance, or None if initialization fails.
    """
    client = AlgoliaSearchClient()
    client.init_index()
    if not client.algolia_index:
        logger.error('Failed to initialize Algolia client.')
        return None
    return client


def _generate_algolia_objects_for_content(content_keys, content_type):
    """
    Generate Algolia objects for the given content keys.

    This reuses the existing Algolia object generation logic from the
    full reindex task but for a smaller batch of content.

    Args:
        content_keys: List of content keys to generate objects for.
        content_type: The content type (COURSE, PROGRAM, LEARNER_PATHWAY).

    Returns:
        List of Algolia objects ready for indexing.
    """
    if not content_keys:
        return []

    # Precalculate content mappings (program -> courses, pathway -> programs/courses)
    program_to_courses_mapping, pathway_to_programs_courses_mapping = _precalculate_content_mappings()

    context_accumulator = {
        'total_algolia_products_count': 0,
        'discarded_algolia_object_ids': {},
    }

    # Get the Algolia products for this batch
    algolia_objects = _get_algolia_products_for_batch(
        batch_num=0,
        content_keys_batch=content_keys,
        all_indexable_content_keys=set(content_keys),
        program_to_courses_mapping=program_to_courses_mapping,
        pathway_to_programs_courses_mapping=pathway_to_programs_courses_mapping,
        context_accumulator=context_accumulator,
        dry_run=False,
    )

    logger.info(
        'Generated %d Algolia objects for %d %s content keys.',
        len(algolia_objects),
        len(content_keys),
        content_type,
    )

    return algolia_objects


def _filter_content_keys_for_indexing(content_keys, content_type, force, results):
    """
    Filter content keys based on staleness.

    Args:
        content_keys: List of content keys to filter.
        content_type: The content type.
        force: If True, skip staleness check.
        results: Results dict to update with skip counts.

    Returns:
        List of content keys that need indexing.
    """
    content_keys_to_index = []
    for content_key in content_keys:
        try:
            content = ContentMetadata.objects.get(
                content_key=content_key,
                content_type=content_type,
            )
        except ContentMetadata.DoesNotExist:
            logger.warning(
                'ContentMetadata not found for content_key=%s, content_type=%s',
                content_key,
                content_type,
            )
            results['skipped'] += 1
            continue

        state = ContentMetadataIndexingState.get_or_create_for_content(content)

        # Record-level deduplication: skip if already indexed at current version
        if not force and state.last_indexed_at and state.last_indexed_at >= content.modified:
            logger.debug(
                'Skipping %s - already indexed at current version.',
                content_key,
            )
            results['skipped'] += 1
            continue

        content_keys_to_index.append(content_key)

    return content_keys_to_index


def _index_single_content_item(client, content_key, content_type, objects_by_content_key, index_name):
    """
    Index a single content item in Algolia.

    Args:
        client: The Algolia client.
        content_key: The content key to index.
        content_type: The content type.
        objects_by_content_key: Dict mapping content keys to Algolia objects.
        index_name: Optional custom index name.

    Raises:
        Exception: If indexing fails.
    """
    content = ContentMetadata.objects.get(
        content_key=content_key,
        content_type=content_type,
    )
    state = ContentMetadataIndexingState.get_or_create_for_content(content)

    # Get existing shards from Algolia
    existing_object_ids = client.get_object_ids_by_prefix(content_key, index_name)

    # Get new objects for this content
    new_objects = objects_by_content_key.get(content_key, [])
    new_object_ids = [obj['objectID'] for obj in new_objects]

    # Save new objects
    if new_objects:
        client.save_objects_batch(new_objects, index_name)

    # Delete orphaned shards (objects that exist but aren't in the new set)
    orphaned_ids = set(existing_object_ids) - set(new_object_ids)
    if orphaned_ids:
        client.delete_objects_batch(list(orphaned_ids), index_name)
        logger.info(
            'Deleted %d orphaned shards for %s',
            len(orphaned_ids),
            content_key,
        )

    # Update indexing state
    state.mark_as_indexed(algolia_object_ids=new_object_ids)

    logger.info(
        'Successfully indexed %s with %d Algolia objects.',
        content_key,
        len(new_objects),
    )


def _index_content_batch(
    content_keys,
    content_type,
    index_name=None,
    force=False,
):
    """
    Index a batch of content in Algolia.

    This is the core logic shared by all content-type specific tasks.

    Args:
        content_keys: List of content keys to index.
        content_type: The content type (COURSE, PROGRAM, LEARNER_PATHWAY).
        index_name: Optional custom index name for testing.
        force: If True, index regardless of staleness.

    Returns:
        dict with counts of indexed, skipped, and failed content.
    """
    results = {
        'indexed': 0,
        'skipped': 0,
        'failed': 0,
        'content_keys': content_keys,
    }

    if not content_keys:
        logger.info('No content keys provided for indexing.')
        return results

    # Get Algolia client
    client = _get_algolia_client()
    if not client:
        logger.error('Could not initialize Algolia client.')
        for content_key in content_keys:
            _mark_content_as_failed(content_key, 'Algolia client initialization failed')
        results['failed'] = len(content_keys)
        return results

    # Configure index settings if needed
    configure_algolia_index(client)

    # Filter content keys based on staleness (unless force=True)
    content_keys_to_index = _filter_content_keys_for_indexing(
        content_keys, content_type, force, results
    )

    if not content_keys_to_index:
        logger.info('No content keys need indexing after staleness check.')
        return results

    # Generate Algolia objects for the batch
    try:
        algolia_objects = _generate_algolia_objects_for_content(
            content_keys_to_index,
            content_type,
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.exception('Failed to generate Algolia objects: %s', exc)
        for content_key in content_keys_to_index:
            _mark_content_as_failed(content_key, f'Failed to generate Algolia objects: {exc}')
        results['failed'] = len(content_keys_to_index)
        return results

    if not algolia_objects:
        logger.warning('No Algolia objects generated for content keys: %s', content_keys_to_index)
        return results

    # Group objects by aggregation_key (content_key) for shard management
    objects_by_content_key = {}
    for obj in algolia_objects:
        aggregation_key = obj.get('aggregation_key')
        if aggregation_key:
            if aggregation_key not in objects_by_content_key:
                objects_by_content_key[aggregation_key] = []
            objects_by_content_key[aggregation_key].append(obj)

    # Index each content item
    for content_key in content_keys_to_index:
        try:
            _index_single_content_item(
                client, content_key, content_type, objects_by_content_key, index_name
            )
            results['indexed'] += 1
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.exception('Failed to index %s: %s', content_key, exc)
            _mark_content_as_failed(content_key, str(exc))
            results['failed'] += 1

    return results


def _mark_content_as_failed(content_key, reason):
    """
    Mark a content item as having failed indexing.

    Args:
        content_key: The content key that failed.
        reason: The failure reason.
    """
    try:
        content = ContentMetadata.objects.get(content_key=content_key)
        state = ContentMetadataIndexingState.get_or_create_for_content(content)
        state.mark_as_failed(reason)
    except ContentMetadata.DoesNotExist:
        logger.warning(
            'Cannot mark %s as failed - ContentMetadata not found.',
            content_key,
        )


@shared_task(base=LoggedTaskWithRetry, bind=True, max_retries=1)
def index_courses_batch_in_algolia(
    self,  # pylint: disable=unused-argument
    content_keys,
    index_name=None,
    force=False,
):
    """
    Index a batch of courses in Algolia.

    Args:
        content_keys: List of course content keys to index (max 10 recommended).
        index_name: Optional custom index name for testing against a v2 index.
        force: If True, index regardless of whether the content appears stale.

    Returns:
        dict with counts of indexed, skipped, and failed courses.
    """
    logger.info(
        'index_courses_batch_in_algolia called with %d content keys, '
        'index_name=%s, force=%s',
        len(content_keys),
        index_name,
        force,
    )
    return _index_content_batch(
        content_keys=content_keys,
        content_type=COURSE,
        index_name=index_name,
        force=force,
    )


@shared_task(base=LoggedTaskWithRetry, bind=True, max_retries=1)
def index_programs_batch_in_algolia(
    self,  # pylint: disable=unused-argument
    content_keys,
    index_name=None,
    force=False,
):
    """
    Index a batch of programs in Algolia.

    Args:
        content_keys: List of program content keys (UUIDs) to index (max 10 recommended).
        index_name: Optional custom index name for testing against a v2 index.
        force: If True, index regardless of whether the content appears stale.

    Returns:
        dict with counts of indexed, skipped, and failed programs.
    """
    logger.info(
        'index_programs_batch_in_algolia called with %d content keys, '
        'index_name=%s, force=%s',
        len(content_keys),
        index_name,
        force,
    )
    return _index_content_batch(
        content_keys=content_keys,
        content_type=PROGRAM,
        index_name=index_name,
        force=force,
    )


@shared_task(base=LoggedTaskWithRetry, bind=True, max_retries=1)
def index_pathways_batch_in_algolia(
    self,  # pylint: disable=unused-argument
    content_keys,
    index_name=None,
    force=False,
):
    """
    Index a batch of learner pathways in Algolia.

    Args:
        content_keys: List of pathway content keys to index (max 10 recommended).
        index_name: Optional custom index name for testing against a v2 index.
        force: If True, index regardless of whether the content appears stale.

    Returns:
        dict with counts of indexed, skipped, and failed pathways.
    """
    logger.info(
        'index_pathways_batch_in_algolia called with %d content keys, '
        'index_name=%s, force=%s',
        len(content_keys),
        index_name,
        force,
    )
    return _index_content_batch(
        content_keys=content_keys,
        content_type=LEARNER_PATHWAY,
        index_name=index_name,
        force=force,
    )
