# Pattern: scripts/<name>_integration (modeled on enterprise-access)

This is the structure used by `~/code/enterprise-access/scripts/billing_integration`. We're going to mirror it for Algolia sandbox integration tests.

## Layout

```
scripts/
├── .env.example              # documents every env var the runner needs
├── .env                      # gitignored, holds real credentials locally
├── requirements.txt          # requests + python-dotenv (and SDK we're testing)
├── test_<name>_integration.py  # runner: argparse, dependency resolver, colored output
└── <name>_integration/       # supporting package
    ├── __init__.py
    ├── config.py             # @dataclass Config + from_env() + validate() + mask_secrets()
    ├── auth.py               # token acquisition / caching (skipped for Algolia — API key auth)
    ├── client.py             # thin wrapper around the SDK or HTTP API under test
    └── test_scenarios.py     # @dataclass TestScenario + TEST_SCENARIOS registry
```

## How the pieces fit together

**`config.py`** — single `Config` dataclass loaded via `Config.from_env(env_file)`. Required vs optional vars are listed explicitly; `from_env` raises if any required var is missing. `mask_secrets()` is used by the runner's `--verbose` mode so credentials don't leak into stdout. All real values come from `.env` (loaded via `python-dotenv`) or the process environment.

**`auth.py`** — a `JWTAuthenticator` for the billing case (OAuth client-credentials → JWT, with caching and a 60s pre-expiry buffer). For Algolia we don't need this: the SDK accepts the application ID and admin API key directly.

**`client.py`** — high-level wrapper around the API under test. For billing it wraps HTTP calls (`requests`); for Algolia it'll wrap `SearchClientSync` (or instantiate the project's own `AlgoliaSearchClient`).

**`test_scenarios.py`** — each test is a function `(client, config) -> {'status': 'PASS', 'data': ...}` that uses `assert` for failures. Functions are registered as `TestScenario(name, description, run, depends_on=[...], skip_on_error=False)` in a `TEST_SCENARIOS` list. Helpers `get_scenario_by_name` and `list_scenario_names` round it out.

**Runner (`test_<name>_integration.py`)**:

- argparse flags: `--all`, `--test NAME` (repeatable), `--list`, `--env PATH`, `--verbose`, `--debug`, `--stop-on-fail`.
- Resolves `depends_on` topologically before execution; skips a scenario whose dependency failed (the skipped run is reported separately from a hard fail).
- Catches `AssertionError` (logical failure) and `Exception` (unexpected error) per scenario, so one failure doesn't kill the run unless `--stop-on-fail` is set.
- Colored terminal output via a small `Colors` class (no extra deps).
- Exit code `0` if everything passed, `1` if anything failed, `130` on Ctrl-C.

## Why the pattern works for sandbox-style tests

- **Secrets stay in `.env`** — never on the command line, never in code, masked in logs.
- **Each scenario is an independent function** — easy to add new ones, easy to run a single scenario when iterating.
- **Dependency declarations** make multi-step flows (create-then-verify, update-then-cache-bust) safe to run individually.
- **No test framework lock-in** — plain Python + requests/SDK, no pytest/Django machinery to wire up.

## What to keep, drop, or change for Algolia

- **Drop:** `auth.py`. Algolia auth is just the admin API key.
- **Keep:** `config.py`, `client.py`, `test_scenarios.py`, the runner skeleton.
- **Change:** `config.py` carries `ALGOLIA_APPLICATION_ID`, `ALGOLIA_ADMIN_API_KEY`, `ALGOLIA_SEARCH_API_KEY`, and the names of the stable sandbox indices we own (`ALGOLIA_INDEX_NAME`, `ALGOLIA_REPLICA_INDEX_NAME`). `client.py` bootstraps Django and instantiates the project's `AlgoliaSearchClient` so the wrapper code path is exercised end-to-end; a few low-level scenarios construct a raw `SearchClientSync` directly. Each scenario verifies one operation against the sandbox index. Index contents are overwritten on each run, so the same index is safe to reuse.

## To run against your Algolia sandbox account

### One-time setup

1. Create or pick a sandbox Algolia application (never production). Provision a primary and replica index inside it.
2. Copy the example env file and fill it in:
   ```
   cp scripts/.env.example scripts/.env
   ```
   Required vars in `scripts/.env`: `ALGOLIA_APPLICATION_ID`, `ALGOLIA_ADMIN_API_KEY`, `ALGOLIA_INDEX_NAME`, `ALGOLIA_REPLICA_INDEX_NAME`. `ALGOLIA_SEARCH_API_KEY` is optional and only needed for the `generate_secured_api_key` scenario. The runner refuses to start if `ALGOLIA_INDEX_NAME` contains the substring `prod` as a guardrail.
