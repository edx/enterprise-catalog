"""
Validate parity between two Algolia indices (v1 vs v2).

Two subcommands:

  fetch    — Hit the Algolia API, save raw results to scripts/data/algolia_validation/.
             Expensive (browses the full index); safe to skip if cached data exists.

  analyze  — Read saved data and print analysis. No API calls; fast to re-run.

You can chain them:
  python scripts/validate_algolia_indices.py fetch analyze --v1 enterprise_catalog --v2 enterprise_catalog_v2

Credentials are read from scripts/.env (or the file given by --env-file).
Required vars: ALGOLIA_APPLICATION_ID and ALGOLIA_ADMIN_API_KEY (or ALGOLIA_API_KEY).
You can also export them in your shell to skip the env file.

Run from the project root, inside the app container:

  docker exec -it enterprise.catalog.app bash -c \\
    'cd /edx/app/enterprise-catalog && \\
     python scripts/validate_algolia_indices.py fetch analyze \\
       --v1 enterprise_catalog --v2 enterprise_catalog_v2'
"""
# pragma: no cover

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    from algoliasearch.search_client import SearchClient
except ImportError:
    sys.exit(
        'algoliasearch package not found.\n'
        'Run inside the app container, or: pip install algoliasearch==3.*'
    )

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR / 'data' / 'algolia_validation'

# ---------------------------------------------------------------------------
# Facets to compare — chosen for signal value; excludes high-cardinality UUID
# fields that would produce thousands of facet buckets.
# ---------------------------------------------------------------------------

FACET_FIELDS = [
    'content_type',
    'language',
    'level_type',
    'course_type',
    'program_type',
    'learning_type',
    'learning_type_v2',
    'availability',
]

# ---------------------------------------------------------------------------
# .env loader (no python-dotenv dependency)
# ---------------------------------------------------------------------------

def _load_dotenv(path):
    path = Path(path)
    if not path.exists():
        return  # silently skip; caller checks that required vars are set
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        os.environ.setdefault(key.strip(), value.strip())


