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

from __future__ import print_function

import argparse
import logging
import sys
from pathlib import Path
from typing import Dict, Pattern, List

import requests
from requests.auth import HTTPBasicAuth
import heapq


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
        return f'{self.group_id}:{self.artifact_id}:{self.version}'

    def __repr__(self):
        return str(self)


class BaseNexusUploader:
    m2_path: Path
    repo_id: str
    auth: HTTPBasicAuth
    include_artifact_pattern: Pattern
    include_group_pattern: Pattern
    include_version_pattern: Pattern
    force_upload: bool
    repo_url: str
    classifiers: List[str]
    types: List[str]
    limit: int

    def __init__(self, m2_path='~/.m2/repository', repo_id=None, auth=None, include_artifact_pattern=None,
                 include_group_pattern=None, include_version_pattern=None, force_upload=False,
                 repo_url=None, classifiers=None, types=None, limit=None):
        self.m2_path = Path(m2_path).expanduser()
        self.repo_id = repo_id
        self.auth = HTTPBasicAuth(*auth) if auth is not None else None
        self.include_artifact_pattern = include_artifact_pattern
        self.include_group_pattern = include_group_pattern
        self.include_version_pattern = include_version_pattern
        self.force_upload = force_upload
        self.repo_url = repo_url
        self.classifiers = classifiers
        self.types = types
        self.limit = limit

    @staticmethod
    def list_files(root, file_filter=lambda x: True, recurse=False):
        """ list all files matching a filter in a given dir with optional recursion. """
        for p in root.glob('**/*'):
            if p.is_file() and file_filter(p.name):
                yield str(p)
            elif recurse and p.is_dir():
                yield from BaseNexusUploader.list_files(p, file_filter, False)

    def m2_maven_info(self):
        """ walks an on-disk m2 repo yielding a dict of pom/gav/jar info. """
        for pom in BaseNexusUploader.list_files(self.m2_path, lambda x: x.endswith(".pom")):
            rpath_parts = list(filter(bool, str(Path(pom).relative_to(self.m2_path)).split('/')))
            info = MavenInfo(
                path=Path(pom).parent,
                pom=Path(pom).name,
                group_id='.'.join(rpath_parts[:-3]),
                artifact_id=rpath_parts[-3],
                version=rpath_parts[-2],
                mtime=Path(pom).stat().st_mtime,
            )
            # check for jar
            jarfile = pom.replace('.pom', '.jar')
            if Path(jarfile).is_file():
                info.jar = Path(jarfile).name

            # classifiers: 'sources', 'javadoc', 'no_aop', 'noaop', 'linux-x86_64'
            for classifier in self.classifiers:
                files = []
                for filetype in self.types:
                    classifier_file = jarfile.replace('.jar', f'-{classifier}.{filetype}')
                    if Path(classifier_file).is_file():
                        logging.info(f'Found {filetype}: {classifier_file}')
                        files.append(Path(classifier_file).name)
                if len(files) > 0:
                    info.classifiers[classifier] = files

            yield info

    def _upload_single(self, maven_info):
        raise NotImplementedError

    def upload(self):
        logging.info(f'Repodirs: {self.m2_path}')
        maven_info_list = self.m2_maven_info()

        logging.info(f'Uploading content from [{self.m2_path}] to {self.repo_id} repo on {self.repo_url}')
        total = 0
        m = {}
        for maven_info in maven_info_list:
            # only include specific groups if group regex supplied
            if self.include_group_pattern and not self.include_group_pattern.search(maven_info.group_id):
                continue

            # only include specific artifact if artifact regex supplied
            if self.include_artifact_pattern and not self.include_artifact_pattern.search(maven_info.artifact_id):
                continue

            # only include specific version if version regex supplied
            if self.include_version_pattern and not self.include_version_pattern.search(maven_info.version):
                continue

            k = f'{maven_info.group_id}:{maven_info.artifact_id}'

            if k not in m:
                m[k] = []
            heap = m[k]
            if self.limit and len(heap) < self.limit:
                if heap[0] < MinHeapObj(maven_info):
                    discarded = maven_info
                else:
                    discarded = heapq.heapreplace(heap, MinHeapObj(maven_info)).val
                logging.info(f'Discard: {discarded} due to version limits')
            else:
                heapq.heappush(heap, MinHeapObj(maven_info))
                total += 1

        current = 0
        for heap in m.values():
            for obj in heap:
                maven_info = obj.val
                current += 1
                logging.info(f'\nProcessing: {maven_info}, {current}/{total}')
                self._upload_single(maven_info)


class Nexus3Uploader(BaseNexusUploader):
    """
    Uploads local m2 repositories to a Nexus 3 Server
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def _nexus_post_form(self, minfo, files, form_params):
        url = '%s/%s?repository=%s' % (self.repo_url, 'service/rest/v1/components', self.repo_id)
        req = requests.post(url, files=files, auth=self.auth, data=form_params)
        if req.status_code > 299:
            logging.error('Error communicating with Nexus! url=' + url + ', code=' + str(
                req.status_code) + ', msg=' + req.content.decode('utf-8'))
        else:
            logging.info('Successfully uploaded: %s', self.last_attached_file(files, minfo))

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
    def last_attached_file(files, minfo):
        m2_path = '%s/%s/%s' % (minfo.group_id.replace('.', '/'), minfo.artifact_id, minfo.version)
        return '%s/%s' % (m2_path, files[-1][1][0])

    def _upload_single(self, maven_info):
        def encode_file(basename, num):
            fullpath = maven_info.path / basename
            return f'maven2.asset{num}', (basename, fullpath.open('rb'))

        files = []
        payload = {'maven2.generate-pom': 'false'}

        # append file params
        files.append(encode_file(maven_info.pom, 1))
        payload.update({'maven2.asset1.extension': 'pom'})

        extension_num = 2

        if maven_info.jar is not None:
            files.append(encode_file(maven_info.jar, 2))
            last_artifact = self.last_attached_file(files, maven_info)
            if not self.force_upload and self._artifact_exists(last_artifact):
                del files[-1]
            else:
                payload.update({'maven2.asset2.extension': 'jar'})
                extension_num += 1

        for classifier in self.classifiers:
            if classifier in maven_info.classifiers:
                for filename in maven_info.classifiers[classifier]:
                    files.append(encode_file(filename, extension_num))
                    last_artifact = self.last_attached_file(files, maven_info)
                    if not self.force_upload and self._artifact_exists(last_artifact):
                        files.pop()
                    else:
                        payload.update({f'maven2.asset{extension_num}.extension': filename.split('.')[-1]})
                        payload.update({f'maven2.asset{extension_num}.classifier': classifier})
                        extension_num += 1
                        logging.info(f'Appended file {filename} num = {extension_num - 1}')

        self._nexus_post_form(maven_info, files=files, form_params=payload)


class MinHeapObj(object):
    def __init__(self, val): self.val = val

    def __lt__(self, other): return self.val.mtime < other.val.mtime

    def __eq__(self, other): return self.val.mtime == other.val.mtime

    def __str__(self): return str(self.val)


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
