"""
Phase 3 smoke test — exercises the incremental indexing batch tasks against
existing dev data and a sandbox Algolia index.

Required env vars (in scripts/.env):
    ALGOLIA_APPLICATION_ID
    ALGOLIA_ADMIN_API_KEY
    ALGOLIA_INDEX_NAME
    ALGOLIA_REPLICA_INDEX_NAME
    SMOKE_CONTENT_KEY        — a ContentMetadata.content_key in the dev DB

Optional:
    SMOKE_CONTENT_TYPE       — 'course' | 'program' | 'learnerpathway'
                                (auto-detected from the DB if omitted)

Usage (must run inside the app container for DB access):

    cat scripts/algolia_phase3_smoke.py | ./manage.py shell

The script:
  1. Looks up the record and prints its CatalogQuery memberships, expected
     facet UUIDs, and indexability per the partition fns.
  2. Runs _index_content_batch synchronously against the sandbox index with
     force=True.
  3. Verifies the result dict, the state row, the round-trip via
     get_object_ids_for_aggregation_key, and that the union of the Algolia
     objects' enterprise_catalog_query_uuids is a superset of what the DB
     said the record should be in.

Persistence:
  Both the ContentMetadataIndexingState row and the Algolia shards are
  intentionally left in place after the run so they can be inspected in the
  Django admin and the Algolia dashboard. Re-running the script is safe —
  the task uses ``get_or_create_for_content`` to update the existing state
  row, and Algolia's upsert + the task's orphan detection keeps the index
  consistent with the latest run.
"""
# pragma: no cover

import os
import sys
import time
from pathlib import Path

from django.conf import settings


# ----- 1. Load sandbox config from scripts/.env --------------------------

def _load_dotenv(path):
    if not path.exists():
        sys.exit(f'Missing {path}; copy the example and fill in sandbox creds.')
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(Path('scripts/.env'))


# ----- 2. Validate env + override settings.ALGOLIA -----------------------

required = [
    'ALGOLIA_APPLICATION_ID',
    'ALGOLIA_ADMIN_API_KEY',
    'ALGOLIA_INDEX_NAME',
    'ALGOLIA_REPLICA_INDEX_NAME',
    'SMOKE_CONTENT_KEY',
]
missing = [k for k in required if not os.environ.get(k)]
if missing:
    sys.exit(f'Missing required env vars: {", ".join(missing)}')

settings.ALGOLIA = {
    'APPLICATION_ID': os.environ['ALGOLIA_APPLICATION_ID'],
    'API_KEY': os.environ['ALGOLIA_ADMIN_API_KEY'],
    'INDEX_NAME': os.environ['ALGOLIA_INDEX_NAME'],
    'REPLICA_INDEX_NAME': os.environ['ALGOLIA_REPLICA_INDEX_NAME'],
    'SEARCH_API_KEY': os.environ.get('ALGOLIA_SEARCH_API_KEY', ''),
}

if 'prod' in settings.ALGOLIA['INDEX_NAME'].lower():
    sys.exit(f'Refusing to run against {settings.ALGOLIA["INDEX_NAME"]!r}; '
             'index name contains "prod".')

CONTENT_KEY = os.environ['SMOKE_CONTENT_KEY']
CONTENT_TYPE_OVERRIDE = os.environ.get('SMOKE_CONTENT_TYPE')


# ----- 3. ORM pre-flight -------------------------------------------------

from enterprise_catalog.apps.api_client.algolia import (  # noqa: E402
    AlgoliaSearchClient,
)
from enterprise_catalog.apps.catalog.algolia_utils import (  # noqa: E402
    partition_course_keys_for_indexing,
    partition_program_keys_for_indexing,
)
from enterprise_catalog.apps.catalog.constants import (  # noqa: E402
    COURSE,
    LEARNER_PATHWAY,
    PROGRAM,
)
from enterprise_catalog.apps.catalog.models import ContentMetadata  # noqa: E402
from enterprise_catalog.apps.search.models import (  # noqa: E402
    ContentMetadataIndexingState,
)
from enterprise_catalog.apps.search.tasks import _index_content_batch  # noqa: E402


try:
    content = ContentMetadata.objects.get(content_key=CONTENT_KEY)
except ContentMetadata.DoesNotExist:
    sys.exit(f'No ContentMetadata found for content_key={CONTENT_KEY!r}')

content_type = CONTENT_TYPE_OVERRIDE or content.content_type
if content.content_type != content_type:
    sys.exit(f'Content has type {content.content_type!r}, but '
             f'SMOKE_CONTENT_TYPE={content_type!r}; aborting.')
if content_type not in (COURSE, PROGRAM, LEARNER_PATHWAY):
    sys.exit(f'Unsupported content_type={content_type!r}')

catalog_queries = list(content.catalog_queries.all())
expected_catalog_query_uuids = {str(cq.uuid) for cq in catalog_queries}

if content_type == COURSE:
    indexable, _ = partition_course_keys_for_indexing(
        ContentMetadata.objects.filter(pk=content.pk)
    )
    is_indexable = bool(indexable)
elif content_type == PROGRAM:
    indexable, _ = partition_program_keys_for_indexing(
        ContentMetadata.objects.filter(pk=content.pk)
    )
    is_indexable = bool(indexable)
