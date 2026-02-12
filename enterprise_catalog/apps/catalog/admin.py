from urllib.parse import urlencode

from django.contrib import admin
from django.db.models import Count
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from edx_rbac.admin import UserRoleAssignmentAdmin

from enterprise_catalog.apps.catalog.constants import (
    admin_model_changes_allowed,
)
from enterprise_catalog.apps.catalog.forms import (
    CatalogQueryForm,
    EnterpriseCatalogRoleAssignmentAdminForm,
)
from enterprise_catalog.apps.catalog.models import (
    CatalogQuery,
    ContentMetadata,
    ContentTranslation,
    EnterpriseCatalog,
    EnterpriseCatalogRoleAssignment,
    RestrictedCourseMetadata,
    RestrictedRunAllowedForRestrictedCourse,
)


class CatalogQueryListFilter(admin.SimpleListFilter):
    """
    Filter ContentMetadata records by the CatalogQuery (and thus EnterpriseCatalog) they are associated with.

    This makes it easy for operators to answer "which content is currently associated with query X?"
    from the ContentMetadata list view without needing to write raw SQL.
    """
    title = 'Catalog Query'
    parameter_name = 'catalog_query_id'

    def lookups(self, request, model_admin):
        # Show the 200 most recently modified queries to keep the dropdown manageable.
        queries = CatalogQuery.objects.order_by('-modified')[:200]
        return [
            (str(q.id), q.short_str_for_listings())
            for q in queries
        ]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(catalog_queries__id=self.value())
        return queryset


def _html_list_from_objects(objs, viewname, str_callback=str):
    """
    Get a pretty, clickable list of objects.

    Args:
      objs (iterable of Django ORM objects): List/queryset of objects to display.
      viewname (str): The `viewname` representing the django admin "change" view for the objects in obj.
      str_callback (callable): Optionally, a function to stringify one object for display purposes.
    """
    return format_html_join(
        # I already tried proper HTML lists, but they format really weird in django admin.
        sep=mark_safe('<br>'),
        format_string='<a href="{}">{}</a>',
        args_generator=((reverse(viewname, args=[obj.pk]), str_callback(obj)) for obj in objs),
    )


class UnchangeableMixin(admin.ModelAdmin):
    """
    Mixin for disabling changing models through the admin

    We're disabling changing models in this admin while we transition over from the LMS
    """
    @classmethod
    def has_add_permission(cls, request):  # pylint: disable=arguments-differ
        return admin_model_changes_allowed()

    @classmethod
    def has_delete_permission(cls, request, obj=None):  # pylint: disable=arguments-differ
        return admin_model_changes_allowed()

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        extra_context = extra_context or {}
        if not admin_model_changes_allowed():
            extra_context['show_save_and_continue'] = False
            extra_context['show_save'] = False

        return super().changeform_view(request, object_id, extra_context=extra_context)


@admin.register(ContentMetadata)
class ContentMetadataAdmin(UnchangeableMixin):
    """ Admin configuration for the custom ContentMetadata model. """
    list_display = (
        'id',  # added to facilitate creating highlighted content (curation app) via the ContentMetadata `id`.
        'content_key',
        'content_type',
        'parent_content_key',
        'get_catalog_count',
    )
    list_filter = (
        'content_type',
        CatalogQueryListFilter,
    )
    search_fields = (
        'content_key',
        'parent_content_key',
    )
    readonly_fields = (
        'associated_content_metadata',
        'get_catalog_queries',
        'get_catalogs',
        'get_catalog_count',
        'get_restricted_courses_for_this_course',
        'get_restricted_courses_for_this_restricted_run',
        'modified',
    )
    exclude = (
        'catalog_queries',
    )

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _catalog_count=Count('catalog_queries__enterprise_catalogs', distinct=True),
        )

    @admin.display(description='# Catalogs', ordering='_catalog_count')
    def get_catalog_count(self, obj):
        """Number of EnterpriseCatalogs that include this content via their CatalogQuery."""
        return getattr(obj, '_catalog_count', None)

    @admin.display(description='Catalog Queries')
    def get_catalog_queries(self, obj):
        catalog_queries = obj.catalog_queries.all()
        return _html_list_from_objects(
            objs=catalog_queries,
            viewname="admin:catalog_catalogquery_change",
            str_callback=lambda cq: cq.short_str_for_listings(),
        )

    @admin.display(description='Enterprise Catalogs')
    def get_catalogs(self, obj):
        catalogs = EnterpriseCatalog.objects.filter(
            catalog_query_id__in=obj.catalog_queries.all().values_list('id')
        )
        return _html_list_from_objects(catalogs, "admin:catalog_enterprisecatalog_change")

    @admin.display(description='Restricted Courses For This Course')
    def get_restricted_courses_for_this_course(self, obj):
        restricted_courses = RestrictedCourseMetadata.objects.filter(unrestricted_parent=obj)
        return _html_list_from_objects(restricted_courses, "admin:catalog_restrictedcoursemetadata_change")

    @admin.display(description='Restricted Courses For This Restricted Run')
    def get_restricted_courses_for_this_restricted_run(self, obj):
        restricted_runs_allowed_for_restricted_course = RestrictedRunAllowedForRestrictedCourse.objects.select_related(
            'course',
        ).filter(
            run=obj,
        )
        restricted_courses = (relationship.course for relationship in restricted_runs_allowed_for_restricted_course)
        return _html_list_from_objects(restricted_courses, "admin:catalog_restrictedcoursemetadata_change")

    def get_form(self, *args, **kwargs):
        addl_help_texts = {
            'get_restricted_courses_for_this_course': (
                'If this is a course, list any "restricted" versions of this course.'
            ),
            'get_restricted_courses_for_this_restricted_run': (
                'If this is a restricted run, list all RestrictedCourses to which it is related.'
            ),
        }
        return super().get_form(*args, **(kwargs | {'help_texts': addl_help_texts}))


