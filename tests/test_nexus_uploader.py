from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import nexus_uploader
from nexus_uploader import Nexus3Uploader, NexusAuthError, parse_auth


def _make_artifact(
    root: Path,
    group_path: str,
    artifact: str,
    version: str,
    *,
    with_jar: bool = True,
    mtime: float | None = None,
) -> Path:
    gav_dir = root / Path(group_path) / artifact / version
    gav_dir.mkdir(parents=True, exist_ok=True)
    pom = gav_dir / f"{artifact}-{version}.pom"
    pom.write_text("<project/>", encoding="utf-8")
    if with_jar:
        jar = gav_dir / f"{artifact}-{version}.jar"
        jar.write_bytes(b"PK\x03\x04jar")
    if mtime is not None:
        os.utime(pom, (mtime, mtime))
    return gav_dir


def _fake_response(status_code: int, content: bytes = b"") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = content
    return resp


def test_parse_auth_handles_colon_in_password_and_env():
    assert parse_auth("admin:p@ss:with:colons", env={}) == ("admin", "p@ss:with:colons")
    assert parse_auth(None, env={"NEXUS_AUTH": "bot:sec:ret"}) == ("bot", "sec:ret")
    assert parse_auth(None, env={"NEXUS_USER": "ci", "NEXUS_PASSWORD": "pw:1"}) == ("ci", "pw:1")
    assert parse_auth(None, env={}) is None
    with pytest.raises(ValueError, match="username:password"):
        parse_auth("invalid-no-colon", env={})


def test_401_raises_auth_error_instead_of_silently_skipping(tmp_path: Path):
    _make_artifact(tmp_path, "com/example", "demo", "1.0.0")
    session = MagicMock()
    session.head.return_value = _fake_response(401)

    uploader = Nexus3Uploader(
        m2_path=tmp_path,
        repo_id="releases",
        repo_url="http://nexus.local:8081",
        auth=("bad", "creds"),
        session=session,
    )
    with pytest.raises(NexusAuthError, match="401"):
        uploader.upload()
    session.post.assert_not_called()


def test_all_assets_exist_skips_pom_post(tmp_path: Path):
    _make_artifact(tmp_path, "com/example", "demo", "1.0.0")
    session = MagicMock()
    session.head.return_value = _fake_response(200)

    uploader = Nexus3Uploader(
        m2_path=tmp_path,
        repo_id="releases",
        repo_url="http://nexus.local:8081",
        session=session,
    )
    summary = uploader.upload()
    assert summary.skipped == 1
    assert summary.uploaded == 0
    assert summary.failed == 0
    session.post.assert_not_called()


def test_existing_pom_uploads_missing_jar_with_gav_coordinates(tmp_path: Path):
    _make_artifact(tmp_path, "com/example", "demo", "1.0.0")
    session = MagicMock()
    # pom -> 200 (exists), jar -> 404 (missing)
    session.head.side_effect = [_fake_response(200), _fake_response(404)]
    session.post.return_value = _fake_response(204)

    uploader = Nexus3Uploader(
        m2_path=tmp_path,
        repo_id="releases",
        repo_url="http://nexus.local:8081",
        session=session,
    )
    summary = uploader.upload()
    assert summary.uploaded == 1
    assert session.post.call_count == 1
    _, kwargs = session.post.call_args
    assert kwargs["data"]["maven2.groupId"] == "com.example"
    assert kwargs["data"]["maven2.artifactId"] == "demo"
    assert kwargs["data"]["maven2.version"] == "1.0.0"
    assert kwargs["data"]["maven2.asset1.extension"] == "jar"
    assert len(kwargs["files"]) == 1
    assert kwargs["files"][0][1][0] == "demo-1.0.0.jar"


def test_file_handles_always_closed_on_success_and_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _make_artifact(tmp_path, "com/example", "demo", "1.0.0")
    opened = 0
    closed = 0
    real_open = Path.open

    class _TrackedFile:
        def __init__(self, f):
            self._f = f

        def __enter__(self):
            return self

        def __exit__(self, *args):
            nonlocal closed
            closed += 1
            return self._f.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self._f, name)

    def tracked_open(self, *args, **kwargs):
        nonlocal opened
        if args and args[0] == "rb":
            opened += 1
            return _TrackedFile(real_open(self, *args, **kwargs))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked_open)

    session = MagicMock()
    session.head.return_value = _fake_response(404)
    session.post.return_value = _fake_response(500, b"boom")

    uploader = Nexus3Uploader(
        m2_path=tmp_path,
        repo_id="releases",
        repo_url="http://nexus.local:8081",
        session=session,
    )
    summary = uploader.upload()
    assert summary.failed == 1
    assert opened == 2  # pom + jar
    assert closed == opened


def test_limit_keeps_latest_k_even_with_equal_mtime(tmp_path: Path):
    _make_artifact(tmp_path, "com/example", "demo", "1.0.0", mtime=1000.0)
    _make_artifact(tmp_path, "com/example", "demo", "2.0.0", mtime=1000.0)
    _make_artifact(tmp_path, "com/example", "demo", "3.0.0", mtime=2000.0)

    uploader = Nexus3Uploader(m2_path=tmp_path, repo_id="r", repo_url="http://x", limit=2)
    total, grouped = uploader._filtered_maven_versions()
    assert total == 2
    versions = {info.version for info in grouped["com.example:demo"]}
    assert "3.0.0" in versions
    assert len(versions) == 2


def test_dry_run_and_main_exit_code_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _make_artifact(tmp_path, "org/acme/sub", "lib", "1.2.3")
    session = MagicMock()
    session.head.return_value = _fake_response(404)
    monkeypatch.setattr(nexus_uploader, "_build_session", lambda: session)

    rc = nexus_uploader.main([str(tmp_path), "--repo-url", "http://nexus.local", "--repo-id", "rel", "--dry-run"])
    assert rc == 0
    session.post.assert_not_called()

    session.post.return_value = _fake_response(500, b"server error")
    rc_fail = nexus_uploader.main([str(tmp_path), "--repo-url", "http://nexus.local", "--repo-id", "rel"])
    assert rc_fail == 1


def test_cli_validation_rejects_bad_limit_missing_dir_and_invalid_regex(tmp_path: Path):
    assert (
        nexus_uploader.main([str(tmp_path), "--repo-url", "http://nexus.local", "--repo-id", "rel", "--limit", "0"])
        == 2
    )
    assert (
        nexus_uploader.main([str(tmp_path / "does-not-exist"), "--repo-url", "http://nexus.local", "--repo-id", "rel"])
        == 2
    )
    assert (
        nexus_uploader.main(
            [str(tmp_path), "--repo-url", "http://nexus.local", "--repo-id", "rel", "--include-group", "[unclosed"]
        )
        == 2
    )
