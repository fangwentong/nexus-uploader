#!/usr/bin/env python3
"""Mirror local Maven M2 repositories to a remote Nexus 3 server."""

from __future__ import annotations

import argparse
import heapq
import logging
import os
import re
import sys
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util.retry import Retry

DEFAULT_CLASSIFIERS = ("sources", "javadoc", "no_aop", "noaop", "linux-x86_64", "osx-x86_64")
DEFAULT_TYPES = ("jar", "exe")


class NexusUploaderError(RuntimeError):
    """Base error for Nexus upload failures."""


class NexusAuthError(NexusUploaderError):
    """Raised when Nexus rejects credentials (401/403)."""


@dataclass
class MavenInfo:
    """Metadata for a single Maven GAV directory found in a local M2 repo."""

    path: Path
    pom: str
    group_id: str
    artifact_id: str
    version: str
    mtime: float
    jar: str | None = None
    classifiers: dict[str, list[str]] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.group_id}:{self.artifact_id}:{self.version} ({self.path})"


@dataclass
class UploadSummary:
    uploaded: int = 0
    would_upload: int = 0
    skipped: int = 0
    failed: int = 0


def parse_auth(
    auth_arg: str | None,
    env: dict[str, str] | os._Environ[str] | None = None,
) -> tuple[str, str] | None:
    """Resolve Nexus basic-auth credentials from CLI arg or environment variables."""
    source = os.environ if env is None else env
    raw = auth_arg if auth_arg is not None else source.get("NEXUS_AUTH")
    if raw is not None:
        if ":" not in raw:
            raise ValueError("auth credentials must be in 'username:password' format")
        user, password = raw.split(":", 1)
        return (user, password)
    user = source.get("NEXUS_USER")
    password = source.get("NEXUS_PASSWORD")
    if user is not None and password is not None:
        return (user, password)
    return None


