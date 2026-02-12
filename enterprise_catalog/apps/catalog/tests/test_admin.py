"""
Tests for catalog admin observability improvements.

Verifies that Django admin list views correctly annotate querysets with catalog-content
association counts, improving operator visibility into index health without extra queries.
"""
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory, TestCase

from enterprise_catalog.apps.catalog.admin import (
    CatalogQueryAdmin,
    ContentMetadataAdmin,
    EnterpriseCatalogAdmin,
)
from enterprise_catalog.apps.catalog.constants import COURSE
from enterprise_catalog.apps.catalog.models import (
    CatalogQuery,
    ContentMetadata,
    EnterpriseCatalog,
)
from enterprise_catalog.apps.catalog.tests.factories import (
    CatalogQueryFactory,
    ContentMetadataFactory,
    EnterpriseCatalogFactory,
)


class ContentMetadataAdminObservabilityTests(TestCase):
    """
    Tests that ContentMetadataAdmin correctly shows how many catalogs include each content item.
    This is a Phase 0 observability improvement: operators can now sort/filter by catalog count
    to quickly find orphaned content or identify widely-shared content.
    """

    def setUp(self):
        super().setUp()
        self.site = AdminSite()
        self.admin = ContentMetadataAdmin(ContentMetadata, self.site)
        self.factory = RequestFactory()
        # Build a minimal request (no auth needed for queryset annotation test)
        self.request = self.factory.get('/')
        self.request.user = None  # not needed for annotation checks

    def test_queryset_annotates_catalog_count(self):
        """
        get_queryset() should annotate each ContentMetadata with _catalog_count,
        the number of EnterpriseCatalogs that include it via their CatalogQuery.
        """
        catalog_query = CatalogQueryFactory()
        catalog1 = EnterpriseCatalogFactory(catalog_query=catalog_query)
        catalog2 = EnterpriseCatalogFactory(catalog_query=catalog_query)
        content = ContentMetadataFactory(content_type=COURSE)
        content.catalog_queries.set([catalog_query])

        qs = self.admin.get_queryset(self.request)
        annotated = qs.get(pk=content.pk)

        # The content belongs to one CatalogQuery which has two EnterpriseCatalogs
        self.assertEqual(annotated._catalog_count, 2)

    def test_catalog_count_zero_for_orphaned_content(self):
        """
        Content with no catalog_queries associations should have _catalog_count=0.
        """
        content = ContentMetadataFactory(content_type=COURSE)
        # No catalog_queries associations

        qs = self.admin.get_queryset(self.request)
        annotated = qs.get(pk=content.pk)

        self.assertEqual(annotated._catalog_count, 0)

    def test_get_catalog_count_display_method(self):
        """
        get_catalog_count() should return the annotated count value directly.
        """
        catalog_query = CatalogQueryFactory()
        EnterpriseCatalogFactory(catalog_query=catalog_query)
        content = ContentMetadataFactory(content_type=COURSE)
        content.catalog_queries.set([catalog_query])

        qs = self.admin.get_queryset(self.request)
        annotated = qs.get(pk=content.pk)

        self.assertEqual(self.admin.get_catalog_count(annotated), 1)


class CatalogQueryAdminObservabilityTests(TestCase):
    """
    Tests that CatalogQueryAdmin shows how many content items each query matches
    and which catalogs use it. This is critical for diagnosing sync health:
    a query suddenly dropping to 0 content is a signal of a broken sync.
    """

    def setUp(self):
        super().setUp()
        self.site = AdminSite()
        self.admin = CatalogQueryAdmin(CatalogQuery, self.site)
        self.factory = RequestFactory()
        self.request = self.factory.get('/')
        self.request.user = None

    def test_queryset_annotates_content_metadata_count(self):
        """
        get_queryset() should annotate each CatalogQuery with _content_metadata_count.
        """
        catalog_query = CatalogQueryFactory()
        content1 = ContentMetadataFactory(content_type=COURSE)
        content2 = ContentMetadataFactory(content_type=COURSE)
        content1.catalog_queries.set([catalog_query])
        content2.catalog_queries.set([catalog_query])

        qs = self.admin.get_queryset(self.request)
        annotated = qs.get(pk=catalog_query.pk)

        self.assertEqual(annotated._content_metadata_count, 2)

    def test_content_metadata_count_zero_for_empty_query(self):
        """
        A newly-created CatalogQuery with no associated content should have count=0.
        """
        catalog_query = CatalogQueryFactory()

        qs = self.admin.get_queryset(self.request)
        annotated = qs.get(pk=catalog_query.pk)

        self.assertEqual(annotated._content_metadata_count, 0)

    def test_get_content_metadata_count_display_method(self):
        """
        get_content_metadata_count() should return the annotated count value.
        """
        catalog_query = CatalogQueryFactory()
        content = ContentMetadataFactory(content_type=COURSE)
        content.catalog_queries.set([catalog_query])

        qs = self.admin.get_queryset(self.request)
        annotated = qs.get(pk=catalog_query.pk)

        self.assertEqual(self.admin.get_content_metadata_count(annotated), 1)


class EnterpriseCatalogAdminObservabilityTests(TestCase):
    """
    Tests that EnterpriseCatalogAdmin shows content count in the list view.
    This enables operators to immediately see if a catalog has lost all its content
    (count drops to 0) without having to open each catalog detail page.
    """

    def setUp(self):
        super().setUp()
        self.site = AdminSite()
        self.admin = EnterpriseCatalogAdmin(EnterpriseCatalog, self.site)
        self.factory = RequestFactory()
        self.request = self.factory.get('/')
        self.request.user = None

    def test_queryset_annotates_content_metadata_count(self):
        """
        get_queryset() should annotate each EnterpriseCatalog with _content_metadata_count,
        the number of ContentMetadata records reachable via its CatalogQuery.
        """
        catalog_query = CatalogQueryFactory()
        catalog = EnterpriseCatalogFactory(catalog_query=catalog_query)
        content1 = ContentMetadataFactory(content_type=COURSE)
        content2 = ContentMetadataFactory(content_type=COURSE)
        content1.catalog_queries.set([catalog_query])
        content2.catalog_queries.set([catalog_query])

        qs = self.admin.get_queryset(self.request)
        annotated = qs.get(pk=catalog.pk)

        self.assertEqual(annotated._content_metadata_count, 2)

    def test_content_metadata_count_zero_for_empty_catalog(self):
        """
        A catalog with a query that has no content should have count=0 in the list view.
        """
        catalog_query = CatalogQueryFactory()
        catalog = EnterpriseCatalogFactory(catalog_query=catalog_query)

        qs = self.admin.get_queryset(self.request)
        annotated = qs.get(pk=catalog.pk)

        self.assertEqual(annotated._content_metadata_count, 0)

    def test_get_content_metadata_count_display_method(self):
        """
        get_content_metadata_count() should return the annotated count value.
        """
        catalog_query = CatalogQueryFactory()
        catalog = EnterpriseCatalogFactory(catalog_query=catalog_query)
        content = ContentMetadataFactory(content_type=COURSE)
        content.catalog_queries.set([catalog_query])

        qs = self.admin.get_queryset(self.request)
        annotated = qs.get(pk=catalog.pk)

        self.assertEqual(self.admin.get_content_metadata_count(annotated), 1)