@admin.register(RestrictedCourseMetadata)
class RestrictedCourseMetadataAdmin(UnchangeableMixin):
    """ Admin configuration for the custom RestrictedCourseMetadata model. """
    list_display = (
        'content_key',
        'get_catalog_query_for_list',
        'get_unrestricted_parent',
    )
    search_fields = (
        'content_key',
        'catalog_query',
    )
    readonly_fields = (
        'get_catalog_query',
        'get_catalogs',
        'get_restricted_runs_allowed',
        'modified',
    )
    exclude = (
        'catalog_query',
    )

    @admin.display(
        description='Catalog Query'
    )
    def get_catalog_query_for_list(self, obj):
        if not obj.catalog_query:
            return None

        link = reverse("admin:catalog_catalogquery_change", args=[obj.catalog_query.id])
        return format_html('<a href="{}">{}</a>', link, obj.catalog_query.short_str_for_listings())

    @admin.display(
        description='Catalog Query'
    )
    def get_catalog_query(self, obj):
        if not obj.catalog_query:
            return None

        link = reverse("admin:catalog_catalogquery_change", args=[obj.catalog_query.id])
        return format_html('<a href="{}">{}</a>', link, obj.catalog_query.pretty_print_content_filter())

    @admin.display(
        description='Unrestricted Parent'
    )
    def get_unrestricted_parent(self, obj):
        link = reverse("admin:catalog_contentmetadata_change", args=[obj.unrestricted_parent.id])
        return format_html('<a href="{}">{}</a>', link, str(obj.unrestricted_parent))

    @admin.display(description='Enterprise Catalogs')
    def get_catalogs(self, obj):
        catalogs = EnterpriseCatalog.objects.filter(catalog_query=obj.catalog_query)
        return _html_list_from_objects(catalogs, "admin:catalog_enterprisecatalog_change")

    @admin.display(description='Restricted Runs Allowed')
    def get_restricted_runs_allowed(self, obj):
        restricted_runs_allowed_for_restricted_course = RestrictedRunAllowedForRestrictedCourse.objects.select_related(
            'run',
        ).filter(
            course=obj,
        )
        restricted_runs = [
            relationship.run for relationship in restricted_runs_allowed_for_restricted_course
            if relationship.run
        ]
        return _html_list_from_objects(restricted_runs, "admin:catalog_contentmetadata_change")


@admin.register(RestrictedRunAllowedForRestrictedCourse)
class RestrictedRunAllowedForRestrictedCourseAdmin(UnchangeableMixin):
    """
    Admin class to show restricted course <-> run relationships.
    """
    list_display = (
        'id',
        'course',
        'run',
    )