def _build_session() -> requests.Session:
    session = requests.Session()
    # Retry idempotent HEAD existence probes on transient gateway errors; never auto-retry POST.
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(502, 503, 504),
        allowed_methods=frozenset({"HEAD"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


class BaseNexusUploader:
    def __init__(
        self,
        m2_path: str | Path = "~/.m2/repository",
        repo_id: str = "",
        auth: tuple[str, str] | None = None,
        include_artifact_pattern: re.Pattern[str] | None = None,
        include_group_pattern: re.Pattern[str] | None = None,
        include_version_pattern: re.Pattern[str] | None = None,
        force_upload: bool = False,
        repo_url: str = "",
        classifiers: Iterable[str] | None = None,
        types: Iterable[str] | None = None,
        limit: int | None = None,
        timeout: float = 30.0,
        dry_run: bool = False,
        session: requests.Session | None = None,
    ):
        self.m2_path = Path(m2_path).expanduser()
        self.repo_id = repo_id
        self.auth = HTTPBasicAuth(*auth) if auth is not None else None
        self.include_artifact_pattern = include_artifact_pattern
        self.include_group_pattern = include_group_pattern
        self.include_version_pattern = include_version_pattern
        self.force_upload = force_upload
        self.repo_url = repo_url.rstrip("/")
        self.classifiers = sorted(set(classifiers)) if classifiers is not None else []
        self.types = sorted(set(types)) if types is not None else []
        if limit is not None and limit < 1:
            raise ValueError("limit must be >= 1")
        self.limit = limit if limit is not None else sys.maxsize
        self.timeout = timeout
        self.dry_run = dry_run
        self._owns_session = session is None
        self.session = session if session is not None else _build_session()

    def close(self) -> None:
        if self._owns_session:
            self.session.close()

    def __enter__(self) -> BaseNexusUploader:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @staticmethod
    def list_files(root: Path, suffix: str = ".pom") -> Iterator[Path]:
        for p in sorted(root.rglob(f"*{suffix}")):
            if p.is_file():
                yield p

    def m2_maven_info(self) -> Iterator[MavenInfo]:
        """Walk an on-disk M2 repository yielding MavenInfo entries."""
        for pom in self.list_files(self.m2_path, ".pom"):
            rpath_parts = pom.relative_to(self.m2_path).parts
            if len(rpath_parts) < 4:
                logging.warning("Skipping non-GAV pom path: %s", pom)
                continue
            info = MavenInfo(
                path=pom.parent,
                pom=pom.name,
                group_id=".".join(rpath_parts[:-3]),
                artifact_id=rpath_parts[-3],
                version=rpath_parts[-2],
                mtime=pom.stat().st_mtime,
            )
            jarfile = pom.with_suffix(".jar")
            if jarfile.is_file():
                info.jar = jarfile.name

            for classifier in self.classifiers:
                files: list[str] = []
                for filetype in self.types:
                    classifier_file = jarfile.with_name(f"{jarfile.stem}-{classifier}.{filetype}")
                    if classifier_file.is_file():
                        logging.info("Found %s: %s", filetype, classifier_file)
                        files.append(classifier_file.name)
                if files:
                    info.classifiers[classifier] = files

            yield info

    def _upload_single(self, maven_info: MavenInfo) -> str:
        raise NotImplementedError

    def _filtered_maven_versions(self) -> tuple[int, dict[str, list[MavenInfo]]]:
        total = 0
        seq = 0
        artifact_heaps: dict[str, list[tuple[float, int, MavenInfo]]] = defaultdict(list)

        for maven_info in self.m2_maven_info():
            if self.include_group_pattern and not self.include_group_pattern.search(maven_info.group_id):
                continue
            if self.include_artifact_pattern and not self.include_artifact_pattern.search(maven_info.artifact_id):
                continue
            if self.include_version_pattern and not self.include_version_pattern.search(maven_info.version):
                continue

            key = f"{maven_info.group_id}:{maven_info.artifact_id}"
            minheap = artifact_heaps[key]
            seq += 1
            entry = (maven_info.mtime, seq, maven_info)
            if len(minheap) >= self.limit:
                if entry < minheap[0]:
                    discarded = maven_info
                else:
                    discarded = heapq.heapreplace(minheap, entry)[2]
                logging.info("Discard: %s due to version limit (%d)", discarded, self.limit)
            else:
                heapq.heappush(minheap, entry)
                total += 1

        grouped = {
            k: [item[2] for item in sorted(heap, key=lambda x: (x[0], x[1]))]
            for k, heap in artifact_heaps.items()
        }
        return total, grouped

    def upload(self) -> UploadSummary:
        logging.info("Uploading content from [%s] to %s repo on %s", self.m2_path, self.repo_id, self.repo_url)
        total, artifact_versions = self._filtered_maven_versions()
        summary = UploadSummary()

        current = 0
        for versions in artifact_versions.values():
            for maven_info in versions:
                current += 1
                logging.info("Processing: %s (%d/%d)", maven_info, current, total)
                try:
                    outcome = self._upload_single(maven_info)
                except NexusAuthError:
                    raise
                except Exception as exc:
                    logging.error("Failed uploading %s: %s", maven_info, exc)
                    summary.failed += 1
                    continue

                if outcome == "uploaded":
                    summary.uploaded += 1
                elif outcome == "would_upload":
                    summary.would_upload += 1
                else:
                    summary.skipped += 1

        logging.info(
            "Done: uploaded=%d would_upload=%d skipped=%d failed=%d",
            summary.uploaded,
            summary.would_upload,
            summary.skipped,
            summary.failed,
        )
        return summary


class Nexus3Uploader(BaseNexusUploader):
    """Uploads local M2 repositories to a Nexus 3 Server."""

    def _nexus_post_form(
        self,
        minfo: MavenInfo,
        files: list[tuple[str, tuple[str, object]]],
        form_params: dict[str, str],
    ) -> None:
        encoded_repo = quote(self.repo_id, safe="")
        url = f"{self.repo_url}/service/rest/v1/components?repository={encoded_repo}"
        resp = self.session.post(url, files=files, auth=self.auth, data=form_params, timeout=self.timeout)
        if resp.status_code in (401, 403):
            raise NexusAuthError(f"Authentication failed ({resp.status_code}) uploading {minfo} to {url}")
        if resp.status_code > 299:
            body = resp.content.decode("utf-8", errors="replace")
            raise NexusUploaderError(f"Error communicating with Nexus! url={url}, code={resp.status_code}, msg={body}")
        logging.info("Successfully uploaded: %s, %s", minfo, [item[1][0] for item in files])

    def _artifact_exists(self, artifact_path: str) -> bool:
        encoded_repo = quote(self.repo_id, safe="")
        url = f"{self.repo_url}/repository/{encoded_repo}/{artifact_path}"
        logging.info("Checking for: %s", url)
        resp = self.session.head(url, auth=self.auth, timeout=self.timeout)
        if resp.status_code == 404:
            return False
        if resp.status_code == 200:
            logging.info("Will *NOT* upload %s, artifact already exists", artifact_path)
            return True
        if resp.status_code in (401, 403):
            raise NexusAuthError(f"Authentication failed ({resp.status_code}) checking {url}")
        raise NexusUploaderError(f"Unexpected status {resp.status_code} checking {url}")

    @staticmethod
    def artifact_path(minfo: MavenInfo, filename: str) -> str:
        m2_path = f"{minfo.group_id.replace('.', '/')}/{minfo.artifact_id}/{minfo.version}"
        return f"{m2_path}/{filename}"

    def _upload_single(self, maven_info: MavenInfo) -> str:
        def need_upload(filename: str) -> bool:
            return self.force_upload or not self._artifact_exists(self.artifact_path(maven_info, filename))

        assets_to_upload: list[tuple[str, str, str | None]] = []
        include_pom = need_upload(maven_info.pom)
        if include_pom:
            assets_to_upload.append((maven_info.pom, "pom", None))

        if maven_info.jar is not None and need_upload(maven_info.jar):
            assets_to_upload.append((maven_info.jar, "jar", None))

        for classifier in self.classifiers:
            if classifier not in maven_info.classifiers:
                continue
            for filename in maven_info.classifiers[classifier]:
                if need_upload(filename):
                    ext = filename.rsplit(".", 1)[-1]
                    assets_to_upload.append((filename, ext, classifier))

        if not assets_to_upload:
            logging.info("Skipping %s: all assets already exist on server", maven_info)
            return "skipped"

        if self.dry_run:
            logging.info("[DRY-RUN] Would upload %s assets: %s", maven_info, [a[0] for a in assets_to_upload])
            return "would_upload"

        payload: dict[str, str] = {"maven2.generate-pom": "false"}
        if not include_pom:
            # When uploading assets without a POM asset, Nexus 3 requires explicit Maven coordinates.
            payload["maven2.groupId"] = maven_info.group_id
            payload["maven2.artifactId"] = maven_info.artifact_id
            payload["maven2.version"] = maven_info.version

        with ExitStack() as stack:
            files: list[tuple[str, tuple[str, object]]] = []
            for idx, (filename, ext, classifier) in enumerate(assets_to_upload, start=1):
                fullpath = maven_info.path / filename
                fh = stack.enter_context(fullpath.open("rb"))
                files.append((f"maven2.asset{idx}", (filename, fh)))
                payload[f"maven2.asset{idx}.extension"] = ext
                if classifier is not None:
                    payload[f"maven2.asset{idx}.classifier"] = classifier

            self._nexus_post_form(maven_info, files=files, form_params=payload)

        return "uploaded"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Easily upload multiple artifacts to a remote Nexus 3 server.")
    parser.add_argument("repodirs", type=str, nargs="+", help="list of local m2 repodirs to scan")
    parser.add_argument("--repo-url", type=str, required=True, help="Nexus repo URL (e.g. http://localhost:8081)")
    parser.add_argument("--repo-id", type=str, required=True, help="Repository ID in Nexus")
    parser.add_argument(
        "--auth",
        type=str,
        help="Basic auth credentials as username:password (or set NEXUS_AUTH / NEXUS_USER + NEXUS_PASSWORD)",
    )
    parser.add_argument("--include-artifact", "-ia", type=str, metavar="REGEX", help="regex to apply to artifactId")
    parser.add_argument("--include-group", "-ig", type=str, metavar="REGEX", help="regex to apply to groupId")
    parser.add_argument("--include-version", "-iv", type=str, metavar="REGEX", help="regex to apply to version")
    parser.add_argument("--force-upload", "-F", action="store_true", help="upload even if artifact already exists")
    parser.add_argument("--dry-run", action="store_true", help="preview uploads without sending POST requests")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP request timeout in seconds (default: 30)")
    parser.add_argument(
        "--limit",
        "-l",
        type=int,
        help="only upload the latest k versions (by mtime) for each artifact",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)

    try:
        auth = parse_auth(args.auth)
    except ValueError as exc:
        logging.error("Invalid credentials: %s", exc)
        return 2

    if args.limit is not None and args.limit < 1:
        logging.error("--limit must be >= 1, got %d", args.limit)
        return 2

    try:
        include_artifact = re.compile(args.include_artifact) if args.include_artifact else None
        include_group = re.compile(args.include_group) if args.include_group else None
        include_version = re.compile(args.include_version) if args.include_version else None
    except re.error as exc:
        logging.error("Invalid regex pattern: %s", exc)
        return 2

    resolved_dirs: list[Path] = []
    for repodir in args.repodirs:
        p = Path(repodir).expanduser()
        if not p.is_dir():
            logging.error("Repository directory does not exist or is not a directory: %s", p)
            return 2
        resolved_dirs.append(p)

    total_failed = 0
    for repodir in resolved_dirs:
        try:
            with Nexus3Uploader(
                m2_path=repodir,
                repo_id=args.repo_id,
                auth=auth,
                include_artifact_pattern=include_artifact,
                include_group_pattern=include_group,
                include_version_pattern=include_version,
                force_upload=args.force_upload,
                repo_url=args.repo_url,
                classifiers=DEFAULT_CLASSIFIERS,
                types=DEFAULT_TYPES,
                limit=args.limit,
                timeout=args.timeout,
                dry_run=args.dry_run,
            ) as uploader:
                summary = uploader.upload()
                total_failed += summary.failed
        except NexusAuthError as exc:
            logging.error("%s", exc)
            return 1

    return 1 if total_failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
