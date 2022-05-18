#!/usr/bin/env python


""""
nexus-uploader.py
Allows mirroring local M2 repositories to a remote Nexus server with a single command.
Supports:
   - uploading of common classifiers (sources, javadocs) if available
   - using regex include pattern for artifactIds/groupIds
   - recursively processing local repo, just point to the root
   - only upload artifacts missing on server (with option to force if needed)
"""

from __future__ import print_function

import requests
from requests.auth import HTTPBasicAuth
import os
import os.path as path
import sys
import argparse


def list_files(root, ffilter=lambda x: True, recurse=False):
    """ list all files matching a filter in a given dir with optional recursion. """
    for root1, subdirs, files in os.walk(root):
        for f in filter(ffilter, files):
            yield path.join(root1, f)
        # print "subdirs: " + str(subdirs)
        if recurse:
            for sdir in subdirs:
                for f1 in list_files(full_path(root1, sdir), ffilter, False):
                    yield f1


def full_path(root, directory):
    if root.endswith(os.sep):
        return root + directory
    else:
        return "%s/%s" % (root, directory)


def m2_maven_info(root, classifiers):
    """ walks an on-disk m2 repo yielding a dict of pom/gav/jar info. """
    for pom in list_files(root, lambda x: x.endswith(".pom")):
        rpath = path.dirname(pom).replace(root, '')
        rpath_parts = list(filter(lambda x: x != '', rpath.split(os.sep)))
        info = {'path': path.dirname(pom), 'pom': path.basename(pom)}
        info['g'] = '.'.join(rpath_parts[:-2])
        info['a'] = rpath_parts[-2:-1][0]
        info['v'] = rpath_parts[-1:][0]
        # check for jar
        jarfile = pom.replace('.pom', '.jar')
        if path.isfile(jarfile):
            info['jar'] = path.basename(jarfile)

        # classifiers: 'sources', 'javadoc', 'no_aop', 'noaop', 'linux-x86_64'
        for classifier in classifiers:
            endofjar = "-%s.jar" % (classifier)
            minfo_key = "classifier.%s" % (classifier)
            classifjar = jarfile.replace('.jar', endofjar)
            if path.isfile(classifjar):
                print("Found jar: " + classifjar)
                info[minfo_key] = path.basename(classifjar)

        yield info


def nexus_postform(minfo, repo_url, repo_id, files, auth, form_params):
    url = "%s/%s?repository=%s" % (repo_url, 'service/rest/v1/components', repo_id)
    req = requests.post(url, files=files, auth=auth, data=form_params)
    if req.status_code > 299:
        print("Error communicating with Nexus!")
        print("url=" + url + ", code=" + str(req.status_code) + ", msg=" + req.content)
    else:
        print("Successfully uploaded: " + last_attached_file(files, minfo))


def artifact_exists(repo_url, repo_id, auth, artifact_path):
    url = "%s/repository/%s/%s" % (repo_url, repo_id, artifact_path)
    print("Checking for: " + url)
    req = requests.head(url, auth=auth)
    if req.status_code == 404:
        return False
    if req.status_code == 200:
        print("Will *NOT* upload %s, artifact already exists" % (artifact_path))
        return True
    else:
        # for safety, return true if we cannot determine if file exists
        print("Error checking status of: " + basename)
        return True


def last_attached_file(files, minfo):
    m2_path = "%s/%s/%s" % (minfo['g'].replace('.', '/'), minfo['a'], minfo['v'])
    return "%s/%s" % (m2_path, files[-1][1][0])


def nexus_upload(maven_info, repo_url, repo_id, classifiers, credentials=None, force=False):
    def encode_file(basename, num):
        fullpath = path.join(maven_info['path'], basename)
        return ('maven2.asset' + str(num), (basename, open(fullpath, 'rb')))

    files = []
    basepath = maven_info['path']
    payload = {'maven2.generate-pom': 'false'}
    auth = None
    if credentials is not None:
        auth = HTTPBasicAuth(credentials[0], credentials[1])

    # append file params
    files.append(encode_file(maven_info['pom'], 1))
    payload.update({'maven2.asset1.extension': 'pom'})

    extension_num = 2

    if 'jar' in maven_info:
        files.append(encode_file(maven_info['jar'], 2))
        last_artifact = last_attached_file(files, maven_info)
        if not force and artifact_exists(repo_url, repo_id, auth, last_artifact):
            del files[-1]
        else:
            payload.update({'maven2.asset2.extension': 'jar'})
            extension_num = extension_num + 1

    for classifier in classifiers:
        minfo_key = "classifier.%s" % (classifier)
        if minfo_key in maven_info:
            files.append(encode_file(maven_info[minfo_key], extension_num))
            last_artifact = last_attached_file(files, maven_info)
            if not force and artifact_exists(repo_url, repo_id, auth, last_artifact):
                del files[-1]
            else:
                formdata_key_ext = "maven2.asset%s.extension" % (str(extension_num))
                formdata_key_classif = "maven2.asset%s.classifier" % (str(extension_num))
                payload.update({formdata_key_ext: 'jar'})
                payload.update({formdata_key_classif: classifier})
                extension_num = extension_num + 1
                print("Appended file " + str(maven_info[minfo_key]) + " num = " + str(extension_num - 1))

    nexus_postform(maven_info, repo_url, repo_id, files=files, auth=auth, form_params=payload)


def gav(info):
    return (info['g'], info['a'], info['v'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Easily upload multiple artifacts to a remote Nexus server.')
    parser.add_argument('repodirs', type=str, nargs='+',
                        help='list of repodirs to scan')
    parser.add_argument('--repo-id', type=str, help='Repository ID (in Nexus) to u/l to.', required=True)
    parser.add_argument('--auth', type=str, help='basicauth credentials in the form of username:password.')
    parser.add_argument('--include-artifact', '-ia', type=str, metavar='REGEX', help='regex to apply to artifactId')
    parser.add_argument('--include-group', '-ig', type=str, metavar='REGEX', help='regex to apply to groupId')
    parser.add_argument('--include-version', '-iv', type=str, metavar='REGEX', help='regex to apply to version')
    parser.add_argument('--force-upload', '-F', action='store_true', help='force u/l to Nexus even if artifact exists.')
    parser.add_argument('--repo-url', type=str, required=True,
                        help="Nexus repo URL (e.g. http://localhost:8081)")

    args = parser.parse_args()

    import re

    igroup_pat = None
    iartifact_pat = None
    iversion_pat = None
    if args.include_group:
        igroup_pat = re.compile(args.include_group)
    if args.include_artifact:
        iartifact_pat = re.compile(args.include_artifact)
    if args.include_version:
        iversion_pat = re.compile(args.include_version)

    print("Repodirs: %s" % (args.repodirs))
    repo = args.repodirs[0]
    classifiers = ['sources', 'javadoc', 'no_aop', 'noaop', 'linux-x86_64']
    mmi = m2_maven_info(repo, classifiers)

    print("Uploading content from [%s] to %s repo on %s" % (repo, args.repo_id, args.repo_url))
    for info in mmi:
        # only include specific groups if group regex supplied
        if igroup_pat and not igroup_pat.search(info['g']):
            continue

        # only include specific artifact if artifact regex supplied
        if iartifact_pat and not iartifact_pat.search(info['a']):
            continue

        # only include specific version if version regex supplied
        if iversion_pat and not iversion_pat.search(info['v']):
            continue

        print("\nProcessing: %s" % (gav(info),))
        nexus_upload(info, args.repo_url, args.repo_id, classifiers, credentials=tuple(args.auth.split(':')),
                     force=args.force_upload)
