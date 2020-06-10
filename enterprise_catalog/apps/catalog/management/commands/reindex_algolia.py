import json
import logging

from django.conf import settings
from django.core.management.base import BaseCommand

from enterprise_catalog.apps.api.tasks import (
    index_enterprise_catalog_courses_in_algolia,
)
from enterprise_catalog.apps.api_client.algolia import AlgoliaSearchClient
from enterprise_catalog.apps.catalog.constants import COURSE
from enterprise_catalog.apps.catalog.models import (
    CatalogQuery,
    ContentMetadata,
    EnterpriseCatalog,
)


logger = logging.getLogger(__name__)

ALGOLIA_INDEX_NAME = settings.ALGOLIA.get('INDEX_NAME')

# keep attributes from course objects that we explicitly want in Algolia
ALGOLIA_FIELDS = [
    'availability',
    'additional_information',
    'card_image_url',
    'enterprise_catalog_uuids',
    'enterprise_customer_uuids',
    'entitlements',
    'expected_learning_items',
    'extra_description',
    'faq',
    'full_description',
    'key',  # for links to course about pages from the Learner Portal search page
    'language',
    'level_type',
    'objectID',  # required by Algolia, e.g. "course-{uuid}"
    'outcome',
    'owners',
    'programs',
    'recent_enrollment_count',
    'short_description',
    'subjects',
    'syllabus_raw',
    'title',
    'uuid',
]

# default configuration for the index
ALGOLIA_INDEX_SETTINGS = {
    'searchableAttributes': [
        'unordered(title)',
        'unordered(full_description)',
        'unordered(short_description)',
        'unordered(additional_information)',
        'owners.name',
    ],
    'attributesForFaceting': [
        'enterprise_catalog_uuids',
        'enterprise_customer_uuids',
        'availability',
        'language',
        'level_type',
        'owners.name',
        'programs.type',
        'subjects.name',
    ],
    'unretrievableAttributes': [
        'enterprise_catalog_uuids',
        'enterprise_customer_uuids',
    ],
    'customRanking': [
        'desc(recent_enrollment_count)',
    ],
}


class Command(BaseCommand):
    help = (
        'Reindex course data in Algolia from course-discovery, adding on enterprise-specific metadata'
    )

    def batch(self, iterable, batch_size=1):
        """
        Break up an iterable into equal-sized batches.

        Arguments:
            iterable (e.g. list): an iterable to batch
            batch_size (int): the size of each batch. Defaults to 1.

        Returns:
            generator: iterates through each batch of an iterable
        """
        iterable_len = len(iterable)
        for index in range(0, iterable_len, batch_size):
            yield iterable[index:min(index + batch_size, iterable_len)]

    def handle(self, *args, **options):
        """
        Spin off tasks to fetch courses from the discovery service and index them in Algolia.
        """
        algolia_client = AlgoliaSearchClient()
        algolia_client.init_index()

        # configure the Algolia index
        algolia_client.set_index_settings(ALGOLIA_INDEX_SETTINGS)

        # find all ContentMetadata records with a content type of "course" that are
        # also part of at least one EnterpriseCatalog
        content_metadata = ContentMetadata.objects.filter(
            content_type=COURSE,
            catalog_queries__enterprise_catalogs__isnull=False,
        ).distinct()

        if not content_metadata:
            message = (
                'There are no ContentMetadata records of content type "%s" that are '
                'part of at least one EnterpriseCatalog.'
            )
            logger.error(message, COURSE)
            # we can't do much without content_metadata so return early
            return

        content_keys = [metadata.content_key for metadata in content_metadata]

        # break up the content_keys in smaller batches, where each batch will spin off its
        # own celery task. this should help performance and prevent errors with having too
        # many content_keys for a GET request to the discovery service's /courses endpoint
        for keys in self.batch(content_keys, batch_size=50):
            index_enterprise_catalog_courses_in_algolia.delay(
                content_keys=keys,
                algolia_fields=ALGOLIA_FIELDS,
            )
            message = (
                'Spinning off index_enterprise_catalog_courses_in_algolia from reindex_algolia command'
                ' to add %d courses to Algolia\'s %s index: %s'
            )
            logger.info(message, len(content_keys), ALGOLIA_INDEX_NAME, keys)
