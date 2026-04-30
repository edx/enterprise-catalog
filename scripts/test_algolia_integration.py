#!/usr/bin/env python3
"""
Integration tests for the project's AlgoliaSearchClient wrapper against an
Algolia sandbox application.

Designed to run inside the enterprise.catalog.app container so the project's
Django + algolia_utils imports resolve cleanly.

Usage:
    # Inside the container (recommended path)
    docker exec -e DJANGO_SETTINGS_MODULE=enterprise_catalog.settings.test \\
      enterprise.catalog.app python scripts/test_algolia_integration.py --all

    # List available scenarios without running them
    docker exec enterprise.catalog.app python scripts/test_algolia_integration.py --list

    # Run a single scenario
    docker exec enterprise.catalog.app python scripts/test_algolia_integration.py \\
      --test browse_by_aggregation_key

Configuration is read from scripts/.env (or the path passed via --env). See
scripts/.env.example for the required variables.
"""
import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Set

# Make both the project root (so ``enterprise_catalog`` imports) and the
# scripts/ directory (so ``algolia_integration`` imports) discoverable.
_SCRIPT_DIR = Path(__file__).parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_SCRIPT_DIR))

from algolia_integration.client import bootstrap_django, make_wrapper_client  # noqa: E402
from algolia_integration.config import Config  # noqa: E402
from algolia_integration.test_scenarios import (  # noqa: E402
    TEST_SCENARIOS,
    get_scenario_by_name,
    list_scenario_names,
)


class Colors:
    HEADER = '\033[95m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    ENDC = '\033[0m'


def setup_logging(verbose: bool, debug: bool) -> None:
    if debug:
        level = logging.DEBUG
    elif verbose:
        level = logging.INFO
    else:
        level = logging.WARNING
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )


def print_header(text: str) -> None:
    print(f"\n{Colors.BOLD}{Colors.HEADER}{text}{Colors.ENDC}")
    print('=' * len(text))


def print_success(text: str) -> None:
    print(f"{Colors.GREEN}\u2713 {text}{Colors.ENDC}")


def print_error(text: str) -> None:
    print(f"{Colors.RED}\u2717 {text}{Colors.ENDC}")


def print_warning(text: str) -> None:
    print(f"{Colors.YELLOW}\u26a0 {text}{Colors.ENDC}")


def print_info(text: str) -> None:
    print(f"{Colors.CYAN}\u2139 {text}{Colors.ENDC}")


def resolve_dependencies(test_names: List[str]) -> List[str]:
    """Topologically sort the requested test names by their depends_on chains."""
    resolved: List[str] = []
    seen: Set[str] = set()

    def resolve(name: str, path: Set[str]) -> None:
        if name in path:
            raise ValueError(f"Circular dependency: {' -> '.join(path)} -> {name}")
        if name in seen:
            return
        scenario = get_scenario_by_name(name)
        if not scenario:
            raise ValueError(f"Unknown scenario: {name}")
        if scenario.depends_on:
            for dep in scenario.depends_on:
                resolve(dep, path | {name})
        if name not in seen:
            resolved.append(name)
            seen.add(name)

    for name in test_names:
        resolve(name, set())
    return resolved