else:  # LEARNER_PATHWAY — no partition fn; all pathways are indexable.
    is_indexable = True

print('Phase 3 incremental indexing smoke test')
print(f'  Index:                        {settings.ALGOLIA["INDEX_NAME"]}')
print(f'  Content key:                  {content.content_key}')
print(f'  Content type:                 {content_type}')
print(f'  Modified:                     {content.modified.isoformat()}')
print(f'  CatalogQuery memberships:     {len(catalog_queries)}')
print(f'  Indexable per partition fn:   {is_indexable}')
print()

if not is_indexable:
    sys.exit('Content is non-indexable per the partition function — pick a key '
             'that should be indexed for a meaningful smoke test.')
if not expected_catalog_query_uuids:
    sys.exit('Content has zero CatalogQuery memberships — facet verification '
             'would be meaningless. Pick a key with at least one membership.')


def _step(label, fn):
    print(f'  {label} ... ', end='', flush=True)
    try:
        result = fn()
        print('OK')
        return result
    except Exception as exc:
        print(f'FAIL: {type(exc).__name__}: {exc}')
        raise


# Build the aggregation_key the legacy generator uses: ``"{content_type}:{content_key}"``.
AGGREGATION_KEY = f'{content_type}:{CONTENT_KEY}'

client = AlgoliaSearchClient()
client.init_index()

# ----- 4. Run the task synchronously ------------------------------------

print()
print('Running _index_content_batch with force=True ...')
result = _index_content_batch(
    [CONTENT_KEY],
    content_type,
    index_name=settings.ALGOLIA['INDEX_NAME'],
    force=True,
)
print(f'  Result: {result}')
print()

state = ContentMetadataIndexingState.objects.get(content_metadata=content)
shards = list(state.algolia_object_ids or [])

assert result.indexed == 1, f'expected indexed=1, got {result}'
assert result.failed == 0, f'expected failed=0, got {result}'
assert result.skipped == 0, f'expected skipped=0, got {result}'
assert state.last_indexed_at is not None, 'state row last_indexed_at not set'
assert shards, 'state row has no algolia_object_ids'

print(f'  State row uuid={state.uuid}; {len(shards)} shards stored:')
for shard_id in shards:
    print(f'    - {shard_id}')


# ----- 5. Verify against Algolia + check facet superset ------------------
#
# The production task fires-and-forgets the Algolia save: throughput
# matters, the state row is the system of record, and ``IndexingResponse``
# is discarded inside ``_process_content_key``. The smoke test reads back
# immediately and would race the indexing pipeline, so we poll
# ``get_objects`` until every shard is visible (or fail loudly on
# timeout). This adapts to actual propagation time and surfaces post-
# acceptance failures that a fixed ``time.sleep`` would mask.

def _wait_for_shards_visible(algolia_client, object_ids, timeout=30, interval=0.5):
    deadline = time.monotonic() + timeout
    while True:
        results = algolia_client.algolia_index.get_objects(object_ids)['results']
        if all(r is not None for r in results):
            return
        if time.monotonic() >= deadline:
            missing = [oid for oid, r in zip(object_ids, results) if r is None]
            raise RuntimeError(
                f'timed out after {timeout}s waiting for {len(missing)} of '
                f'{len(object_ids)} shards to become visible: {missing}'
            )
        time.sleep(interval)


_step(
    f'wait for all {len(shards)} shards to become visible in Algolia',
    lambda: _wait_for_shards_visible(client, shards),
)

found_ids = _step(
    'get_object_ids_for_aggregation_key (state vs. Algolia)',
    lambda: client.get_object_ids_for_aggregation_key(AGGREGATION_KEY),
)
assert set(found_ids) == set(shards), (
    f'state vs. Algolia mismatch: state={sorted(shards)}, '
    f'algolia={sorted(found_ids)}'
)
print(f'    -> state and Algolia agree on {len(found_ids)} shards')

# Pull the full objects so we can inspect their facets. Algolia's
# get_objects returns {'results': [obj_or_None, ...]} in the same order
# as the requested object_ids.
fetched = client.algolia_index.get_objects(shards)['results']

facet_union = set()
for obj in fetched:
    if obj is None:
        continue
    for cq_uuid in (obj.get('enterprise_catalog_query_uuids') or []):
        facet_union.add(str(cq_uuid))

print(f'    -> facet union has {len(facet_union)} catalog_query_uuids '
      f'(DB expected {len(expected_catalog_query_uuids)})')

missing_in_facets = expected_catalog_query_uuids - facet_union
assert not missing_in_facets, (
    f'catalog_query_uuids missing from Algolia facets: {sorted(missing_in_facets)}'
)
print('    -> all DB-expected catalog_query_uuids are present in the facets')


# ----- 6. Leave the state row + Algolia shards in place for inspection ---

print()
print('Phase 3 smoke test passed.')
print(f'  State row left in place: ContentMetadataIndexingState({state.uuid})')
print(f'  Algolia shards left in place ({len(shards)}) on index '
      f'{settings.ALGOLIA["INDEX_NAME"]!r}.')
print('  Both will be inspectable in Django admin and the Algolia dashboard.')
print('  Re-run is idempotent: the task updates the existing state row and '
      'Algolia upserts.')