@admin.register(CatalogQuery)
class CatalogQueryAdmin(UnchangeableMixin):
    """ Admin configuration for the custom CatalogQuery model. """
    fields = (
        'uuid',
        'title',
        'content_filter',
        'get_associated_catalogs',
        'get_content_metadata_count',
        'get_view_content_link',
    )
    readonly_fields = (
        'uuid',
        'get_associated_catalogs',
        'get_content_metadata_count',
        'get_view_content_link',
    )
    list_display = (
        'uuid',
        'title',
        'content_filter_hash',
        'get_content_metadata_count',
        'get_view_content_link',
        'get_content_filter',
    )
    search_fields = (
        'content_filter_hash',
        'title',
    )

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _content_metadata_count=Count('contentmetadata', distinct=True),
        )

    @admin.display(description='Content Filter')
    def get_content_filter(self, obj):
        return obj.pretty_print_content_filter()

    @admin.display(description='# Content Items', ordering='_content_metadata_count')
    def get_content_metadata_count(self, obj):
        """Number of ContentMetadata records associated with this CatalogQuery."""
        return getattr(obj, '_content_metadata_count', obj.contentmetadata_set.count())

    @admin.display(description='Associated Enterprise Catalogs')
    def get_associated_catalogs(self, obj):
        catalogs = obj.enterprise_catalogs.all()
        return _html_list_from_objects(catalogs, "admin:catalog_enterprisecatalog_change")

    @admin.display(description='Browse Content')
    def get_view_content_link(self, obj):
        """
        Clickable link to the ContentMetadata list filtered to this CatalogQuery.

        Allows operators to immediately see which content records are currently associated
        with this query, directly from the CatalogQuery admin list or detail view.
        """
        url = (
            reverse('admin:catalog_contentmetadata_changelist')
            + '?'
            + urlencode({'catalog_query_id': obj.pk})
        )
        count = getattr(obj, '_content_metadata_count', None)
        label = f'View {count} content items' if count is not None else 'View content'
        return format_html('<a href="{}">{}</a>', url, label)

    form = CatalogQueryForm


@admin.register(EnterpriseCatalog)
class EnterpriseCatalogAdmin(UnchangeableMixin):
    """ Admin configuration for the custom EnterpriseCatalog model. """
    list_display = (
        'uuid',
        'enterprise_uuid',
        'enterprise_name',
        'title',
        'get_catalog_query',
        'get_content_metadata_count',
        'get_view_content_link',
    )

    search_fields = (
        'uuid',
        'enterprise_uuid',
        'enterprise_name',
        'title',
        'catalog_query__content_filter_hash__exact'
    )

    autocomplete_fields = (
        'catalog_query',
    )

    list_select_related = (
        'catalog_query',
    )

    readonly_fields = (
        'get_content_metadata_count',
        'get_view_content_link',
    )

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _content_metadata_count=Count('catalog_query__contentmetadata', distinct=True),
        )

    @admin.display(
        description='# Content Items',
        ordering='_content_metadata_count',
    )
    def get_content_metadata_count(self, obj):
        """Number of ContentMetadata records associated with this catalog via its CatalogQuery."""
        return getattr(obj, '_content_metadata_count', obj.catalog_query.contentmetadata_set.count())

    @admin.display(
        description='Catalog Query'
    )
    def get_catalog_query(self, obj):
        link = reverse("admin:catalog_catalogquery_change", args=[obj.catalog_query.id])
        return format_html('<a href="{}">{}</a>', link, obj.catalog_query.pretty_print_content_filter())

    @admin.display(description='Browse Content')
    def get_view_content_link(self, obj):
        """
        Clickable link to the ContentMetadata list filtered to this catalog's CatalogQuery.

        Operators can use this to quickly inspect exactly which content records are currently
        associated with a catalog, without needing raw SQL or API calls.
        """
        if not obj.catalog_query_id:
            return '—'
        url = (
            reverse('admin:catalog_contentmetadata_changelist')
            + '?'
            + urlencode({'catalog_query_id': obj.catalog_query_id})
        )
        count = getattr(obj, '_content_metadata_count', None)
        label = f'View {count} content items' if count is not None else 'View content'
        return format_html('<a href="{}">{}</a>', url, label)


@admin.register(EnterpriseCatalogRoleAssignment)
class EnterpriseCatalogRoleAssignmentAdmin(UserRoleAssignmentAdmin):
    """
    Django admin for EnterpriseCatalogRoleAssignment Model.
    """
    list_display = (
        'get_username',
        'role',
        'enterprise_id',
    )

    @admin.display(
        description='User'
    )
    def get_username(self, obj):
        return obj.user.username

    class Meta:
        """
        Meta class for EnterpriseCatalogRoleAssignmentAdmin.
        """

        model = EnterpriseCatalogRoleAssignment

    fields = ('user', 'role', 'enterprise_id', 'applies_to_all_contexts')
    form = EnterpriseCatalogRoleAssignmentAdminForm


@admin.register(ContentTranslation)
class ContentTranslationAdmin(admin.ModelAdmin):
    """
    Admin configuration for the ContentTranslation model.
    """
    list_display = ('content_metadata', 'language_code', 'modified', 'source_hash')
    list_filter = ('language_code', 'modified')
    search_fields = ('content_metadata__content_key', 'title')
    readonly_fields = ('created', 'modified', 'source_hash')
    raw_id_fields = ('content_metadata',)
    fields = (
        'content_metadata',
        'language_code',
        'title',
        'short_description',
        'full_description',
        'outcome',
        'prerequisites',
        'subtitle',
        'source_hash',
        'created',
        'modified',
    )