def run_scenario(scenario, wrapper, config, verbose: bool) -> Dict:
    print(f"\n{Colors.BOLD}\u25b6 Running: {scenario.name}{Colors.ENDC}")
    print(f"  {scenario.description}")
    try:
        result = scenario.run(wrapper, config)
    except AssertionError as exc:
        print_error(f"{scenario.name}: ASSERTION FAILED")
        print(f"  {exc}")
        return {'status': 'FAIL', 'error': str(exc), 'error_type': 'assertion'}
    except Exception as exc:  # pylint: disable=broad-except
        print_error(f"{scenario.name}: {type(exc).__name__}")
        print(f"  {exc}")
        return {'status': 'FAIL', 'error': str(exc), 'error_type': type(exc).__name__}

    if result.get('status') == 'PASS':
        print_success(f"{scenario.name}: PASSED")
    elif result.get('status') == 'SKIP':
        print_warning(f"{scenario.name}: SKIPPED ({result.get('data', {}).get('reason', '')})")
    else:
        print_error(f"{scenario.name}: {result.get('status')}")

    if verbose and 'data' in result:
        print(f"\n{Colors.CYAN}  Result data:{Colors.ENDC}")
        print(f"  {json.dumps(result['data'], indent=2, default=str)}")

    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Algolia sandbox integration tests',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--env', type=Path, default=None,
                        help='Path to .env file (default: scripts/.env then ./.env)')
    parser.add_argument('--test', action='append', dest='tests',
                        help='Run a specific scenario by name (repeatable)')
    parser.add_argument('--all', action='store_true', help='Run all scenarios')
    parser.add_argument('--list', action='store_true', help='List scenarios and exit')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    parser.add_argument('--debug', action='store_true', help='Debug logging')
    parser.add_argument('--stop-on-fail', action='store_true',
                        help='Stop after the first failed scenario')
    args = parser.parse_args()

    setup_logging(args.verbose, args.debug)

    if args.list:
        print_header('Available Scenarios')
        for scenario in TEST_SCENARIOS:
            deps = f' (depends on: {", ".join(scenario.depends_on)})' if scenario.depends_on else ''
            disabled = '' if scenario.enabled else f' {Colors.YELLOW}[opt-in]{Colors.ENDC}'
            print(f"  \u2022 {Colors.BOLD}{scenario.name}{Colors.ENDC}{deps}{disabled}")
            print(f"    {scenario.description}")
        return 0

    if args.tests:
        invalid = [name for name in args.tests if not get_scenario_by_name(name)]
        if invalid:
            print_error(f"Unknown scenario(s): {', '.join(invalid)}")
            print_info('Use --list to see available scenarios')
            return 1
        names = args.tests
    elif args.all:
        names = list_scenario_names()
    else:
        parser.print_help()
        return 1

    try:
        names = resolve_dependencies(names)
    except ValueError as exc:
        print_error(f"Dependency error: {exc}")
        return 1

    print_header('Configuration')
    try:
        config = Config.from_env(args.env)
        config.validate()
        print_success('Configuration loaded')
        if args.verbose:
            print('\nConfiguration (secrets masked):')
            for key, value in config.mask_secrets().items():
                print(f"  {key}: {value}")
    except FileNotFoundError as exc:
        print_error(f"Env file not found: {exc}")
        print_info('See scripts/.env.example')
        return 1
    except ValueError as exc:
        print_error(f"Configuration error: {exc}")
        return 1

    print_header('Bootstrapping Django + AlgoliaSearchClient')
    try:
        bootstrap_django(config)
        wrapper = make_wrapper_client()
        print_success('Wrapper initialized')
    except Exception as exc:  # pylint: disable=broad-except
        print_error(f"Bootstrap failed: {type(exc).__name__}: {exc}")
        if args.debug:
            raise
        return 1

    print_header('Running Scenarios')
    print(f"Will run {len(names)} scenario(s)")

    results: Dict[str, Dict] = {}
    skipped: List[str] = []

    for name in names:
        scenario = get_scenario_by_name(name)
        if scenario.depends_on:
            failed = [d for d in scenario.depends_on
                      if results.get(d, {}).get('status') != 'PASS']
            if failed:
                print_warning(f"Skipping {name}; dependencies failed: {', '.join(failed)}")
                skipped.append(name)
                continue
        result = run_scenario(scenario, wrapper, config, args.verbose)
        results[name] = result
        if args.stop_on_fail and result.get('status') == 'FAIL':
            print_warning('Stopping due to --stop-on-fail')
            break

    print_header('Summary')
    passed = sum(1 for r in results.values() if r.get('status') == 'PASS')
    skipped_in_run = sum(1 for r in results.values() if r.get('status') == 'SKIP')
    failed = sum(1 for r in results.values() if r.get('status') == 'FAIL')
    total = len(results)
    print(f"\nResults: {passed}/{total} passed, {skipped_in_run} skipped, {failed} failed")

    if failed:
        print(f"\n{Colors.RED}Failed:{Colors.ENDC}")
        for name, result in results.items():
            if result.get('status') == 'FAIL':
                print(f"  \u2022 {name}: {result.get('error_type', 'unknown')}")
                if args.verbose:
                    print(f"    {result.get('error', '')}")

    if skipped:
        print(f"\n{Colors.YELLOW}Skipped (unmet deps):{Colors.ENDC}")
        for name in skipped:
            print(f"  \u2022 {name}")

    if failed:
        return 1
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print_warning('\nInterrupted')
        sys.exit(130)
