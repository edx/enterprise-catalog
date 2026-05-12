"""
Smoke test for the new AlgoliaSearchClient batch methods.

Exercises ``save_objects_batch``, ``get_object_ids_for_aggregation_key``,
``get_aggregation_keys_for_catalog_query``, and ``delete_objects_batch`` against
a sandbox Algolia index. Reads sandbox credentials from ``scripts/.env``.

Usage (from the project root, inside the app container)::

    cat scripts/algolia_batch_smoke.py | ./manage.py shell

Or via docker from the host::

    docker exec -i enterprise.catalog.app bash -c \\
        'cd /edx/app/enterprise-catalog && ./manage.py shell' \\
        < scripts/algolia_batch_smoke.py

The script writes 3 synthetic objects to the sandbox index, reads them back
via every new method, and deletes them. Safe to re-run — each invocation uses
a fresh aggregation_key and cleans up after itself.

Assumes the sandbox index has ``enterprise_catalog_query_uuids`` declared as
an attribute for faceting (required for the ``get_aggregation_keys_for_catalog_query``
scenario). If you've been running the existing legacy reindex against this
sandbox, that's already configured.
"""
# pragma: no cover

import os
import sys
import time
import uuid
from pathlib import Path

from django.conf import settings


# ----- 1. Load sandbox config from scripts/.env --------------------------

def _load_dotenv(path):
    """
    Tiny .env parser — avoids adding a python-dotenv dependency for ~10 lines
    of work. Skips comments and blank lines; values are taken verbatim (no
    quote stripping, no variable expansion).
    """
    if not path.exists():
        sys.exit(f'Missing {path}; copy the example and fill in sandbox creds.')
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(Path('scripts/.env'))


# ----- 2. Override settings.ALGOLIA so the client picks up sandbox creds --

required = ['ALGOLIA_APPLICATION_ID', 'ALGOLIA_ADMIN_API_KEY', 'ALGOLIA_INDEX_NAME']
missing = [k for k in required if not os.environ.get(k)]
if missing:
    sys.exit(f'Missing required env vars: {", ".join(missing)}')

settings.ALGOLIA = {
    'APPLICATION_ID': os.environ['ALGOLIA_APPLICATION_ID'],
    'API_KEY': os.environ['ALGOLIA_ADMIN_API_KEY'],
    'INDEX_NAME': os.environ['ALGOLIA_INDEX_NAME'],
    'REPLICA_INDEX_NAME': os.environ.get('ALGOLIA_REPLICA_INDEX_NAME', ''),
    'SEARCH_API_KEY': os.environ.get('ALGOLIA_SEARCH_API_KEY', ''),
}

# Guardrail: refuse to run against anything that looks like a production index.
if 'prod' in settings.ALGOLIA['INDEX_NAME'].lower():
    sys.exit(f'Refusing to run against {settings.ALGOLIA["INDEX_NAME"]!r}; '
             f'index name contains "prod".')


# ----- 3. Exercise the new methods ---------------------------------------

from enterprise_catalog.apps.api_client.algolia import AlgoliaSearchClient  # noqa: E402

CATALOG_QUERY_UUID = str(uuid.uuid4())
AGGREGATION_KEY = f'smoke-test-{uuid.uuid4().hex[:8]}'


def _make_obj(shard_idx):
    return {
        'objectID': f'{AGGREGATION_KEY}-catalog-query-uuids-{shard_idx}',
        'aggregation_key': AGGREGATION_KEY,
        'enterprise_catalog_query_uuids': [CATALOG_QUERY_UUID],
        'content_type': 'course',
        'title': f'Smoke test object {shard_idx}',
    }


def _step(label, fn):
    print(f'  {label} ... ', end='', flush=True)
    try:
        result = fn()
        print('OK')
        return result
    except Exception as exc:
        print(f'FAIL: {type(exc).__name__}: {exc}')
        raise


print('Algolia batch methods smoke test')
print(f'  Index:           {settings.ALGOLIA["INDEX_NAME"]}')
print(f'  Aggregation key: {AGGREGATION_KEY}')
print(f'  Catalog query:   {CATALOG_QUERY_UUID}')
print()

client = AlgoliaSearchClient()
client.init_index()

objects = [_make_obj(i) for i in range(3)]
object_ids = [obj['objectID'] for obj in objects]

try:
    _step(
        'save_objects_batch (3 records)',
        lambda: client.save_objects_batch(objects),
    )
    # save_objects_batch is fire-and-forget per ADR 0012; sleep briefly to let
    # Algolia publish before asserting searchability.
    time.sleep(2)

    found_ids = _step(
        'get_object_ids_for_aggregation_key',
        lambda: client.get_object_ids_for_aggregation_key(AGGREGATION_KEY),
    )
    assert set(found_ids) == set(object_ids), \
        f'expected {sorted(object_ids)}, got {sorted(found_ids)}'
    print(f'    -> {len(found_ids)} objectIDs returned')

    found_keys = _step(
        'get_aggregation_keys_for_catalog_query',
        lambda: client.get_aggregation_keys_for_catalog_query(CATALOG_QUERY_UUID),
    )
    assert AGGREGATION_KEY in found_keys, \
        f'{AGGREGATION_KEY} not in {sorted(found_keys)}'
    print(f'    -> {len(found_keys)} content keys returned (incl. ours)')

    # v2-targeting plumbing: pass the same index as alternate; should behave
    # identically and exercise the _get_index init_index() branch.
    alt_found = _step(
        f'get_object_ids_for_aggregation_key (index_name={settings.ALGOLIA["INDEX_NAME"]!r})',
        lambda: client.get_object_ids_for_aggregation_key(
            AGGREGATION_KEY, index_name=settings.ALGOLIA['INDEX_NAME'],
        ),
    )
    assert set(alt_found) == set(object_ids)

finally:
    _step(
        'delete_objects_batch (cleanup)',
        lambda: client.delete_objects_batch(object_ids),
    )
    time.sleep(2)

    leftover = client.get_object_ids_for_aggregation_key(AGGREGATION_KEY)
    if leftover:
        print(f'  WARNING: {len(leftover)} objects remain after cleanup: {leftover}')
    else:
        print('  cleanup verified — 0 objects remain.')

print()
print('All scenarios passed.')
