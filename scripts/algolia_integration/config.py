"""
Configuration for Algolia integration tests.

Loads credentials and the sandbox index names from environment variables (or a
``.env`` file). Real values must never be committed to the repo.
"""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
except ImportError as exc:
    raise Exception(
        "dotenv package unavailable. Install scripts/requirements.txt:\n"
        "    pip install -r scripts/requirements.txt"
    ) from exc


REQUIRED_VARS = (
    'ALGOLIA_APPLICATION_ID',
    'ALGOLIA_ADMIN_API_KEY',
    'ALGOLIA_INDEX_NAME',
    'ALGOLIA_REPLICA_INDEX_NAME',
)


@dataclass
class Config:
    """
    Configuration loaded from env vars / ``.env``.

    The integration tests target a stable sandbox index whose name is supplied
    via ``ALGOLIA_INDEX_NAME``. Tests overwrite the index contents on each run
    (``replace_all_objects``), so the index can be reused safely across runs as
    long as it is not pointed at production data.
    """
    algolia_application_id: str
    algolia_admin_api_key: str
    algolia_index_name: str
    algolia_replica_index_name: str

    # Optional: required only by the generate_secured_api_key scenario.
    algolia_search_api_key: Optional[str] = None

    # Optional: enterprise UUID used to seed sample records' aggregation_key.
    # Defaults to a fixed UUID-like string so runs are reproducible.
    sample_aggregation_key: str = 'integration-test-aggregation-key'

    @classmethod
    def from_env(cls, env_file: Optional[Path] = None) -> 'Config':
        """
        Load configuration from environment variables.

        Args:
            env_file: Optional path to a ``.env`` file. Defaults to ``scripts/.env``
                if present, then ``./.env``.

        Raises:
            ValueError: If required environment variables are missing.
            FileNotFoundError: If ``env_file`` is given but does not exist.
        """
        if env_file:
            if not env_file.exists():
                raise FileNotFoundError(f"Environment file not found: {env_file}")
            load_dotenv(env_file)
        else:
            for candidate in (Path('scripts/.env'), Path('.env')):
                if candidate.exists():
                    load_dotenv(candidate)
                    break

        missing = [var for var in REQUIRED_VARS if not os.getenv(var)]
        if missing:
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing)}\n"
                f"Set them in your shell or in scripts/.env. "
                f"See scripts/.env.example for reference."
            )

        return cls(
            algolia_application_id=os.environ['ALGOLIA_APPLICATION_ID'],
            algolia_admin_api_key=os.environ['ALGOLIA_ADMIN_API_KEY'],
            algolia_index_name=os.environ['ALGOLIA_INDEX_NAME'],
            algolia_replica_index_name=os.environ['ALGOLIA_REPLICA_INDEX_NAME'],
            algolia_search_api_key=os.getenv('ALGOLIA_SEARCH_API_KEY'),
            sample_aggregation_key=os.getenv(
                'SAMPLE_AGGREGATION_KEY',
                'integration-test-aggregation-key',
            ),
        )

    def validate(self) -> None:
        """
        Validate basic shape of configured values.
        """
        if 'prod' in self.algolia_index_name.lower():
            raise ValueError(
                f"Refusing to run against an index whose name contains 'prod': "
                f"{self.algolia_index_name}"
            )
        if self.algolia_index_name == self.algolia_replica_index_name:
            raise ValueError(
                "ALGOLIA_INDEX_NAME and ALGOLIA_REPLICA_INDEX_NAME must differ."
            )

    def mask_secrets(self) -> dict:
        """
        Return a dict safe for printing — credential fields show only presence, never value.
        """
        def present(val: Optional[str]) -> str:
            return '<set>' if val else '<not set>'

        return {
            'algolia_application_id': present(self.algolia_application_id),
            'algolia_admin_api_key': present(self.algolia_admin_api_key),
            'algolia_search_api_key': present(self.algolia_search_api_key),
            'algolia_index_name': self.algolia_index_name,
            'algolia_replica_index_name': self.algolia_replica_index_name,
            'sample_aggregation_key': self.sample_aggregation_key,
        }
