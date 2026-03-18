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


def _get_stale_content_keys(content_type, force=False, include_failed=True):
    """
    Get content keys that need to be indexed.

    Args:
        content_type: The content type to query for (COURSE, PROGRAM, LEARNER_PATHWAY).
        force: If True, return all content of this type regardless of staleness.
        include_failed: If True, include previously failed records for retry.

    Returns:
        List of content keys that need indexing.
    """
    content_keys = []

    if force:
        # Return all content of this type
        content_qs = ContentMetadata.objects.filter(content_type=content_type)
        content_keys = list(content_qs.values_list('content_key', flat=True))
        logger.info(
            'Force mode: returning all %d %s content keys.',
            len(content_keys),
            content_type,
        )
        return content_keys

    # Get stale content: never indexed, or modified since last indexed
    content_qs = ContentMetadata.objects.filter(content_type=content_type)

    for content in content_qs:
        try:
            state = content.indexing_state
        except ContentMetadataIndexingState.DoesNotExist:
            # Never indexed
            content_keys.append(content.content_key)
            continue

        # Check if stale (modified since last indexed)
        if state.last_indexed_at is None or content.modified > state.last_indexed_at:
            content_keys.append(content.content_key)
            continue

        # Include failed records for retry
        if include_failed and state.last_failure_at is not None:
            content_keys.append(content.content_key)

    logger.info(
        'Found %d stale/failed %s content keys.',
        len(content_keys),
        content_type,
    )
    return content_keys


def _batch_content_keys(content_keys, batch_size=None):
    """
    Split content keys into batches.

    Args:
        content_keys: List of content keys.
        batch_size: Size of each batch. Defaults to INDEXING_BATCH_SIZE.

    Returns:
        Generator yielding batches of content keys.
    """
    if batch_size is None:
        batch_size = INDEXING_BATCH_SIZE

    for i in range(0, len(content_keys), batch_size):
        yield content_keys[i:i + batch_size]


@shared_task(base=LoggedTaskWithRetry, bind=True, max_retries=1)
def dispatch_algolia_indexing(
    self,  # pylint: disable=unused-argument
    content_type=None,
    force=False,
    index_name=None,
    dry_run=False,
):
    """
    Dispatch batch indexing tasks for stale content.

    This task queries for stale/failed content and dispatches batch indexing
    tasks for each content type. It can be run:
    - After update_content_metadata with force=True to reindex all changed content
    - On a schedule (cron) to catch stragglers and retry failures

    Args:
        content_type: Optional content type to limit dispatch to (COURSE, PROGRAM,
            LEARNER_PATHWAY). If not specified, dispatches for all types.
        force: If True, dispatch tasks for all content regardless of staleness.
        index_name: Optional custom index name for testing against a v2 index.
        dry_run: If True, log what would be dispatched but don't actually dispatch.

    Returns:
        dict with counts of dispatched tasks per content type.
    """
    content_types_to_process = []
    if content_type:
        content_types_to_process = [content_type]
    else:
        # Process in dependency order: courses first, then programs, then pathways
        content_types_to_process = [COURSE, PROGRAM, LEARNER_PATHWAY]

    results = {
        'dispatched_tasks': 0,
        'content_types': {},
    }

    task_mapping = {
        COURSE: index_courses_batch_in_algolia,
        PROGRAM: index_programs_batch_in_algolia,
        LEARNER_PATHWAY: index_pathways_batch_in_algolia,
    }

    for ctype in content_types_to_process:
        content_keys = _get_stale_content_keys(ctype, force=force)
        batches = list(_batch_content_keys(content_keys))

        results['content_types'][ctype] = {
            'total_content_keys': len(content_keys),
            'batches_dispatched': 0,
        }

        if not batches:
            logger.info('No %s content keys to index.', ctype)
            continue

        task_func = task_mapping.get(ctype)
        if not task_func:
            logger.error('No task function found for content type: %s', ctype)
            continue

        for batch_keys in batches:
            if dry_run:
                logger.info(
                    '[DRY RUN] Would dispatch %s task for %d content keys: %s',
                    ctype,
                    len(batch_keys),
                    batch_keys[:3],  # Log first 3 keys for reference
                )
            else:
                task_func.delay(
                    content_keys=batch_keys,
                    index_name=index_name,
                    force=force,
                )
                logger.info(
                    'Dispatched %s indexing task for %d content keys.',
                    ctype,
                    len(batch_keys),
                )

            results['content_types'][ctype]['batches_dispatched'] += 1
            results['dispatched_tasks'] += 1

    logger.info(
        'dispatch_algolia_indexing complete. Results: %s',
        results,
    )
    return results


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