3. Install the runner's extra dependencies inside the catalog container (only `python-dotenv` — `algoliasearch` is already in the project requirements):
   ```
   docker exec enterprise.catalog.app pip install -r scripts/requirements.txt
   ```
   This pip install lives only in the running container layer; if the container is rebuilt, repeat the step.

### Running scenarios

List the available scenarios:

```
docker exec enterprise.catalog.app python scripts/test_algolia_integration.py --list
```

Run the full default suite (everything except scenarios marked `[opt-in]`):

```
docker exec enterprise.catalog.app python scripts/test_algolia_integration.py --all -v
```

Run a single scenario (skips dependency resolution if the scenario has none, or runs its dependency chain first):

```
docker exec enterprise.catalog.app python scripts/test_algolia_integration.py \
  --test browse_by_aggregation_key -v
```

Useful flags: `--verbose` / `-v` prints scenario result data and the masked configuration; `--debug` enables debug-level Python logging; `--stop-on-fail` halts after the first failure; `--env PATH` overrides the default `scripts/.env` lookup.

### Default scenarios (run by `--all`)

| Scenario | What it verifies |
| --- | --- |
| `init_and_index_exists` | Wrapper can talk to Algolia and both sandbox indices exist. |
| `set_index_settings` | `set_settings` works for primary and replica (replica gets `customRanking`, the only setting virtual replicas accept here). |
| `seed_sample_records` | Five sample records land in the sandbox index, verified via `get_objects` polling. |
| `browse_by_aggregation_key` | `get_all_objects_associated_with_aggregation_key` returns every seeded objectID via the v4 aggregator-callback path. |
| `delete_objects` | `remove_objects` deletes the requested ids; remaining ids are still browsable. |
| `list_indices_shape` | `list_indices_with_http_info()` returns parseable JSON with the expected camelCase keys. (See production-bug note below.) |
| `create_and_delete_temporary_index` | `delete_index` removes a tmp index this scenario creates. |
| `generate_secured_api_key` | Wrapper generates a restricted secured key; the key is usable for a search and the restriction filter is enforced. Skipped if `ALGOLIA_SEARCH_API_KEY` is not set. |

### Opt-in scenarios

`replace_all_objects_and_search` exercises the wrapper's `replace_all_objects` path. It is excluded from `--all` because the v4 SDK's `wait_for_task` helper has a fixed retry budget that busy sandbox apps blow past, even when the underlying copy/index/swap eventually completes server-side. Run it explicitly when you want to test that path:

```
docker exec enterprise.catalog.app python scripts/test_algolia_integration.py \
  --test replace_all_objects_and_search -v
```

### Notes on sandbox timing

Algolia's task queue can be slow on busy sandbox apps. Several scenarios poll until the operation is observable rather than relying on the SDK's `wait_for_task`:

- `seed_sample_records` polls `get_objects` for up to 5 minutes.
- `delete_objects` polls `get_objects` for up to 3 minutes.
- `create_and_delete_temporary_index` polls `index_exists` for up to 1 minute on each side.
- `generate_secured_api_key` polls `search_single_index` for up to 2 minutes (the secured-key search depends on `attributesForFaceting` having propagated, which can lag the writes).

If a scenario fails with "not visible after Ns", it usually means the sandbox queue was congested. Wait a minute and re-run the scenario, or run with `--debug` to see the polling cadence.

### Production bug surfaced by these scenarios

While running `list_indices_shape` against a real sandbox app, we discovered the v4 SDK's typed `list_indices()` raises `pydantic.ValidationError` because the Algolia API regularly omits fields (`lastBuildTimeS`, `numberOfPendingTasks`, `pendingTask`) that the SDK's `FetchedIndex` model declares as required. The production `_get_all_indices` helper in `enterprise_catalog/apps/api/tasks.py` was rewritten to use `list_indices_with_http_info()` and parse the raw JSON body directly, sidestepping the strict deserialization. The integration scenario takes the same path and this is one reason to keep it green.

### Pytest collection

`scripts/` is excluded from pytest discovery in both `pytest.ini` and `pytest.local.ini` (`norecursedirs = .* docs requirements scripts ...`) so the runner's `test_*.py` filenames don't get collected as unit tests.
