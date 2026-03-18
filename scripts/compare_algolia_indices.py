#!/usr/bin/env python
"""
Compare two Algolia indices to verify parity.

This script helps validate that a v2 index (populated via incremental indexing)
matches the v1 index (populated via full replace_all_objects).

Usage:
    1. Set up enterprise_catalog/settings/private.py with Algolia credentials
    2. Run: cat scripts/compare_algolia_indices.py | ./manage.py shell

Example:
    # In private.py:
    ALGOLIA = {
        'APPLICATION_ID': 'your-app-id',
        'API_KEY': 'your-admin-api-key',
        'INDEX_NAME': 'enterprise_catalog',  # v1 index
        'REPLICA_INDEX_NAME': 'enterprise_catalog_replica',
    }

    # Then run:
    cat scripts/compare_algolia_indices.py | ./manage.py shell
"""
import json
from collections import defaultdict

from algoliasearch.search_client import SearchClient
from django.conf import settings


# Configuration - adjust these as needed
V1_INDEX_NAME = settings.ALGOLIA.get('INDEX_NAME', 'enterprise_catalog')
V2_INDEX_NAME = 'enterprise_catalog_v2'  # Change this to your v2 index name

# Initialize Algolia client
client = SearchClient.create(
    settings.ALGOLIA['APPLICATION_ID'],
    settings.ALGOLIA['API_KEY'],
)

v1_index = client.init_index(V1_INDEX_NAME)
v2_index = client.init_index(V2_INDEX_NAME)


def get_all_objects(index, index_name):
    """Retrieve all objects from an index using browse."""
    print(f'Loading all objects from {index_name}...')
    objects = {}
    count = 0

    for hit in index.browse_objects({'attributesToRetrieve': ['*']}):
        object_id = hit.get('objectID')
        if object_id:
            objects[object_id] = hit
            count += 1
            if count % 1000 == 0:
                print(f'  Loaded {count} objects...')

    print(f'  Total: {count} objects')
    return objects


def get_object_counts_by_type(objects):
    """Count objects by content_type."""
    counts = defaultdict(int)
    for obj in objects.values():
        content_type = obj.get('content_type', 'unknown')
        counts[content_type] += 1
    return dict(counts)


def get_aggregation_key_set(objects):
    """Get set of unique aggregation_keys."""
    return {obj.get('aggregation_key') for obj in objects.values() if obj.get('aggregation_key')}


def compare_object(v1_obj, v2_obj, object_id):
    """Compare two objects and return differences."""
    differences = []

    # Fields to compare (excluding timestamps and other volatile fields)
    fields_to_compare = [
        'content_type',
        'aggregation_key',
        'title',
        'key',
        'partners',
        'enterprise_catalog_query_uuids',
        'enterprise_customer_uuids',
    ]

    for field in fields_to_compare:
        v1_value = v1_obj.get(field)
        v2_value = v2_obj.get(field)

        # Handle sets/lists that might be in different order
        if isinstance(v1_value, list) and isinstance(v2_value, list):
            if set(v1_value) != set(v2_value):
                differences.append({
                    'field': field,
                    'v1': v1_value,
                    'v2': v2_value,
                })
        elif v1_value != v2_value:
            differences.append({
                'field': field,
                'v1': v1_value,
                'v2': v2_value,
            })

    return differences


