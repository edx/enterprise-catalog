""" Admin configuration for core models. """

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.utils.translation import gettext_lazy as _

from enterprise_catalog.apps.core.models import User


# Use our custom admin index template so the dashboard can include a Tools
# panel linking to the project settings view (see admin_views.py). The
# template extends the upstream admin/index.html, so renaming it (rather
# than overriding admin/index.html directly) avoids template-extends
# recursion from our project-level templates dir.
admin.site.index_template = 'admin/catalog_index.html'


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    """ Admin configuration for the custom User model. """
    list_display = ('username', 'email', 'full_name', 'first_name', 'last_name', 'is_staff')
    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        (_('Personal info'), {'fields': ('full_name', 'first_name', 'last_name', 'email')}),
        (_('Permissions'), {'fields': ('is_active', 'is_staff', 'is_superuser',
                                       'groups', 'user_permissions')}),
        (_('Important dates'), {'fields': ('last_login', 'date_joined')}),
    )
