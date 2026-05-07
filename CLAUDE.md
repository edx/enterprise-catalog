# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Enterprise Catalog is a Django-based microservice within the Open edX ecosystem that manages enterprise customer catalogs. It acts as an intermediary between enterprise customers and the course catalog, providing curated content based on enterprise-specific requirements and filters, with integrated search capabilities and bulk content management.

## Test and Quality Instructions

- To run unit tests or generate coverage reports, invoke the `unit-tests` skill.
- Prefer to use docker with explicit test settings: `docker exec -e DJANGO_SETTINGS_MODULE=enterprise_catalog.settings.test enterprise.catalog.app pytest...`
- To run quality checks (linting, style), invoke the `quality-checks` skill.

## Key Principles

- Search the codebase before assuming something isn't implemented
- Write comprehensive tests with clear documentation
- Follow Test-Driven Development when refactoring or modifying existing functionality
- Always write tests for new functionality you implement
- Keep changes focused and minimal
- Follow existing code patterns
- Prefer the `ddt` package for parameterized tests to reduce code duplication

## Documentation & Institutional Memory

- Document new functionality in `docs/how_to/` or `docs/architecture_overview.rst`
- When you learn something important about how this codebase works (gotchas, non-obvious patterns, integration quirks), add it to the relevant section in `docs/architecture_overview.rst` or create new documentation in `docs/how_to/`
- These docs are institutional memory - future sessions (yours or others) will benefit from what you record here

## Architecture Overview

This is a Django service for managing enterprise catalogs within the Open edX ecosystem. It curates course content for enterprise customers based on custom filters and provides search integration through Algolia.

Read `docs/architecture_overview.rst` for comprehensive architecture details.

### Core Applications

- **catalog** - Core catalog models (EnterpriseCatalog, CatalogQuery, ContentMetadata) and business logic
- **api** - REST API endpoints with versioned views (v1, v2), serializers, and filters
- **api_client** - External service integrations (Discovery, LMS, Algolia, Enterprise)
- **curation** - Content curation features and content highlights management
- **ai_curation** - AI-powered content recommendations and curation
- **video_catalog** - Video content metadata management
- **jobs** - Enterprise jobs data integration
- **academy** - Academy content metadata organization
- **core** - Shared utilities, base models, and common functionality
- **track** - Analytics and tracking integration
- **search** - Search functionality and Algolia integration

### Key Concepts

- **EnterpriseCatalog**: Associates enterprise customers with content collections via catalog queries
- **CatalogQuery**: Defines reusable content filtering rules using JSON-based parameters
- **ContentMetadata**: Local cache of course/program metadata synchronized from Discovery Service
- **RestrictedCourseMetadata**: Query-specific versions of courses with filtered restricted runs
- **Normalized Metadata**: Enterprise Catalog provides normalized metadata fields that create consistent data structures across different course types (open courses vs. executive education)

### External Service Integration

- **Course Discovery Service**: Source of truth for course content and metadata
- **LMS**: User authentication, OAuth2 provider, and enterprise customer management
- **Algolia**: Search indexing and query functionality for course discovery
- **Enterprise Service**: Enterprise customer configuration data
- **Celery/Redis**: Asynchronous task processing and job queuing

### Local Development

- This service is included in the [edx/devstack](https://github.com/openedx/devstack) repository for integration testing alongside the rest of the Open edX ecosystem
- Server runs on `localhost:18160`
- Uses Docker Compose with MySQL 8.0, Memcache, Redis, and multiple Celery workers
- Celery workers are organized by queue: default, curations, and algolia

### Management Commands

Key management commands (run via `make app-shell` then execute):
- `./manage.py update_content_metadata` - Sync content from Discovery Service to enterprise catalogs
- `./manage.py update_full_content_metadata` - Fetch additional detailed course metadata
- `./manage.py reindex_algolia` - Rebuild Algolia search index
- `./manage.py migrate` - Apply database migrations

Add `--force` flag to override celery task deduplication (tasks won't run more than once per hour by default).

### Permissions and Authorization

Two authorization mechanisms:
1. **JWT Roles**: Encoded in cookies from LMS (`enterprise_openedx_operator`, `enterprise_catalog_admin`)
2. **Feature-based Role Assignments**: Persisted via `EnterpriseCatalogRoleAssignment` model

## Testing Notes

- Uses pytest with Django integration
- Coverage reporting enabled by default
- PII annotation checks required for Django models
- Docker-based test execution via `make app-shell`
