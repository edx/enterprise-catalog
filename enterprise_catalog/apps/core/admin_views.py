"""
Admin-only, read-only view that displays the resolved Django settings.

Sensitive values are redacted by Django's ``SafeExceptionReporterFilter`` —
the same filter used by the technical 500 page. The redaction regex is
extended to cover project-specific settings that don't match the default
``API|TOKEN|KEY|SECRET|PASS|SIGNATURE`` pattern.
"""
import pprint
import re

from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import user_passes_test
from django.shortcuts import render
from django.views.debug import SafeExceptionReporterFilter


class CatalogSafeReporterFilter(SafeExceptionReporterFilter):
    """Extends Django's redaction regex with project-specific patterns."""
    hidden_settings = re.compile(
        r"API|TOKEN|KEY|SECRET|PASS|SIGNATURE|CLIENT_ID|ALGOLIA|BRAZE",
        flags=re.IGNORECASE,
    )


@staff_member_required
@user_passes_test(lambda u: u.is_superuser)
def settings_view(request):
    """Render a read-only table of cleansed Django settings for superusers."""
    safe = CatalogSafeReporterFilter().get_safe_settings()
    rows = [(name, pprint.pformat(value, width=100)) for name, value in sorted(safe.items())]
    context = {
        **admin.site.each_context(request),
        'title': 'Project settings',
        'rows': rows,
    }
    return render(request, 'admin/settings_view.html', context)
