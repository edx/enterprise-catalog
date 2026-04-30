"""
Bootstrap Django and construct an initialized ``AlgoliaSearchClient`` for the
integration tests. Also exposes a raw ``SearchClientSync`` for the few
scenarios (search-after-replace, secured-key search) that need to act as an
unprivileged consumer of the index.
"""
import logging
import os

from .config import Config


logger = logging.getLogger(__name__)


def bootstrap_django(config: Config) -> None:
    """
    Configure Django and apply the integration-test ALGOLIA settings.

    Sets ``DJANGO_SETTINGS_MODULE`` to the test settings module if it is not
    already set, calls ``django.setup()``, then overwrites ``settings.ALGOLIA``
    with the credentials from ``config``. Subsequent imports of the project's
    wrapper will read these values.
    """
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'enterprise_catalog.settings.test')

    import django  # noqa: PLC0415  (deferred so DJANGO_SETTINGS_MODULE wins)
    django.setup()

    from django.conf import settings  # noqa: PLC0415

    settings.ALGOLIA = {
        'APPLICATION_ID': config.algolia_application_id,
        'API_KEY': config.algolia_admin_api_key,
        'SEARCH_API_KEY': config.algolia_search_api_key,
        'INDEX_NAME': config.algolia_index_name,
        'REPLICA_INDEX_NAME': config.algolia_replica_index_name,
    }
    logger.debug(
        "Django bootstrapped; ALGOLIA settings applied for app id %s",
        config.algolia_application_id,
    )


def make_wrapper_client():
    """
    Return an initialized project ``AlgoliaSearchClient``.

    Must be called after ``bootstrap_django``.
    """
    from enterprise_catalog.apps.api_client.algolia import AlgoliaSearchClient  # noqa: PLC0415

    client = AlgoliaSearchClient()
    client.init_index()
    if client._client is None:  # pylint: disable=protected-access
        raise RuntimeError(
            "AlgoliaSearchClient.init_index() did not initialize a client. "
            "Check ALGOLIA settings (APPLICATION_ID, API_KEY, INDEX_NAME, REPLICA_INDEX_NAME)."
        )
    return client


def make_raw_search_client(config: Config, api_key: str = None):
    """
    Return a fresh ``SearchClientSync`` for low-level operations.

    Defaults to the admin API key. Pass ``api_key`` to construct a client
    scoped to a different key (e.g. a secured search key).
    """
    from algoliasearch.search.client import SearchClientSync  # noqa: PLC0415

    return SearchClientSync(config.algolia_application_id, api_key or config.algolia_admin_api_key)
