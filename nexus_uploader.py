#!/usr/bin/env python3

"""
nexus-uploader.py
Allows mirroring local M2 repositories to a remote Nexus server with a single command.
Supports:
   - uploading of common classifiers (sources, javadocs) if available
   - using regex include pattern for artifactIds/groupIds
   - recursively processing local repo, just point to the root
   - only upload artifacts missing on server (with option to force if needed)
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import Dict, Pattern, List, Tuple, Set

import requests
from requests.auth import HTTPBasicAuth
import heapq
from collections import defaultdict
from functools import total_ordering


@total_ordering
class MavenInfo:
    """ a simple object to hold maven info. """
    path: Path
    pom: str
    artifact_id: str
    group_id: str
    version: str
    jar: str
    classifiers: Dict[str, List[str]]
    mtime: float

    def __init__(self, path=None, pom=None, artifact_id=None, group_id=None, version=None, jar=None, mtime=None):
        self.path = path
        self.pom = pom
        self.artifact_id = artifact_id
        self.group_id = group_id
        self.version = version
        self.jar = jar
        self.classifiers = {}
        self.mtime = mtime

    def __str__(self):
        return f'{self.group_id}:{self.artifact_id}:{self.version} ({self.path})'

    def __repr__(self):
        return f'MavenInfo({self.path}, {self.group_id}, {self.artifact_id}, {self.version}, {self.mtime})'

    def __lt__(self, other): return self.mtime < other.mtime

    def __eq__(self, other): return self.mtime == other.mtime


class BaseNexusUploader:
    m2_path: Path
    repo_id: str
    auth: HTTPBasicAuth
    include_artifact_pattern: Pattern
    include_group_pattern: Pattern
    include_version_pattern: Pattern
    force_upload: bool
    repo_url: str
    classifiers: Set[str]
    types: Set[str]
    limit: int

    def __init__(self, m2_path='~/.m2/repository', repo_id: str = None, auth: Tuple[str] = None,
                 include_artifact_pattern: Pattern = None, include_group_pattern: Pattern = None,
                 include_version_pattern: Pattern = None, force_upload: bool = False,
                 repo_url: str = None, classifiers: List[str] = None, types: List[str] = None,
                 limit: int = sys.maxsize):
        self.m2_path = Path(m2_path).expanduser()
        self.repo_id = repo_id
        self.auth = HTTPBasicAuth(*auth) if auth is not None else None
        self.include_artifact_pattern = include_artifact_pattern
        self.include_group_pattern = include_group_pattern
        self.include_version_pattern = include_version_pattern
        self.force_upload = force_upload
        self.repo_url = repo_url
        self.classifiers = set(classifiers) if classifiers is not None else set()
        self.types = set(types) if types is not None else set()
        self.limit = limit

    @staticmethod
    def list_files(root, file_filter=lambda x: True):
        """ list all files matching a filter in a given dir with optional recursion. """
        for p in root.glob('**/*'):
            if p.is_file() and file_filter(p.name):
                yield p

    def m2_maven_info(self):
        """ walks an on-disk m2 repo yielding a dict of pom/gav/jar info. """
        for pom in self.list_files(self.m2_path, lambda x: x.endswith(".pom")):
            rpath_parts = list(filter(bool, str(pom.relative_to(self.m2_path)).split('/')))
            info = MavenInfo(
                path=pom.parent,
                pom=pom.name,
                group_id='.'.join(rpath_parts[:-3]),
                artifact_id=rpath_parts[-3],
                version=rpath_parts[-2],
                mtime=pom.stat().st_mtime,
            )
            # check for jar
            jarfile = pom.with_suffix('.jar')
            if jarfile.is_file():
                info.jar = jarfile.name

            # classifiers: 'sources', 'javadoc', 'no_aop', 'noaop', 'linux-x86_64'
            for classifier in self.classifiers:
                files = []
                for filetype in self.types:
                    classifier_file = jarfile.with_name(jarfile.stem + f'-{classifier}.{filetype}')
                    if classifier_file.is_file():
                        logging.info(f'Found {filetype}: {classifier_file}')
                        files.append(classifier_file.name)
                if len(files) > 0:
                    info.classifiers[classifier] = files

            yield info

    def _upload_single(self, maven_info):
        raise NotImplementedError

    def _filtered_maven_versions(self) -> Tuple[int, Dict[str, List[MavenInfo]]]:
        """filter maven versions by group, artifact, version patterns and version limit for each artifact"""
        total = 0
        artifact_versions = defaultdict(list)
        for maven_info in self.m2_maven_info():
            # only include specific groups if group regex supplied
            if self.include_group_pattern and not self.include_group_pattern.search(maven_info.group_id):
                continue

            # only include specific artifact if artifact regex supplied
            if self.include_artifact_pattern and not self.include_artifact_pattern.search(maven_info.artifact_id):
                continue

            # only include specific version if version regex supplied
            if self.include_version_pattern and not self.include_version_pattern.search(maven_info.version):
                continue

            maven_artifact_key = f'{maven_info.group_id}:{maven_info.artifact_id}'

            minheap = artifact_versions[maven_artifact_key]
            # use min heap to manipulate the latest k versions,
            if len(minheap) >= self.limit:
                discarded = maven_info if maven_info < minheap[0] else heapq.heapreplace(minheap, maven_info)
                logging.info(f'Discard: {discarded} due to version limits')
            else:
                heapq.heappush(minheap, maven_info)
                total += 1
        return total, artifact_versions

    def upload(self) -> None:
        """uploads all artifacts in a given m2 repo to a remote nexus repo."""
        logging.info(f'Repo dirs: {self.m2_path}')

        logging.info(f'Uploading content from [{self.m2_path}] to {self.repo_id} repo on {self.repo_url}')
        total, artifact_versions = self._filtered_maven_versions()

        current = 0
        for versions in artifact_versions.values():
            for maven_info in versions:
                current += 1
                logging.info(f'\nProcessing: {maven_info}, {current}/{total}')
                self._upload_single(maven_info)


class Nexus3Uploader(BaseNexusUploader):
    """
    Uploads local m2 repositories to a Nexus 3 Server
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def _nexus_post_form(self, minfo: MavenInfo, files, form_params):
        url = '%s/%s?repository=%s' % (self.repo_url, 'service/rest/v1/components', self.repo_id)
        req = requests.post(url, files=files, auth=self.auth, data=form_params)
        if req.status_code > 299:
            logging.error('Error communicating with Nexus! url=' + url + ', code=' + str(
                req.status_code) + ', msg=' + req.content.decode('utf-8'))
        else:
            logging.info('Successfully uploaded: %s, %s', minfo, [file[1][0] for file in files])

    def _artifact_exists(self, artifact_path):
        url = '%s/repository/%s/%s' % (self.repo_url, self.repo_id, artifact_path)
        logging.info('Checking for: %s', url)
        req = requests.head(url, auth=self.auth)
        if req.status_code == 404:
            return False
        if req.status_code == 200:
            logging.info('Will *NOT* upload %s, artifact already exists', artifact_path)
            return True
        else:
            # for safety, return true if we cannot determine if file exists
            logging.warning('Error checking status of: %s', artifact_path)
            return True

    @staticmethod
    def artifact_path(minfo, filename):
        m2_path = '%s/%s/%s' % (minfo.group_id.replace('.', '/'), minfo.artifact_id, minfo.version)
        return '%s/%s' % (m2_path, filename)

    def _upload_single(self, maven_info: MavenInfo):
        def encode_file(basename, num):
            fullpath = maven_info.path / basename
            return f'maven2.asset{num}', (basename, fullpath.open('rb'))

        def need_upload(filename):
            return self.force_upload or not self._artifact_exists(self.artifact_path(maven_info, filename))

        # append pom
        files = [encode_file(maven_info.pom, 1)]
        payload = {
            'maven2.generate-pom': 'false',
            'maven2.asset1.extension': 'pom',
        }

        # append extension params
        extension_num = 2

        if maven_info.jar is not None and need_upload(maven_info.jar):
            files.append(encode_file(maven_info.jar, extension_num))
            payload.update({'maven2.asset2.extension': 'jar'})
            extension_num += 1

        for classifier in self.classifiers:
            if classifier not in maven_info.classifiers:
                continue
            for filename in maven_info.classifiers[classifier]:
                if need_upload(filename):
                    files.append(encode_file(filename, extension_num))
                    payload.update({f'maven2.asset{extension_num}.extension': filename.split('.')[-1]})
                    payload.update({f'maven2.asset{extension_num}.classifier': classifier})
                    extension_num += 1
                    logging.info(f'Appended file {filename} num = {extension_num - 1}')

        self._nexus_post_form(maven_info, files=files, form_params=payload)


