# Changelog

## 0.1.0

### Fixes & Improvements
- **Fail fast on auth errors**: `_artifact_exists` now raises `NexusAuthError` on `401`/`403` and errors on unexpected
  statuses instead of silently treating artifacts as already uploaded.
- **Prevent file descriptor leaks**: manage all uploaded file handles via `contextlib.ExitStack`.
- **Avoid redundant POM uploads**: skip POST when all assets already exist on the server; pass explicit
  `maven2.groupId`/`artifactId`/`version` when uploading JAR/classifier assets without a POM asset.
- **Reliability & CLI ergonomics**: reuse `requests.Session` with configurable `--timeout` and HEAD-only retries,
  add `--dry-run`, return non-zero exit codes on failures, support colon-containing passwords (`split(":", 1)`) and
  `NEXUS_AUTH`/`NEXUS_USER`/`NEXUS_PASSWORD` env vars, and use cross-platform `Path.parts` parsing.
- **Packaging & CI**: migrated from `setup.py` to PEP 621 `pyproject.toml`, removed dead `codeball.yml`, upgraded
  GitHub Actions CI (Python 3.10–3.13, `ruff`, `pytest`), and added unit test suite.
