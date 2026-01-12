from uuid import uuid4

from django.db import transaction
from django.test import TestCase
from rest_framework import serializers

from enterprise_catalog.apps.api.v1.serializers import (
    ContentMetadataSerializer,
    HighlightedContentSerializer,
    HighlightSetSerializer,
    find_and_modify_catalog_query,
)
from enterprise_catalog.apps.catalog.models import CatalogQuery
from enterprise_catalog.apps.catalog.tests.factories import (
    ContentMetadataFactory,
    ContentTranslationFactory,
)
from enterprise_catalog.apps.catalog.utils import get_content_filter_hash
from enterprise_catalog.apps.curation.tests.factories import (
    EnterpriseCurationConfigFactory,
    HighlightedContentFactory,
    HighlightSetFactory,
)


class ContentMetadataSerializerTests(TestCase):
    """
    Tests for the content metadata serializer and how it formats data
    """

    def setUp(self):
        super().setUp()
        self.content_metadata_item = ContentMetadataFactory()

    def test_product_source_formatting(self):
        """
        Test that the content metadata serializer will transform product source data within the json metadata field
        from a string to a dict.
        """
        self.content_metadata_item._json_metadata.update({'product_source': '2u'})  # pylint: disable=protected-access
        self.content_metadata_item.save()
        serialized_data = ContentMetadataSerializer(self.content_metadata_item)
        assert serialized_data.data.get('product_source') == {
            'name': self.content_metadata_item.json_metadata.get('product_source'),
            'slug': None,
            'description': None
        }


class FindCatalogQueryTest(TestCase):
    """
    Tests for API utils
    """

    def setUp(self):
        super().setUp()
        self.old_uuid = uuid4()
        self.old_filter = {'key': ['arglblargl']}
        self.old_catalog_query = CatalogQuery.objects.create(
            content_filter=self.old_filter,
            content_filter_hash=get_content_filter_hash(self.old_filter),
            uuid=self.old_uuid
        )

    def tearDown(self):
        super().tearDown()
        # clean up any stale test objects
        CatalogQuery.objects.all().delete()

    def test_new_uuid_old_filter_saves_query_with_new_uuid(self):
        old_filter = {'key': ['course:testing']}
        CatalogQuery.objects.create(
            content_filter=old_filter,
            content_filter_hash=get_content_filter_hash(old_filter)
        )
        new_uuid = uuid4()
        result = find_and_modify_catalog_query(old_filter, new_uuid)
        self.assertEqual((result.content_filter, result.uuid), (old_filter, new_uuid))

    def test_new_uuid_new_filter_creates_new_query(self):
        new_uuid = uuid4()
        new_filter = {'key': ['course:testingnnnnnn']}
        result = find_and_modify_catalog_query(new_filter, new_uuid)
        self.assertEqual((result.content_filter, result.uuid), (new_filter, new_uuid))

    def test_old_uuid_new_filter_saves_query_with_new_filter(self):
        old_uuid = uuid4()
        old_filter = {'key': ['plpplplpl']}
        new_filter = {'key': ['roger']}
        CatalogQuery.objects.create(
            content_filter=old_filter,
            content_filter_hash=get_content_filter_hash(old_filter),
            uuid=old_uuid
        )
        result = find_and_modify_catalog_query(new_filter, old_uuid)
        self.assertEqual((result.content_filter, result.uuid), (new_filter, old_uuid))

    def test_old_uuid_old_filter_changes_nothing(self):
        result = find_and_modify_catalog_query(self.old_filter, self.old_uuid)
        self.assertEqual(result, self.old_catalog_query)

    def test_no_uuid_old_filter_changes_nothing(self):
        result = find_and_modify_catalog_query(self.old_filter)
        self.assertEqual(result, self.old_catalog_query)

    def test_no_uuid_new_filter_creates_new_query(self):
        new_filter = {'key': ['mmmmmmmm']}
        result = find_and_modify_catalog_query(new_filter)
        self.assertEqual(result.content_filter, new_filter)

    def test_validation_error_raised_on_duplication(self):
        dupe_filter = {'key': ['summerxbreeze']}
        uuid_to_update = uuid4()
        CatalogQuery.objects.create(
            content_filter=dupe_filter,
            uuid=uuid4()
        )
        CatalogQuery.objects.create(
            content_filter={'key': ['tempfilter']},
            uuid=uuid_to_update
        )
        with transaction.atomic():
            self.assertRaises(
                serializers.ValidationError,
                find_and_modify_catalog_query,
                dupe_filter,
                uuid_to_update
            )

    def test_old_uuid_new_title_saves_existing_query_with_title(self):
        new_title = 'testing'
        result = find_and_modify_catalog_query(self.old_filter, self.old_uuid, new_title)
        self.assertEqual(
            (result.content_filter, result.uuid, result.title),
            (self.old_filter, self.old_uuid, new_title)
        )

    def test_title_duplication_causes_error(self):
        query_filter = {'key': ['summerxbreeze']}
        second_filter = {'key': ['winterxfreeze']}
        title = 'testdupe'
        uuid_to_update = uuid4()
        CatalogQuery.objects.create(
            content_filter=query_filter,
            uuid=uuid4(),
            title=title
        )
        CatalogQuery.objects.create(
            content_filter=second_filter,
            uuid=uuid_to_update,
            title='temp_title'
        )
        with transaction.atomic():
            self.assertRaises(
                serializers.ValidationError,
                find_and_modify_catalog_query,
                second_filter,
                uuid_to_update,
                title
            )


