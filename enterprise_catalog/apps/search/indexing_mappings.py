"""
Cached precomputation of the global mappings the incremental Algolia indexing
batch tasks need on every run.

The legacy reindex flow precomputes program/pathway membership mappings and the
indexable-content-key set once per task invocation. For the incremental flow
each batch task would otherwise repeat that O(catalog_size) work — caching the
result for the duration of a dispatcher pass keeps the cost roughly constant.

Cache invalidation:
  - TTL-based by default (``ALGOLIA_INDEXING_MAPPINGS_CACHE_TIMEOUT`` setting).
  - Phase 4's daily-cron dispatcher will explicitly call
    ``invalidate_indexing_mappings_cache()`` after ``update_content_metadata``
    completes; the TTL is the safety net.
"""
import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.core.cache import cache

from enterprise_catalog.apps.api.tasks import _precalculate_content_mappings
from enterprise_catalog.apps.catalog.algolia_utils import (
    partition_course_keys_for_indexing,
    partition_program_keys_for_indexing,
)
from enterprise_catalog.apps.catalog.constants import (
    COURSE,
    LEARNER_PATHWAY,
    PROGRAM,
)
from enterprise_catalog.apps.catalog.models import ContentMetadata


logger = logging.getLogger(__name__)


CACHE_KEY = 'algolia:indexing_mappings:v1'

# Sentinel returned by ``cache.get`` on a true miss — distinguishes "no cached
# value" from "cached value happens to be falsy/None".
_CACHE_MISS = object()


@dataclass
class IndexingMappings:
    """
    The precomputed shape consumed by the incremental indexing batch tasks.

    Values are content_key strings — small enough to live in Redis and survive
    a Celery payload trip if Phase 4 ever decides to pass it through directly
    instead of relying on the cache.
    """
    program_to_course_keys: dict = field(default_factory=dict)
    pathway_to_program_course_keys: dict = field(default_factory=dict)
    all_indexable_content_keys: set = field(default_factory=set)


def get_indexing_mappings(force_refresh=False):
    """
    Return the cached ``IndexingMappings``, recomputing on miss or when forced.
    """
    if not force_refresh:
        cached = cache.get(CACHE_KEY, _CACHE_MISS)
        if cached is not _CACHE_MISS:
            return cached

    mappings = _compute_indexing_mappings()
    timeout = getattr(settings, 'ALGOLIA_INDEXING_MAPPINGS_CACHE_TIMEOUT', 60 * 30)
    cache.set(CACHE_KEY, mappings, timeout=timeout)
    return mappings


def invalidate_indexing_mappings_cache():
    """
    Drop the cached mappings so the next ``get_indexing_mappings`` call recomputes.
    """
    cache.delete(CACHE_KEY)


def _compute_indexing_mappings():
    """
    Compute the mappings from scratch by reusing the legacy precompute helper
    and partition functions. ``_precalculate_content_mappings`` returns
    ``defaultdict[str, set[str]]`` of content_keys.
    """
    program_to_courses, pathway_to_programs_courses = _precalculate_content_mappings()

    indexable_courses, _ = partition_course_keys_for_indexing(
        ContentMetadata.objects.filter(content_type=COURSE)
    )
    indexable_programs, _ = partition_program_keys_for_indexing(
        ContentMetadata.objects.filter(content_type=PROGRAM)
    )
    pathway_keys = list(
        ContentMetadata.objects.filter(content_type=LEARNER_PATHWAY)
        .values_list('content_key', flat=True)
    )
    all_indexable_content_keys = set(indexable_courses) | set(indexable_programs) | set(pathway_keys)

    return IndexingMappings(
        program_to_course_keys=dict(program_to_courses),
        pathway_to_program_course_keys=dict(pathway_to_programs_courses),
        all_indexable_content_keys=all_indexable_content_keys,
    )