def main():
    parser = argparse.ArgumentParser(description='Easily upload multiple artifacts to a remote Nexus server.')
    parser.add_argument('repodirs', type=str, nargs='+', help='list of repodirs to scan')
    parser.add_argument('--repo-url', type=str, required=True, help='Nexus repo URL (e.g. http://localhost:8081)')
    parser.add_argument('--repo-id', type=str, help='Repository ID (in Nexus) to u/l to.', required=True)
    parser.add_argument('--auth', type=str, help='basicauth credentials in Nexus with the form of username:password.')
    parser.add_argument('--include-artifact', '-ia', type=str, metavar='REGEX', help='regex to apply to artifactId')
    parser.add_argument('--include-group', '-ig', type=str, metavar='REGEX', help='regex to apply to groupId')
    parser.add_argument('--include-version', '-iv', type=str, metavar='REGEX', help='regex to apply to version')
    parser.add_argument('--force-upload', '-F', action='store_true',
                        help='force upload to Nexus even if artifact exists.')
    parser.add_argument('--limit', '-l', type=int,
                        help='only upload the latest k versions (by mtime) for each artifact, default no limit.')

    args = parser.parse_args()
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
    import re

    for repodir in args.repodirs:
        uploader = Nexus3Uploader(
            m2_path=repodir,
            repo_id=args.repo_id,
            auth=args.auth.split(':') if args.auth else None,
            include_artifact_pattern=re.compile(args.include_artifact) if args.include_artifact else None,
            include_group_pattern=re.compile(args.include_group) if args.include_group else None,
            include_version_pattern=re.compile(args.include_version) if args.include_version else None,
            force_upload=args.force_upload,
            repo_url=args.repo_url,
            classifiers={'sources', 'javadoc', 'no_aop', 'noaop', 'linux-x86_64', 'osx-x86_64'},
            types={'jar', 'exe'},
            limit=args.limit,
        )

        uploader.upload()


if __name__ == '__main__':
    main()