class HighlightedContentSerializerTests(TestCase):
    """
    Tests for the HighlightedContentSerializer and multilingual support
    """

    def setUp(self):
        super().setUp()

        self.serializer_class = HighlightedContentSerializer

        # Create content metadata
        self.content_metadata = ContentMetadataFactory()
        self.original_title = self.content_metadata.json_metadata['title']

        # Create Spanish translation
        self.spanish_title = 'Título en Español'
        self.translation = ContentTranslationFactory(
            content_metadata=self.content_metadata,
            language_code='es',
            title=self.spanish_title
        )

        # Create highlighted content
        self.curation_config = EnterpriseCurationConfigFactory()
        self.highlight_set = HighlightSetFactory(enterprise_curation=self.curation_config)
        self.highlighted_content = HighlightedContentFactory(
            catalog_highlight_set=self.highlight_set,
            content_metadata=self.content_metadata
        )

    def test_get_title_with_spanish_language(self):
        """
        Test that get_title returns Spanish translation when lang=es in context.
        """
        context = {'lang': 'es'}
        serializer = self.serializer_class(self.highlighted_content, context=context)

        assert serializer.data['title'] == self.spanish_title

    def test_get_title_with_english_language(self):
        """
        Test that get_title returns original title when lang=en in context.
        """
        context = {'lang': 'en'}
        serializer = self.serializer_class(self.highlighted_content, context=context)

        assert serializer.data['title'] == self.original_title

    def test_get_title_without_language_context(self):
        """
        Test that get_title returns original title when no lang in context.
        """
        context = {}
        serializer = self.serializer_class(self.highlighted_content, context=context)

        assert serializer.data['title'] == self.original_title

    def test_get_title_with_no_translation_available(self):
        """
        Test that get_title falls back to original title when no translation exists.
        """

        # Create content without translation
        content_without_translation = ContentMetadataFactory()
        highlighted_content_no_translation = HighlightedContentFactory(
            catalog_highlight_set=self.highlight_set,
            content_metadata=content_without_translation
        )

        context = {'lang': 'es'}
        serializer = self.serializer_class(highlighted_content_no_translation, context=context)

        original_title = content_without_translation.json_metadata['title']
        assert serializer.data['title'] == original_title

    def test_get_title_with_empty_translation_title(self):
        """
        Test that get_title falls back to original when translation title is empty.
        """

        # Create content with empty translation title
        content_metadata = ContentMetadataFactory()
        ContentTranslationFactory(
            content_metadata=content_metadata,
            language_code='es',
            title=''  # Empty title
        )
        highlighted_content = HighlightedContentFactory(
            catalog_highlight_set=self.highlight_set,
            content_metadata=content_metadata
        )

        context = {'lang': 'es'}
        serializer = self.serializer_class(highlighted_content, context=context)

        original_title = content_metadata.json_metadata['title']
        assert serializer.data['title'] == original_title


class HighlightSetSerializerTests(TestCase):
    """
    Tests for the HighlightSetSerializer and multilingual support
    """

    def setUp(self):
        super().setUp()

        self.serializer_class = HighlightSetSerializer

        # Create curation config and highlight set
        self.curation_config = EnterpriseCurationConfigFactory()
        self.highlight_set = HighlightSetFactory(enterprise_curation=self.curation_config)

        # Create multiple content items with translations
        self.content_items = []
        self.spanish_titles = []
        for i in range(3):
            content = ContentMetadataFactory()
            spanish_title = f'Título en Español {i + 1}'
            ContentTranslationFactory(
                content_metadata=content,
                language_code='es',
                title=spanish_title
            )
            HighlightedContentFactory(
                catalog_highlight_set=self.highlight_set,
                content_metadata=content
            )
            self.content_items.append(content)
            self.spanish_titles.append(spanish_title)

    def test_get_highlighted_content_with_spanish_language(self):
        """
        Test that get_highlighted_content returns Spanish translations when lang=es.
        """
        context = {'lang': 'es'}
        serializer = self.serializer_class(self.highlight_set, context=context)

        highlighted_content = serializer.data['highlighted_content']

        for idx, item in enumerate(highlighted_content):
            assert item['title'] == self.spanish_titles[idx]

    def test_get_highlighted_content_with_english_language(self):
        """
        Test that get_highlighted_content returns original titles when lang=en.
        """
        context = {'lang': 'en'}
        serializer = self.serializer_class(self.highlight_set, context=context)

        highlighted_content = serializer.data['highlighted_content']

        for idx, item in enumerate(highlighted_content):
            original_title = self.content_items[idx].json_metadata['title']
            assert item['title'] == original_title

    def test_get_highlighted_content_prefetch_optimization(self):
        """
        Test that prefetch_related is used for supported languages.
        This is a smoke test to ensure the optimization doesn't break functionality.
        """
        context = {'lang': 'es'}
        serializer = self.serializer_class(self.highlight_set, context=context)

        # If prefetch works correctly, this should not raise any errors
        highlighted_content = serializer.data['highlighted_content']
        assert len(highlighted_content) == 3

        # Verify Spanish titles are returned
        for idx, item in enumerate(highlighted_content):
            assert item['title'] == self.spanish_titles[idx]

    def test_get_highlighted_content_without_language(self):
        """
        Test that get_highlighted_content works without lang in context.
        """
        context = {}
        serializer = self.serializer_class(self.highlight_set, context=context)

        highlighted_content = serializer.data['highlighted_content']

        # Should return original titles
        for idx, item in enumerate(highlighted_content):
            original_title = self.content_items[idx].json_metadata['title']
            assert item['title'] == original_title
