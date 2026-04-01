Enterprise Ecosystem Repositories
==================================

This document lists all GitHub repositories in the ``edx`` and ``openedx`` GitHub
organizations that are used in the development of the edX enterprise software. These
span core microservices, frontend applications, shared libraries, and development
tooling.

.. contents::
   :local:
   :depth: 2


Core Enterprise Repositories
-----------------------------

These repositories make up the enterprise product's microservices and frontend
applications.

Microservices / Backend Services
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 35 30 35

   * - Repository
     - Purpose
     - Usage
   * - `openedx/enterprise-catalog <https://github.com/openedx/enterprise-catalog>`_
     - Django-based microservice for managing enterprise catalogs, associating
       enterprise customers with curated courses from the full course catalog.
     - Core service — this repository.
   * - `openedx/edx-enterprise <https://github.com/openedx/edx-enterprise>`_
     - Django plugin for the LMS that provides enterprise enrollment, learner
       management, and the canonical enterprise customer model.
     - Service integration; exposes the ``migrate_enterprise_catalogs`` management
       command consumed during catalog migrations.
   * - `openedx/enterprise-access <https://github.com/openedx/enterprise-access>`_
     - Manages access policies (subscriptions, learner-credit budgets) for
       enterprise users.
     - Related microservice — coordinates with enterprise-catalog to gate access to
       catalog content.
   * - `openedx/enterprise-subsidy <https://github.com/openedx/enterprise-subsidy>`_
     - Captures and balances enterprise-subsidized transactions (learner-credit
       purchases).
     - Related microservice — supports subsidized enrollment workflows that rely on
       catalog data.
   * - `openedx/enterprise-integrated-channels <https://github.com/openedx/enterprise-integrated-channels>`_
     - Manages integrations with third-party LMS and HR platforms (e.g., SAP,
       Cornerstone, Canvas).
     - Related microservice — sends catalog and completion data to external systems.
   * - `openedx/edx-enterprise-data <https://github.com/openedx/edx-enterprise-data>`_
     - Provides tools and APIs for accessing enterprise analytics and reporting data.
     - Related service — surfaces enterprise enrollment and progress data.
   * - `openedx/course-discovery <https://github.com/openedx/course-discovery>`_
     - Source-of-truth service for consolidated course and program metadata.
     - Primary data source; enterprise-catalog syncs content metadata from this
       service and references its Algolia model constants directly.
   * - `openedx/edx-enterprise-subsidy-client <https://github.com/openedx/edx-enterprise-subsidy-client>`_
     - Python client library for making requests to the enterprise-subsidy service.
     - Shared client library used by services that interact with enterprise-subsidy.

