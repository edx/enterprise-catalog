# Ralph Agent Configuration

## Build Instructions

```bash
# Build the project
echo 'No build command configured'
```

## Test Instructions

You must enter a docker container shell to run tests and linters.
```bash
# Run tests via docker container
docker compose run app bash -c "pytest -c pytest.local.ini path/to/test_file.py::TestClass::test_method"
```

## Run Instructions

```bash
# Start/run the project
echo 'No run command configured'
```

## Notes
- Update this file when build process changes
- Add environment setup instructions as needed
- Include any pre-requisites or dependencies
