# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

Enterprise Catalog is a Django-based microservice that manages enterprise catalogs, associating enterprise customers with curated courses from the full course catalog. It integrates with multiple edX services including course-discovery, LMS, and Algolia for search functionality.

## Development Commands

### Testing and Quality

You must enter a docker container shell to run tests and linters.
```bash
# Run tests via docker container
docker exec enterprise.catalog.app bash -c "pytest -c pytest.local.ini <TEST_FILES>"
docker exec enterprise.catalog.app bash -c "make isort style lint"
```

Important: make use of ddt to reduce test setup boilerplate.

### Management Commands
Key management commands (run via `docker exec` as you do for tests)
- `./manage.py update_content_metadata` - Sync content from discovery service to enterprise catalogs
- `./manage.py update_full_content_metadata` - Fetch additional course metadata
- `./manage.py reindex_algolia` - Rebuild Algolia search index
- `./manage.py migrate` - Apply database migrations

Add `--force` flag to override celery task deduplication (tasks won't run more than once per hour by default).

## Architecture

### Core Apps Structure
- `enterprise_catalog/apps/catalog/` - Core catalog models and business logic
- `enterprise_catalog/apps/api/` - REST API endpoints (v1 and v2)
- `enterprise_catalog/apps/api_client/` - External service clients (Discovery, Enterprise, Algolia, etc.)
- `enterprise_catalog/apps/curation/` - Content curation and highlights
- `enterprise_catalog/apps/ai_curation/` - AI-powered content curation
- `enterprise_catalog/apps/video_catalog/` - Video content management
- `enterprise_catalog/apps/jobs/` - Enterprise jobs integration

### Key Models
- `EnterpriseCatalog` - Associates enterprises with content filters
- `CatalogQuery` - Defines content filtering rules
- `ContentMetadata` - Cached content from discovery service
- `EnterpriseCatalogRoleAssignment` - Permission management

### External Integrations
- **Discovery Service** - Source of truth for course content
- **LMS** - Authentication and enterprise user management
- **Algolia** - Search indexing and query functionality
- **Enterprise Service** - Enterprise customer data
- **Celery/Redis** - Asynchronous task processing

### Permission System
Two authorization mechanisms:
1. JWT Roles encoded in cookies from LMS
2. Feature-based Role Assignments via `EnterpriseCatalogRoleAssignment` model

## Development Notes

### Settings
- Development settings in `enterprise_catalog/settings/`
- Create `private.py` for local overrides (gitignored)
- Docker configuration uses `devstack.py` settings

### Monitoring tools
You can use edx-django-utils `monitoring.function_trace()` decorator to explicitly wrap
functions as a segment in our monitoring tool (datadog), but we get traces on most python
view and celery operations out of the box.
