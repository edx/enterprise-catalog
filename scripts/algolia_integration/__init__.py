"""
Algolia sandbox integration test package.

Exercises the project's ``AlgoliaSearchClient`` wrapper against a real Algolia
sandbox application. Designed to run inside the ``enterprise.catalog.app``
container so the wrapper imports cleanly. See ``scripts/.env.example`` for the
required environment variables and ``docs/how_to/algolia_integration_tests_pattern.md``
for the structural pattern this is modeled on.
"""

__version__ = '0.1.0'
