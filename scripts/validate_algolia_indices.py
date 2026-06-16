"""
Validate parity between two Algolia indices (v1 vs v2).

Subcommands (may be combined on one invocation in any order):

  fetch              Hit the Algolia API, save raw results to scripts/data/algolia_validation/.
                     Expensive (browses the full index); safe to skip if cached data exists.

  analyze            Read saved facet/ID data and print analysis. No API calls; fast to re-run.

  fetch-level-type   Browse both indices and save objectID + level_type + content_type + title
                     for every record. Prerequisite for the level-type subcommand.

  level-type         Compare level_type values record-by-record and report mismatches.

  fetch-spot-check   Fetch --sample-size (default 50) full records from both indices.
                     Reads the existing object-ID data to pick IDs that appear in both.
                     Prerequisite for the spot-check subcommand.

  spot-check         Compare every field of the sampled records between v1 and v2.

Examples:

  # Fetch everything and run all analyses in one shot:
  python scripts/validate_algolia_indices.py \\
    fetch fetch-level-type fetch-spot-check \\
    analyze level-type spot-check \\
    --v1 enterprise_catalog --v2 enterprise_catalog_v2

  # Re-run analyses against cached data (no API calls):
  python scripts/validate_algolia_indices.py analyze level-type spot-check

  # Fetch a larger spot-check sample:
  python scripts/validate_algolia_indices.py fetch-spot-check spot-check \\
    --v1 enterprise_catalog --v2 enterprise_catalog_v2 --sample-size 200

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
import random
import sys
import time
from collections import defaultdict
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


def _browse_fields(index, fields):
    """
    Browse the entire index returning a dict keyed by objectID.
    Each value is a dict of the requested fields (None for missing).
    """
    records = {}
    for hit in index.browse_objects({'attributesToRetrieve': fields}):
        oid = hit['objectID']
        records[oid] = {f: hit.get(f) for f in fields if f != 'objectID'}
        if len(records) % 25_000 == 0:
            print(f'    ... {len(records):,} records fetched', flush=True)
    return records


def _batch_get_objects(index, object_ids, batch_size=500):
    """Fetch full records for object_ids in batches via getObjects."""
    records = []
    for i in range(0, len(object_ids), batch_size):
        batch = object_ids[i:i + batch_size]
        result = index.get_objects(batch)
        records.extend(r for r in result.get('results', []) if r is not None)
    return records


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
# Subcommand: fetch-level-type
# ---------------------------------------------------------------------------

def cmd_fetch_level_type(args):
    """
    Browse both indices, pulling objectID + level_type + content_type + title
    for every record. Stores a dict keyed by objectID for fast comparison.
    Expect ~2-3 minutes per index (~370k records).
    """
    _load_dotenv(args.env_file)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    client = _algolia_client()

    for label, index_name in [('v1', args.v1), ('v2', args.v2)]:
        print(f'\n[{label}] {index_name} — browsing level_type fields...')
        index = client.init_index(index_name)
        records = _browse_fields(index, ['objectID', 'level_type', 'content_type', 'title'])
        path = DATA_DIR / f'{label}_level_type.json'
        path.write_text(json.dumps({
            'count': len(records),
            'records': records,
            'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }))
        size_kb = path.stat().st_size // 1024
        print(f'  Saved → {path}  ({len(records):,} records, {size_kb:,} KB)')

    print()


# ---------------------------------------------------------------------------
# Subcommand: fetch-spot-check
# ---------------------------------------------------------------------------

def cmd_fetch_spot_check(args):
    """
    Pull a stratified random sample of full records from both indices.
    Stratifies across content_type by sampling proportionally from each
    type found in the level_type data (if available) or falling back to
    a flat random sample from the ID intersection.
    """
    _load_dotenv(args.env_file)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    sample_size = args.sample_size

    # Prefer stratified sample using level_type data (has content_type per ID).
    lt_path_v1 = DATA_DIR / 'v1_level_type.json'
    ids_path_v1 = DATA_DIR / 'v1_object_ids.json'
    ids_path_v2 = DATA_DIR / 'v2_object_ids.json'

    if lt_path_v1.exists():
        lt_data = json.loads(lt_path_v1.read_text())
        lt_path_v2 = DATA_DIR / 'v2_level_type.json'
        in_both_set = set(lt_data['records'])
        if lt_path_v2.exists():
            in_both_set &= set(json.loads(lt_path_v2.read_text())['records'])
        # Group by content_type
        by_type = defaultdict(list)
        for oid, rec in lt_data['records'].items():
            if oid in in_both_set:
                by_type[rec.get('content_type') or '_unknown'].append(oid)
        # Proportional allocation
        total_in_both = len(in_both_set)
        sample_ids = []
        for ctype, ids in sorted(by_type.items()):
            n = max(1, round(sample_size * len(ids) / total_in_both))
            random.seed(42)
            sample_ids.extend(random.sample(ids, min(n, len(ids))))
        # Trim to requested size
        random.seed(42)
        if len(sample_ids) > sample_size:
            sample_ids = random.sample(sample_ids, sample_size)
        print(f'Stratified sample: {len(sample_ids)} records across {len(by_type)} content types')
        for ctype, ids in sorted(by_type.items()):
            count = sum(1 for oid in sample_ids if lt_data['records'].get(oid, {}).get('content_type') == ctype)
            print(f'  {ctype}: {count}')
    elif ids_path_v1.exists() and ids_path_v2.exists():
        v1_ids = set(json.loads(ids_path_v1.read_text())['object_ids'])
        v2_ids = set(json.loads(ids_path_v2.read_text())['object_ids'])
        in_both = sorted(v1_ids & v2_ids)
        random.seed(42)
        sample_ids = random.sample(in_both, min(sample_size, len(in_both)))
        print(f'Flat random sample: {len(sample_ids)} records (no content_type stratification)')
    else:
        sys.exit(
            'No object-ID or level-type data found.\n'
            'Run `fetch --v1 <name> --v2 <name>` or `fetch-level-type` first.'
        )

    client = _algolia_client()
    for label, index_name in [('v1', args.v1), ('v2', args.v2)]:
        print(f'\n[{label}] {index_name} — fetching {len(sample_ids)} full records...')
        index = client.init_index(index_name)
        records = _batch_get_objects(index, sample_ids)
        path = DATA_DIR / f'{label}_sample.json'
        path.write_text(json.dumps({
            'sample_size': len(sample_ids),
            'records': records,
            'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }, indent=2))
        print(f'  Saved → {path}  ({len(records)} records)')

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


# ---------------------------------------------------------------------------
# Subcommand: level-type
# ---------------------------------------------------------------------------

def cmd_level_type():
    """
    Compare level_type values record-by-record between v1 and v2.
    Reports a breakdown of (v1_value → v2_value) transition counts with
    samples, then saves the full mismatch list to level_type_mismatches.json.
    """
    for label in ('v1', 'v2'):
        p = DATA_DIR / f'{label}_level_type.json'
        if not p.exists():
            sys.exit(
                f'Missing {p}\n'
                'Run `fetch-level-type --v1 <name> --v2 <name>` first.'
            )

    v1_data = json.loads((DATA_DIR / 'v1_level_type.json').read_text())
    v2_data = json.loads((DATA_DIR / 'v2_level_type.json').read_text())
    v1_records = v1_data['records']
    v2_records = v2_data['records']

    in_both = set(v1_records) & set(v2_records)

    mismatches = []
    for oid in in_both:
        v1_lt = v1_records[oid].get('level_type')
        v2_lt = v2_records[oid].get('level_type')
        if v1_lt != v2_lt:
            mismatches.append({
                'objectID': oid,
                'v1_level_type': v1_lt,
                'v2_level_type': v2_lt,
                'content_type': v1_records[oid].get('content_type') or v2_records[oid].get('content_type'),
                'title': (v1_records[oid].get('title') or v2_records[oid].get('title') or ''),
            })

    groups = defaultdict(list)
    for m in mismatches:
        groups[(m['v1_level_type'], m['v2_level_type'])].append(m)

    print()
    print('=' * 72)
    print('LEVEL_TYPE MISMATCH ANALYSIS')
    print('=' * 72)
    print(f'Records in both indices:          {len(in_both):>10,}')
    print(f'Records with level_type mismatch: {len(mismatches):>10,}  '
          f'({len(mismatches)/len(in_both)*100:.2f}%)')
    print()

    SAMPLE = 10
    for (v1_lt, v2_lt), group in sorted(groups.items(), key=lambda x: -len(x[1])):
        print(f'  v1={v1_lt!r:<20}  →  v2={v2_lt!r:<20}  ({len(group):,} records)')
        for m in group[:SAMPLE]:
            title_preview = (m['title'] or '')[:60]
            print(f'    [{m["content_type"] or "?":20}] {m["objectID"]}  "{title_preview}"')
        if len(group) > SAMPLE:
            print(f'    ... and {len(group) - SAMPLE:,} more')
        print()

    out_path = DATA_DIR / 'level_type_mismatches.json'
    out_path.write_text(json.dumps(mismatches, indent=2))
    print(f'Full mismatch list saved → {out_path}')
    print(f'\nv1 fetched at: {v1_data.get("fetched_at", "unknown")}')
    print(f'v2 fetched at: {v2_data.get("fetched_at", "unknown")}')


# ---------------------------------------------------------------------------
# Subcommand: spot-check
# ---------------------------------------------------------------------------

def cmd_spot_check():
    """
    Compare every field of the sampled records between v1 and v2.
    Prints a summary table of fields with differences, sorted by frequency,
    then shows up to 3 concrete examples per differing field.
    """
    for label in ('v1', 'v2'):
        p = DATA_DIR / f'{label}_sample.json'
        if not p.exists():
            sys.exit(
                f'Missing {p}\n'
                'Run `fetch-spot-check --v1 <name> --v2 <name>` first.'
            )

    v1_data = json.loads((DATA_DIR / 'v1_sample.json').read_text())
    v2_data = json.loads((DATA_DIR / 'v2_sample.json').read_text())
    v1_by_id = {r['objectID']: r for r in v1_data['records']}
    v2_by_id = {r['objectID']: r for r in v2_data['records']}
    in_both = sorted(set(v1_by_id) & set(v2_by_id))

    all_fields = set()
    for oid in in_both:
        all_fields |= set(v1_by_id[oid]) | set(v2_by_id[oid])
    all_fields.discard('objectID')

    field_diffs = defaultdict(list)
    for oid in in_both:
        for field in all_fields:
            v1_val = v1_by_id[oid].get(field)
            v2_val = v2_by_id[oid].get(field)
            if v1_val != v2_val:
                field_diffs[field].append({
                    'objectID': oid,
                    'content_type': v1_by_id[oid].get('content_type') or v2_by_id[oid].get('content_type'),
                    'v1': v1_val,
                    'v2': v2_val,
                })

    print()
    print('=' * 72)
    print('SPOT CHECK — FIELD COMPARISON')
    print('=' * 72)
    print(f'Records compared:          {len(in_both)}')
    print(f'Total distinct fields:     {len(all_fields)}')
    print(f'Fields with any diff:      {len(field_diffs)}')
    print(f'Fields always matching:    {len(all_fields) - len(field_diffs)}')
    print()

    if not field_diffs:
        print('All fields match for every sampled record.')
    else:
        print(f'{"Field":<45} {"Diffs":>5}  {"Diff%":>6}')
        print('-' * 60)
        for field, diffs in sorted(field_diffs.items(), key=lambda x: -len(x[1])):
            pct = len(diffs) / len(in_both) * 100
            print(f'{field:<45} {len(diffs):>5}  {pct:>5.1f}%')

        SAMPLE = 3
        print()
        print(f'SAMPLE DIFFS PER FIELD (up to {SAMPLE} per field, most-differing first):')
        print('-' * 72)
        for field, diffs in sorted(field_diffs.items(), key=lambda x: -len(x[1])):
            print(f'\n{field}  ({len(diffs)} diffs):')
            for d in diffs[:SAMPLE]:
                v1_str = _truncate(d['v1'])
                v2_str = _truncate(d['v2'])
                print(f'  [{d["content_type"] or "?":20}] {d["objectID"]}')
                print(f'    v1: {v1_str}')
                print(f'    v2: {v2_str}')

    out_path = DATA_DIR / 'spot_check_diffs.json'
    out_path.write_text(json.dumps({
        'summary': {
            'records_compared': len(in_both),
            'fields_seen': len(all_fields),
            'fields_with_diffs': len(field_diffs),
            'diff_counts': {f: len(d) for f, d in field_diffs.items()},
        },
        'diffs_by_field': {
            field: [{'objectID': d['objectID'], 'content_type': d['content_type'], 'v1': d['v1'], 'v2': d['v2']}
                    for d in diffs]
            for field, diffs in field_diffs.items()
        },
    }, indent=2, default=str))
    print(f'\nFull diff saved → {out_path}')
    print(f'\nv1 fetched at: {v1_data.get("fetched_at", "unknown")}')
    print(f'v2 fetched at: {v2_data.get("fetched_at", "unknown")}')


def _truncate(val, max_len=120):
    s = json.dumps(val, default=str)
    if len(s) > max_len:
        return s[:max_len] + '…'
    return s


# ---------------------------------------------------------------------------
# analyze helpers (unchanged)
# ---------------------------------------------------------------------------

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
# Scans sys.argv for known subcommand tokens, then hands remaining tokens
# (flags) to a shared argparse parser so --v1 / --v2 / --env-file /
# --sample-size are available to any fetch-type subcommand.

_SUBCOMMAND_TOKENS = {
    'fetch', 'analyze',
    'fetch-level-type', 'level-type',
    'fetch-spot-check', 'spot-check',
}


def _split_argv():
    tokens = sys.argv[1:]
    active = {t for t in tokens if t in _SUBCOMMAND_TOKENS}
    flag_argv = [t for t in tokens if t not in _SUBCOMMAND_TOKENS]
    return active, flag_argv


def main():
    active, flag_argv = _split_argv()

    if not active:
        print(__doc__)
        print('error: specify at least one subcommand:', ', '.join(sorted(_SUBCOMMAND_TOKENS)))
        sys.exit(1)

    needs_fetch_args = active & {'fetch', 'fetch-level-type', 'fetch-spot-check'}
    fetch_args = None
    if needs_fetch_args:
        fetch_parser = argparse.ArgumentParser(add_help=False)
        fetch_parser.add_argument('--v1', required=True)
        fetch_parser.add_argument('--v2', required=True)
        fetch_parser.add_argument('--env-file', default='scripts/.env')
        fetch_parser.add_argument('--sample-size', type=int, default=50,
                                  help='Number of records for fetch-spot-check (default: 50)')
        fetch_args, _ = fetch_parser.parse_known_args(flag_argv)

    # Run subcommands in a sensible order: fetches before analyses.
    if 'fetch' in active:
        cmd_fetch(fetch_args)
    if 'fetch-level-type' in active:
        cmd_fetch_level_type(fetch_args)
    if 'fetch-spot-check' in active:
        cmd_fetch_spot_check(fetch_args)
    if 'analyze' in active:
        cmd_analyze()
    if 'level-type' in active:
        cmd_level_type()
    if 'spot-check' in active:
        cmd_spot_check()


if __name__ == '__main__':
    main()
