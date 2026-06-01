"""
Fixture loader for Phase 4a/4b Algolia integration tests.

Idempotently loads and cleans up the test fixture set.
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).resolve().parent.parent / 'fixtures' / 'algolia_reindexing.json'

FIXTURE_CONTENT_KEYS = [
    'edX+DemoX',
    'edX+E2E-101',
    'IBM+CAD0210EN',
    '96b52724-0a92-44e7-a856-dbbfbc696a6a',
]
FIXTURE_CATALOG_QUERY_UUIDS = [
    'aaaaaaaa-1111-1111-1111-000000000001',
    'aaaaaaaa-1111-1111-1111-000000000002',
    'aaaaaaaa-1111-1111-1111-000000000003',
]
FIXTURE_ENTERPRISE_CATALOG_UUIDS = [
    'bbbbbbbb-1111-1111-1111-000000000001',
    'bbbbbbbb-1111-1111-1111-000000000002',
    'bbbbbbbb-1111-1111-1111-000000000003',
]


def load_fixtures():
    """
    Idempotently load all fixture data. Returns (content_map, cq_map, ec_map).
    """
    # Imports inside function to avoid loading Django before settings are configured
    from enterprise_catalog.apps.catalog.models import (
        CatalogQuery, ContentMetadata, EnterpriseCatalog,
    )
    from enterprise_catalog.apps.search.indexing_mappings import invalidate_indexing_mappings_cache

    cleanup_db_fixtures()

    data = json.loads(FIXTURE_PATH.read_text())

    # Create/update ContentMetadata records (keyed by content_key, which is unique)
    content_map = {}
    for spec in data['content_metadata']:
        cm, _ = ContentMetadata.objects.update_or_create(
            content_key=spec['content_key'],
            defaults=dict(
                content_uuid=spec['content_uuid'],
                content_type=spec['content_type'],
                parent_content_key=spec.get('parent_content_key'),
                _json_metadata=spec['_json_metadata'],
            ),
        )
        content_map[spec['content_key']] = cm

    # Create/update CatalogQuery records (keyed by uuid, which is unique).
    # Each fixture CQ has a distinct content_filter (and therefore a distinct
    # content_filter_hash) to satisfy the unique constraint on that column.
    cq_map = {}
    for spec in data['catalog_queries']:
        cq, _ = CatalogQuery.objects.update_or_create(
            uuid=spec['uuid'],
            defaults=dict(
                title=spec['title'],
                content_filter=spec.get('content_filter', {}),
            ),
        )
        cq_map[spec['uuid']] = cq

    # Create/update EnterpriseCatalog records (keyed by uuid)
    ec_map = {}
    for spec in data['enterprise_catalogs']:
        cq = cq_map[spec['catalog_query_uuid']]
        ec, _ = EnterpriseCatalog.objects.update_or_create(
            uuid=spec['uuid'],
            defaults=dict(
                enterprise_uuid=spec['enterprise_uuid'],
                enterprise_name=spec['enterprise_name'],
                title=spec['title'],
                catalog_query=cq,
            ),
        )
        ec_map[spec['uuid']] = ec

    # Set catalog_queries M2M
    assoc = data['associations']
    for content_key, cq_uuids in assoc['content_catalog_queries'].items():
        cm = content_map[content_key]
        for cq_uuid in cq_uuids:
            cm.catalog_queries.add(cq_map[cq_uuid])

    # Set program → course associated_content_metadata M2M
    for program_key, course_keys in assoc['program_courses'].items():
        program_cm = content_map[program_key]
        for course_key in course_keys:
            program_cm.associated_content_metadata.add(content_map[course_key])

    invalidate_indexing_mappings_cache()
    logger.info('Loaded %d fixture content records', len(content_map))
    return content_map, cq_map, ec_map


def cleanup_db_fixtures():
    """Delete all fixture records from the DB."""
    from enterprise_catalog.apps.catalog.models import (
        CatalogQuery, ContentMetadata, EnterpriseCatalog,
    )
    # Clear M2M associations first to avoid FK constraint issues
    for cm in ContentMetadata.objects.filter(content_key__in=FIXTURE_CONTENT_KEYS):
        cm.catalog_queries.clear()
        cm.associated_content_metadata.clear()
    EnterpriseCatalog.objects.filter(uuid__in=FIXTURE_ENTERPRISE_CATALOG_UUIDS).delete()
    ContentMetadata.objects.filter(content_key__in=FIXTURE_CONTENT_KEYS).delete()
    CatalogQuery.objects.filter(uuid__in=FIXTURE_CATALOG_QUERY_UUIDS).delete()


def cleanup_algolia_objects(algolia_index):
    """
    Best-effort: delete Algolia objects written for fixture content keys.
    Reads algolia_object_ids from ContentMetadataIndexingState and deletes
    them via the raw Algolia index object (not AlgoliaSearchClient wrapper).

    ``algolia_index`` is the object returned by ``SearchClient.init_index()``.
    """
    from enterprise_catalog.apps.search.models import ContentMetadataIndexingState
    from enterprise_catalog.apps.catalog.models import ContentMetadata

    all_object_ids = []
    for cm in ContentMetadata.objects.filter(content_key__in=FIXTURE_CONTENT_KEYS):
        try:
            state = cm.indexing_state
            if state.algolia_object_ids:
                all_object_ids.extend(state.algolia_object_ids)
        except ContentMetadataIndexingState.DoesNotExist:
            pass

    if all_object_ids:
        try:
            algolia_index.delete_objects(all_object_ids)
            logger.info('Deleted %d Algolia objects for fixture content', len(all_object_ids))
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning('Could not delete Algolia objects (best-effort): %s', exc)
