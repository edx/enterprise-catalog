"""
Integration check for ALLOWED_INDEX_NAMES / generate_secured_api_key.

Generates a secured API key via the Django client (respecting your private
settings) and then uses it to search each index in ALLOWED_INDEX_NAMES.
Optionally confirms the key is blocked on an out-of-scope index.

Requires a running Django environment with real Algolia credentials.

Usage:
    DJANGO_SETTINGS_MODULE=enterprise_catalog.settings.private \\
        python scripts/check_secured_api_key.py

Optional env vars:
    CATALOG_QUERY_UUIDS   comma-separated UUIDs to include in the key filter
                          (defaults to empty, which returns all hits in the index)
    BLOCKED_INDEX_NAME    index name expected to be blocked by the key;
                          omit to skip the block check
"""
import os
import sys

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
django.setup()

from algoliasearch.search_client import SearchClient  # noqa: E402
from django.conf import settings  # noqa: E402

from enterprise_catalog.apps.api_client.algolia import AlgoliaSearchClient  # noqa: E402


def ok(label, detail=''):
    print(f'[OK  ] {label}' + (f': {detail}' if detail else ''))


def fail(label, detail=''):
    print(f'[FAIL] {label}' + (f': {detail}' if detail else ''))


def main():
    algolia = settings.ALGOLIA
    print('ALGOLIA settings:')
    for key in ('INDEX_NAME', 'REPLICA_INDEX_NAME', 'INCREMENTAL_INDEX_NAME', 'ALLOWED_INDEX_NAMES'):
        print(f'  {key}: {algolia.get(key)!r}')
    print()

    catalog_query_uuids = [
        u.strip() for u in os.environ.get('CATALOG_QUERY_UUIDS', '').split(',') if u.strip()
    ]

    client = AlgoliaSearchClient()
    client.init_index()

    result = client.generate_secured_api_key(
        user_id='check-secured-api-key-script',
        enterprise_catalog_query_uuids=catalog_query_uuids,
    )
    secured_key = result['secured_api_key']

    allowed = algolia.get('ALLOWED_INDEX_NAMES') or [
        algolia.get('INDEX_NAME'),
        algolia.get('REPLICA_INDEX_NAME'),
    ]
    allowed = [n for n in allowed if n]

    search_client = SearchClient.create(algolia.get('APPLICATION_ID'), secured_key)

    for index_name in allowed:
        try:
            hits = search_client.init_index(index_name).search('', {'hitsPerPage': 1})
            ok(f'search on {index_name!r}', f'{hits["nbHits"]} hits')
        except Exception as exc:
            fail(f'search on {index_name!r}', str(exc))

    blocked = os.environ.get('BLOCKED_INDEX_NAME')
    if blocked:
        print()
        try:
            search_client.init_index(blocked).search('', {'hitsPerPage': 1})
            fail(f'block check on {blocked!r}', 'expected IndexNotAllowedError but search succeeded')
        except Exception as exc:
            ok(f'block check on {blocked!r}', f'correctly blocked ({type(exc).__name__})')


if __name__ == '__main__':
    main()
