"""
Tests for the catalog_content_status management command.
"""
import uuid
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from enterprise_catalog.apps.catalog.constants import COURSE, PROGRAM
from enterprise_catalog.apps.catalog.tests.factories import (
    CatalogQueryFactory,
    ContentMetadataFactory,
    EnterpriseCatalogFactory,
)


class CatalogContentStatusCommandTests(TestCase):
    """Tests for the catalog_content_status management command."""

    command_name = 'catalog_content_status'

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.enterprise_uuid = uuid.uuid4()
        cls.catalog_query = CatalogQueryFactory()
        cls.catalog = EnterpriseCatalogFactory(
            enterprise_uuid=cls.enterprise_uuid,
            enterprise_name='Test Enterprise',
            title='Test Catalog',
            catalog_query=cls.catalog_query,
        )
        cls.courses = ContentMetadataFactory.create_batch(3, content_type=COURSE)
        cls.programs = ContentMetadataFactory.create_batch(2, content_type=PROGRAM)
        cls.catalog_query.contentmetadata_set.set(cls.courses + cls.programs)

    def test_single_catalog_shows_counts(self):
        """
        Calling with --catalog-uuid should print count totals and a per-type breakdown.
        """
        out = StringIO()
        call_command(self.command_name, catalog_uuid=str(self.catalog.uuid), stdout=out)
        output = out.getvalue()

        assert str(self.catalog.uuid) in output
        assert str(self.enterprise_uuid) in output
        assert 'Test Catalog' in output
        assert 'Total content items: 5' in output
        assert COURSE in output
        assert PROGRAM in output
        # Breakdown counts
        assert '3' in output  # 3 courses
        assert '2' in output  # 2 programs

    def test_single_catalog_show_content_keys(self):
        """
        With --show-content-keys, each content_key should appear in the output.
        """
        out = StringIO()
        call_command(
            self.command_name,
            catalog_uuid=str(self.catalog.uuid),
            show_content_keys=True,
            stdout=out,
        )
        output = out.getvalue()

        for cm in self.courses + self.programs:
            assert cm.content_key in output

    def test_enterprise_uuid_lists_all_catalogs(self):
        """
        Calling with --enterprise-uuid should list all catalogs for that enterprise with counts.
        """
        # Create a second catalog for the same enterprise
        query2 = CatalogQueryFactory()
        catalog2 = EnterpriseCatalogFactory(
            enterprise_uuid=self.enterprise_uuid,
            enterprise_name='Test Enterprise',
            title='Second Catalog',
            catalog_query=query2,
        )
        extra_courses = ContentMetadataFactory.create_batch(1, content_type=COURSE)
        query2.contentmetadata_set.set(extra_courses)

        out = StringIO()
        call_command(self.command_name, enterprise_uuid=str(self.enterprise_uuid), stdout=out)
        output = out.getvalue()

        assert 'Test Catalog' in output
        assert 'Second Catalog' in output
        assert str(self.catalog.uuid) in output
        assert str(catalog2.uuid) in output

    def test_unknown_catalog_uuid_reports_error(self):
        """
        Using an unknown catalog UUID should print an error to stderr and exit cleanly.
        """
        err = StringIO()
        out = StringIO()
        call_command(
            self.command_name,
            catalog_uuid=str(uuid.uuid4()),
            stdout=out,
            stderr=err,
        )
        assert 'No EnterpriseCatalog found' in err.getvalue()

    def test_unknown_enterprise_uuid_reports_warning(self):
        """
        Using an enterprise UUID with no catalogs should print a warning to stderr.
        """
        err = StringIO()
        out = StringIO()
        call_command(
            self.command_name,
            enterprise_uuid=str(uuid.uuid4()),
            stdout=out,
            stderr=err,
        )
        assert 'No catalogs found' in err.getvalue()