Frontend Applications
~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 35 30 35

   * - Repository
     - Purpose
     - Usage
   * - `openedx/frontend-app-admin-portal <https://github.com/openedx/frontend-app-admin-portal>`_
     - React-based admin portal for enterprise administrators to manage learners,
       catalogs, and budgets.
     - Primary enterprise admin UI; queries enterprise-catalog APIs directly.
   * - `openedx/frontend-app-learner-portal-enterprise <https://github.com/openedx/frontend-app-learner-portal-enterprise>`_
     - React-based learner portal for enterprise learners to browse and enroll in
       catalog content.
     - Enterprise learner-facing UI; consumes catalog search and content APIs.
   * - `openedx/frontend-app-enterprise-public-catalog <https://github.com/openedx/frontend-app-enterprise-public-catalog>`_
     - React micro-frontend for public browsing of enterprise catalogs (the "Explore
       Catalog" experience).
     - Enterprise discovery UI; reads catalog data from enterprise-catalog.
   * - `openedx/frontend-enterprise <https://github.com/openedx/frontend-enterprise>`_
     - Shared React utilities, hooks, and components for enterprise frontend
       applications.
     - Shared frontend library used across enterprise micro-frontends.
   * - `edx/frontend-app-enterprise-checkout <https://github.com/edx/frontend-app-enterprise-checkout>`_
     - React micro-frontend for B2B self-service checkout integrated with Stripe.
     - Enterprise purchasing UI; relies on catalog data to display purchasable
       content.


Shared edX Platform Libraries / Dependencies
--------------------------------------------

These are edX/OpenEdX Python libraries consumed as pip dependencies by
enterprise-catalog.

.. list-table::
   :header-rows: 1
   :widths: 35 25 40

   * - Repository
     - PyPI Package
     - Purpose / Role
   * - `openedx/edx-django-utils <https://github.com/openedx/edx-django-utils>`_
     - ``edx-django-utils``
     - Core Django utilities: monitoring, caching, middleware, and plugin
       infrastructure used throughout the service.
   * - `openedx/edx-drf-extensions <https://github.com/openedx/edx-drf-extensions>`_
     - ``edx-drf-extensions``
     - Django REST Framework extensions: JWT authentication, pagination, and
       permission classes shared across edX services.
   * - `openedx/edx-rbac <https://github.com/openedx/edx-rbac>`_
     - ``edx-rbac``
     - Role-based access control library; drives the enterprise catalog permission
       model (``EnterpriseCatalogRoleAssignment``).
   * - `openedx/edx-toggles <https://github.com/openedx/edx-toggles>`_
     - ``edx-toggles``
     - Feature-flag/toggle library (Waffle integration); used to gate new enterprise
       catalog features.
   * - `openedx/edx-celeryutils <https://github.com/openedx/edx-celeryutils>`_
     - ``edx-celeryutils``
     - Celery task utilities including task deduplication and error handling; used
       extensively for async catalog sync tasks.
   * - `openedx/edx-rest-api-client <https://github.com/openedx/edx-rest-api-client>`_
     - ``edx-rest-api-client``
     - HTTP client for calling other edX REST APIs (discovery, LMS, enterprise);
       used by all ``api_client`` modules in this service.
   * - `openedx/edx-opaque-keys <https://github.com/openedx/edx-opaque-keys>`_
     - ``edx-opaque-keys``
     - Parsing and serialization of opaque course/content keys; used when working
       with course run keys in catalog metadata.
   * - `edx/edx-auth-backends <https://github.com/edx/edx-auth-backends>`_
     - ``edx-auth-backends``
     - Social-auth backends for edX OAuth2 authentication; enables JWT-based login
       against the LMS.
   * - `openedx/ecommerce <https://github.com/openedx/ecommerce>`_
     - N/A (service)
     - Legacy ecommerce service; catalog query logic references ecommerce-defined
       enrollment modes for subsidized purchases.


Development / Tooling Dependencies
------------------------------------

These repositories provide development infrastructure, CI reusable workflows, and
code-quality tooling.

.. list-table::
   :header-rows: 1
   :widths: 35 25 40

   * - Repository
     - Package / Usage
     - Purpose
   * - `openedx/.github <https://github.com/openedx/.github>`_
     - Reusable GitHub Actions workflows
     - Provides shared CI workflows consumed by this repository:
       ``upgrade-python-requirements``, ``commitlint``, ``self-assign-issue``,
       ``add-remove-label-on-comment``, and ``add-depr-ticket-to-depr-board``.
   * - `openedx/devstack <https://github.com/openedx/devstack>`_
     - Docker Compose development environment
     - Required for local development; provisions dependent services (LMS,
       discovery, databases) used during local testing and integration.
   * - `edx/edx-lint <https://github.com/edx/edx-lint>`_
     - ``edx-lint``
     - Manages shared pylint and editorconfig configurations across edX
       repositories; generates and enforces the ``pylintrc`` in this repo.
   * - `edx/edx-django-release-util <https://github.com/edx/edx-django-release-util>`_
     - ``edx-django-release-util``
     - Utilities for release management including DB migration checks run in CI.
   * - `edx/edx-i18n-tools <https://github.com/edx/edx-i18n-tools>`_
     - ``edx-i18n-tools``
     - Internationalization tooling for extracting and compiling translation
       strings (dev dependency).
   * - `openedx/open-edx-proposals <https://github.com/openedx/open-edx-proposals>`_
     - Reference (OEP-37)
     - Open edX proposals that informed architectural decisions in this service;
       referenced in ADR documentation.


Related Platform Services
--------------------------

The following platform-level repositories are not direct pip dependencies but are
integral to the overall enterprise ecosystem at runtime.

.. list-table::
   :header-rows: 1
   :widths: 35 25 40

   * - Repository
     - Role
     - Integration Point
   * - `edx/edx-platform <https://github.com/edx/edx-platform>`_
     - Learning Management System (LMS)
     - Provides JWT authentication, enterprise user data, and enrollment
       capabilities. enterprise-catalog integrates with the LMS for auth and user
       role resolution.