def main():
    """Main comparison logic."""
    print('=' * 60)
    print('Algolia Index Comparison')
    print('=' * 60)
    print(f'V1 Index: {V1_INDEX_NAME}')
    print(f'V2 Index: {V2_INDEX_NAME}')
    print()

    # Load all objects
    v1_objects = get_all_objects(v1_index, V1_INDEX_NAME)
    v2_objects = get_all_objects(v2_index, V2_INDEX_NAME)

    print()
    print('-' * 60)
    print('RECORD COUNTS')
    print('-' * 60)

    # Compare counts by content type
    v1_counts = get_object_counts_by_type(v1_objects)
    v2_counts = get_object_counts_by_type(v2_objects)

    all_types = set(v1_counts.keys()) | set(v2_counts.keys())
    print(f'{"Content Type":<20} {"V1":<10} {"V2":<10} {"Diff":<10}')
    print('-' * 50)

    for content_type in sorted(all_types):
        v1_count = v1_counts.get(content_type, 0)
        v2_count = v2_counts.get(content_type, 0)
        diff = v2_count - v1_count
        diff_str = f'+{diff}' if diff > 0 else str(diff)
        status = '✓' if diff == 0 else '!'
        print(f'{content_type:<20} {v1_count:<10} {v2_count:<10} {diff_str:<10} {status}')

    print()
    total_v1 = sum(v1_counts.values())
    total_v2 = sum(v2_counts.values())
    print(f'{"TOTAL":<20} {total_v1:<10} {total_v2:<10} {total_v2 - total_v1}')

    print()
    print('-' * 60)
    print('AGGREGATION KEY COMPARISON')
    print('-' * 60)

    v1_agg_keys = get_aggregation_key_set(v1_objects)
    v2_agg_keys = get_aggregation_key_set(v2_objects)

    only_in_v1 = v1_agg_keys - v2_agg_keys
    only_in_v2 = v2_agg_keys - v1_agg_keys
    in_both = v1_agg_keys & v2_agg_keys

    print(f'Unique aggregation keys in V1: {len(v1_agg_keys)}')
    print(f'Unique aggregation keys in V2: {len(v2_agg_keys)}')
    print(f'In both: {len(in_both)}')
    print(f'Only in V1: {len(only_in_v1)}')
    print(f'Only in V2: {len(only_in_v2)}')

    if only_in_v1:
        print()
        print('First 10 aggregation keys only in V1:')
        for key in list(only_in_v1)[:10]:
            print(f'  - {key}')

    if only_in_v2:
        print()
        print('First 10 aggregation keys only in V2:')
        for key in list(only_in_v2)[:10]:
            print(f'  - {key}')

    print()
    print('-' * 60)
    print('OBJECT ID COMPARISON')
    print('-' * 60)

    v1_ids = set(v1_objects.keys())
    v2_ids = set(v2_objects.keys())

    only_in_v1_ids = v1_ids - v2_ids
    only_in_v2_ids = v2_ids - v1_ids
    in_both_ids = v1_ids & v2_ids

    print(f'Object IDs in V1: {len(v1_ids)}')
    print(f'Object IDs in V2: {len(v2_ids)}')
    print(f'In both: {len(in_both_ids)}')
    print(f'Only in V1: {len(only_in_v1_ids)}')
    print(f'Only in V2: {len(only_in_v2_ids)}')

    if only_in_v1_ids:
        print()
        print('First 10 object IDs only in V1:')
        for oid in list(only_in_v1_ids)[:10]:
            print(f'  - {oid}')

    if only_in_v2_ids:
        print()
        print('First 10 object IDs only in V2:')
        for oid in list(only_in_v2_ids)[:10]:
            print(f'  - {oid}')

    print()
    print('-' * 60)
    print('CONTENT COMPARISON (spot check)')
    print('-' * 60)

    # Compare a sample of objects that exist in both
    sample_ids = list(in_both_ids)[:100]
    objects_with_differences = []

    for object_id in sample_ids:
        v1_obj = v1_objects[object_id]
        v2_obj = v2_objects[object_id]
        differences = compare_object(v1_obj, v2_obj, object_id)
        if differences:
            objects_with_differences.append({
                'object_id': object_id,
                'differences': differences,
            })

    if objects_with_differences:
        print(f'Found {len(objects_with_differences)} objects with differences (out of {len(sample_ids)} sampled):')
        for obj in objects_with_differences[:5]:
            print(f'\n  Object ID: {obj["object_id"]}')
            for diff in obj['differences']:
                print(f'    - {diff["field"]}:')
                print(f'        V1: {diff["v1"]}')
                print(f'        V2: {diff["v2"]}')
    else:
        print(f'No differences found in {len(sample_ids)} sampled objects. ✓')

    print()
    print('=' * 60)
    print('SUMMARY')
    print('=' * 60)

    issues = []
    if total_v1 != total_v2:
        issues.append(f'Record count mismatch: V1={total_v1}, V2={total_v2}')
    if only_in_v1:
        issues.append(f'{len(only_in_v1)} aggregation keys missing from V2')
    if only_in_v2:
        issues.append(f'{len(only_in_v2)} extra aggregation keys in V2')
    if objects_with_differences:
        issues.append(f'{len(objects_with_differences)} objects have content differences')

    if issues:
        print('Issues found:')
        for issue in issues:
            print(f'  ! {issue}')
    else:
        print('✓ V2 index appears to match V1 index')


if __name__ == '__main__':
    main()
else:
    # Running via manage.py shell
    main()
