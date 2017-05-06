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

import requests
from requests.auth import HTTPBasicAuth
import os
import os.path as path
import sys
import argparse

def list_files(root, ffilter = lambda x: True, recurse = True):
    """ list all files matching a filter in a given dir with optional recursion. """
    for root, subdirs, files in os.walk(root):
        for f in filter(ffilter, files):
            yield path.join(root, f)
        if recurse:
            for sdir in subdirs:
                for f in list_files(sdir, ffilter, recurse):
                    yield f


def m2_maven_info(root):
    """ walks an on-disk m2 repo yielding a dict of pom/gav/jar info. """
    for pom in list_files(root, lambda x: x.endswith(".pom")):
        rpath = path.dirname(pom).replace(root, '')
        rpath_parts = filter(lambda x: x != '', rpath.split(os.sep))
        info = { 'path': path.dirname(pom), 'pom': path.basename(pom) }
        info['g'] = '.'.join(rpath_parts[:-2])
        info['a'] = rpath_parts[-2:-1][0]
        info['v'] = rpath_parts[-1:][0]
        # check for jar
        jarfile = pom.replace('.pom', '.jar')
        if path.isfile(jarfile):            
            info['jar'] = path.basename(jarfile)
            # check for sources
            sourcejar = jarfile.replace('.jar', '-sources.jar')
            if path.isfile(sourcejar):
                info['source'] = path.basename(sourcejar)
            # check for javadoc
            docjar = jarfile.replace('.jar', '-javadoc.jar')
            if path.isfile(docjar):
                info['docs'] = docjar
        yield info

def nexus_postform(minfo, repo_url, files, auth, form_params):
    url = "%s/%s" % (repo_url, 'nexus/service/local/artifact/maven/content')
    req = requests.post(url, files=files, auth=auth, data=form_params)
    if req.status_code > 299:
        print "Error communicating with Nexus!",
        print "code=" + str(req.status_code) + ", msg=" + req.content
    else:
        print "Successfully uploaded: " + last_attached_file(files, minfo)
        

def artifact_exists(repo_url, repo_id, auth, artifact_path):
    url = "%s/nexus/content/repositories/%s/%s" % (repo_url, repo_id, artifact_path)
    #print "Checking for: " + url 
    req = requests.head(url, auth=auth)
    if req.status_code == 404:
        return False
    if req.status_code == 200:
        print "Will *NOT* upload %s, artifact already exists" % (artifact_path)
        return True
    else:
        # for safety, return true if we cannot determine if file exists
        print "Error checking status of: " + basename
        return True

def last_attached_file(files, minfo):
    m2_path = "%s/%s/%s" % (minfo['g'].replace('.','/'), minfo['a'], minfo['v'])
    return "%s/%s"  % (m2_path, files[-1][1][0])

def nexus_upload(maven_info, repo_url, repo_id, credentials=None, force=False):
    def encode_file(basename):
        fullpath = path.join(maven_info['path'], basename)
        return ('file', (basename, open(fullpath, 'rb'))) 

    files = []
    basepath = maven_info['path']
    payload = { 'hasPom':'true', 'r':repo_id }
    auth = None
    if credentials is not None:
        auth = HTTPBasicAuth(credentials[0], credentials[1])
        
    # append file params
    files.append(encode_file(maven_info['pom']))
    if 'jar' in maven_info:
        files.append(encode_file(maven_info['jar']))
        payload.update({'e': 'jar'})

    last_artifact = last_attached_file(files, maven_info)
    if force or not artifact_exists(repo_url, repo_id, auth, last_artifact) :
        nexus_postform(maven_info, repo_url, files=files, auth=auth, form_params=payload)

    if 'source' in maven_info:
        files = [ encode_file(maven_info['pom']), encode_file(maven_info['source']) ]
        payload.update({'e':'jar', 'c':'sources'})
        last_artifact = last_attached_file(files, maven_info)
        if force or not artifact_exists(repo_url, repo_id, auth, last_artifact):
            nexus_postform(maven_info, repo_url, files=files, auth=auth, form_params=payload)

    if 'docs' in maven_info:
        files = [ encode_file(maven_info['pom']), encode_file(maven_info['docs']) ]
        payload.update({'e':'jar', 'c':'javadoc'})
        nexus_postform(repo_url, files=files, auth=auth, form_params=payload)
        last_artifact = last_attached_file(files, maven_info)
        if force or not artifact_exists(repo_url, repo_id, auth, last_artifact):
            nexus_postform(maven_info, repo_url, files=files, auth=auth, form_params=payload)
            

def gav(info):
    return (info['g'], info['a'], info['v'])

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Easily upload multiple artifacts to a remote Nexus server.')
    parser.add_argument('repodirs', type=str, nargs='+',
                        help='list of repodirs to scan')
    parser.add_argument('--repo-id', type=str, help='Repository ID (in Nexus) to u/l to.', required=True)
    parser.add_argument('--auth',type=str, help='basicauth credentials in the form of username:password.')
    parser.add_argument('--include-artifact','-ia', type=str, metavar='REGEX', help='regex to apply to artifactId')
    parser.add_argument('--include-group', '-ig', type=str, metavar='REGEX', help='regex to apply to groupId')
    parser.add_argument('--force-upload', '-F', action='store_true', help='force u/l to Nexus even if artifact exists.')
    parser.add_argument('--repo-url', type=str, required=True, 
                        help="Nexus repo URL (e.g. http://localhost:8081)")


    args = parser.parse_args()
    
    import re
    igroup_pat = None
    iartifact_pat = None
    if args.include_group:
        igroup_pat = re.compile(args.include_group)
    if args.include_artifact:
        iartifact_pat = re.compile(args.include_artifact)

        
    for repo in args.repodirs:
        print "Uploading content from [%s] to %s repo on %s" % (repo, args.repo_id, args.repo_url)
        for info in m2_maven_info(repo):
            # only include specific groups if group regex supplied
            if igroup_pat and not igroup_pat.search(info['g']):
                continue

            # only include specific artifact if artifact regex supplied
            if iartifact_pat and not iartifact_pat.search(info['a']):
                continue
            
            print "\nProcessing: %s" % (gav(info),)
            nexus_upload(info, args.repo_url, args.repo_id, credentials=tuple(args.auth.split(':')), force=args.force_upload)