def _algolia_client():
    app_id = os.environ.get('ALGOLIA_APPLICATION_ID', '').strip()
    api_key = (
        os.environ.get('ALGOLIA_ADMIN_API_KEY', '').strip()
        or os.environ.get('ALGOLIA_API_KEY', '').strip()
    )
    if not app_id or not api_key:
        sys.exit(
            'Missing Algolia credentials.\n'
            'Set ALGOLIA_APPLICATION_ID and ALGOLIA_ADMIN_API_KEY in scripts/.env '
            'or export them in your shell.'
        )
    return SearchClient.create(app_id, api_key)


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def fetch_facets(index, facet_fields):
    """
    Returns a dict with total hit count, facet value distributions, and a
    fetch timestamp. Uses hitsPerPage=0 so no record data is transferred.
    """
    result = index.search('', {
        'facets': facet_fields,
        'hitsPerPage': 0,
    })
    return {
        'nb_hits': result.get('nbHits', 0),
        'facets': result.get('facets', {}),
        'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }


def fetch_object_ids(index):
    """
    Streams all objectIDs via the browse API (attributesToRetrieve=['objectID']
    means no record payload is transferred — just the IDs).

    Returns a dict with count, the ID list, and a fetch timestamp.
    ~371k IDs × ~40 bytes each ≈ 15 MB on disk.
    """
    ids = []
    for hit in index.browse_objects({'attributesToRetrieve': ['objectID']}):
        ids.append(hit['objectID'])
        if len(ids) % 25_000 == 0:
            print(f'    ... {len(ids):,} objectIDs fetched', flush=True)
    return {
        'count': len(ids),
        'object_ids': ids,
        'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }


# ---------------------------------------------------------------------------
# Subcommand: fetch
# ---------------------------------------------------------------------------

def cmd_fetch(args):
    _load_dotenv(args.env_file)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    client = _algolia_client()

    for label, index_name in [('v1', args.v1), ('v2', args.v2)]:
        print(f'\n[{label}] {index_name}')
        index = client.init_index(index_name)

        print('  Fetching facets...')
        facets = fetch_facets(index, FACET_FIELDS)
        facet_path = DATA_DIR / f'{label}_facets.json'
        facet_path.write_text(json.dumps(facets, indent=2))
        print(f'  Saved → {facet_path}  ({facets["nb_hits"]:,} hits)')

        print('  Fetching objectIDs (browse — may take a minute)...')
        ids_data = fetch_object_ids(index)
        ids_path = DATA_DIR / f'{label}_object_ids.json'
        ids_path.write_text(json.dumps(ids_data))
        size_kb = ids_path.stat().st_size // 1024
        print(f'  Saved → {ids_path}  ({ids_data["count"]:,} IDs, {size_kb:,} KB)')

    print()


# ---------------------------------------------------------------------------
# Subcommand: analyze
# ---------------------------------------------------------------------------

def cmd_analyze():
    for label in ('v1', 'v2'):
        for kind in ('facets', 'object_ids'):
            p = DATA_DIR / f'{label}_{kind}.json'
            if not p.exists():
                sys.exit(
                    f'Missing {p}\n'
                    'Run `fetch --v1 <name> --v2 <name>` first.'
                )

    v1_facets = json.loads((DATA_DIR / 'v1_facets.json').read_text())
    v2_facets = json.loads((DATA_DIR / 'v2_facets.json').read_text())
    v1_ids_data = json.loads((DATA_DIR / 'v1_object_ids.json').read_text())
    v2_ids_data = json.loads((DATA_DIR / 'v2_object_ids.json').read_text())

    _print_facet_analysis(v1_facets, v2_facets)
    _print_id_diff_analysis(v1_ids_data, v2_ids_data)


def _print_facet_analysis(v1, v2):
    print()
    print('=' * 72)
    print('FACET ANALYSIS')
    print('=' * 72)

    v1_total = v1['nb_hits']
    v2_total = v2['nb_hits']
    delta = v2_total - v1_total
    pct = (delta / v1_total * 100) if v1_total else 0.0

    print(f'{"":35s} {"v1":>10} {"v2":>10} {"delta":>8} {"delta%":>7}')
    print('-' * 72)
    print(f'{"TOTAL RECORDS":35s} {v1_total:>10,} {v2_total:>10,} {delta:>+8,} {pct:>+6.1f}%')
    print()

    for field in FACET_FIELDS:
        v1_field = v1.get('facets', {}).get(field, {})
        v2_field = v2.get('facets', {}).get(field, {})
        all_values = sorted(set(v1_field) | set(v2_field))
        if not all_values:
            continue

        print(f'{field}:')
        for val in all_values:
            c1 = v1_field.get(val, 0)
            c2 = v2_field.get(val, 0)
            d = c2 - c1
            p = (d / c1 * 100) if c1 else float('inf')
            flag = '  <-- !!!' if abs(d) > 1000 else ''
            print(f'  {val:<33} {c1:>10,} {c2:>10,} {d:>+8,} {p:>+6.1f}%{flag}')
        print()

    print(f'v1 fetched at: {v1.get("fetched_at", "unknown")}')
    print(f'v2 fetched at: {v2.get("fetched_at", "unknown")}')


def _print_id_diff_analysis(v1_data, v2_data):
    print()
    print('=' * 72)
    print('OBJECT ID SET DIFFERENCE')
    print('=' * 72)

    v1_set = set(v1_data['object_ids'])
    v2_set = set(v2_data['object_ids'])

    only_in_v1 = sorted(v1_set - v2_set)
    only_in_v2 = sorted(v2_set - v1_set)
    in_both = v1_set & v2_set

    print(f'v1 total:        {len(v1_set):>10,}')
    print(f'v2 total:        {len(v2_set):>10,}')
    print(f'In both:         {len(in_both):>10,}')
    print(f'Only in v1:      {len(only_in_v1):>10,}  ← missing from v2')
    print(f'Only in v2:      {len(only_in_v2):>10,}  ← extra in v2 (not in v1)')

    SAMPLE = 25
    if only_in_v1:
        print(f'\nSample of records only in v1 (first {min(SAMPLE, len(only_in_v1))}):')
        for oid in only_in_v1[:SAMPLE]:
            print(f'  {oid}')
        if len(only_in_v1) > SAMPLE:
            print(f'  ... and {len(only_in_v1) - SAMPLE:,} more (see id_diff.json)')

    if only_in_v2:
        print(f'\nSample of records only in v2 (first {min(SAMPLE, len(only_in_v2))}):')
        for oid in only_in_v2[:SAMPLE]:
            print(f'  {oid}')
        if len(only_in_v2) > SAMPLE:
            print(f'  ... and {len(only_in_v2) - SAMPLE:,} more (see id_diff.json)')

    diff_path = DATA_DIR / 'id_diff.json'
    diff_path.write_text(json.dumps({
        'summary': {
            'v1_total': len(v1_set),
            'v2_total': len(v2_set),
            'in_both': len(in_both),
            'only_in_v1': len(only_in_v1),
            'only_in_v2': len(only_in_v2),
        },
        'only_in_v1': only_in_v1,
        'only_in_v2': only_in_v2,
    }, indent=2))
    print(f'\nFull ID diff saved → {diff_path}')
    print(f'\nv1 fetched at: {v1_data.get("fetched_at", "unknown")}')
    print(f'v2 fetched at: {v2_data.get("fetched_at", "unknown")}')
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
#
# Supports three invocation patterns:
#
#   fetch only:
#     python scripts/validate_algolia_indices.py fetch --v1 NAME --v2 NAME
#
#   analyze only (uses cached data from a prior fetch):
#     python scripts/validate_algolia_indices.py analyze
#
#   fetch then analyze in one shot:
#     python scripts/validate_algolia_indices.py fetch --v1 NAME --v2 NAME analyze
#
# Implemented by scanning sys.argv for the literal tokens 'fetch' and
# 'analyze' before handing the remainder to per-subcommand parsers. Standard
# argparse mutually-exclusive subcommands can't express "run both in order",
# and a dedicated sub-framework is overkill.

def _split_argv():
    """
    Returns (do_fetch, do_analyze, fetch_argv) by scanning sys.argv[1:].
    fetch_argv contains everything that isn't the literal tokens 'fetch' /
    'analyze', so the fetch sub-parser can handle --v1 / --v2 / --env-file.
    """
    tokens = sys.argv[1:]
    do_fetch = 'fetch' in tokens
    do_analyze = 'analyze' in tokens
    fetch_argv = [t for t in tokens if t not in ('fetch', 'analyze')]
    return do_fetch, do_analyze, fetch_argv


def main():
    do_fetch, do_analyze, fetch_argv = _split_argv()

    if not do_fetch and not do_analyze:
        print(__doc__)
        print('error: specify at least one subcommand: fetch, analyze, or both')
        sys.exit(1)

    fetch_args = None
    if do_fetch:
        fetch_parser = argparse.ArgumentParser(add_help=False)
        fetch_parser.add_argument('--v1', required=True)
        fetch_parser.add_argument('--v2', required=True)
        fetch_parser.add_argument('--env-file', default='scripts/.env')
        fetch_args, _ = fetch_parser.parse_known_args(fetch_argv)

    if do_fetch:
        cmd_fetch(fetch_args)
    if do_analyze:
        cmd_analyze()


if __name__ == '__main__':
    main()
