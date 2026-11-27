# Nexus Uploader

`nexus-uploader` mirrors local Maven M2 repositories to a remote Nexus 3 server with a single command.

## Features

- Uploads POM, JAR, and common classifiers (`sources`, `javadoc`, `no_aop`, `linux-x86_64`, `osx-x86_64`) when present.
- Filters artifacts with `--include-group`, `--include-artifact`, and `--include-version` regexes.
- Uploads only missing assets by default (with `--force-upload` to overwrite and `--dry-run` to preview).
- Keeps only the latest `K` versions per artifact (`--limit K`).
- Supports credentials via `NEXUS_AUTH` or `NEXUS_USER`/`NEXUS_PASSWORD` environment variables (recommended over
  `--auth`) and returns non-zero exit codes on authentication or upload failures.

## Installation

Requires Python 3.10+.

```bash
pip install .
```

For development and tests:

```bash
pip install -e ".[dev]"
ruff check .
pytest -v
```

## Usage

```bash
export NEXUS_USER=admin
export NEXUS_PASSWORD=secret

nexus-uploader ~/.m2/repository \
  --repo-url http://localhost:8081 \
  --repo-id maven-releases \
  --include-group "com\\.example.*" \
  --dry-run
```

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) file for details.
