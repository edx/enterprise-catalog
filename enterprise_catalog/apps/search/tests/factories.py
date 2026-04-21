"""
Test factories for the search app.
"""
import factory

from enterprise_catalog.apps.catalog.tests.factories import (
    ContentMetadataFactory,
)
from enterprise_catalog.apps.search.models import ContentMetadataIndexingState


class ContentMetadataIndexingStateFactory(factory.django.DjangoModelFactory):
    """
    Test factory for the ``ContentMetadataIndexingState`` model.
    """
    class Meta:
        model = ContentMetadataIndexingState

    content_metadata = factory.SubFactory(ContentMetadataFactory)
