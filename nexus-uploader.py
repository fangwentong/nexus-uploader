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

def nexus_upload(maven_info, repo_url, repo_id, credentials=None):
    def encode_file(basename, attach_name=None):
        if attach_name is None:
            attach_name = basename
        return ('file', (attach_name, open(path.join(maven_info['path'], basename), 'rb')) )

    def encode_form_kv(k,v):
        return (k, ('', v))

    form_params = [ encode_file(maven_info['pom'], attach_name='pom.xml') ]
            
    # append non-file fields to the form data
    form_params.append(encode_form_kv('p', 'jar'))
    form_params.append(encode_form_kv('hasPom', 'true'))
    form_params.append(encode_form_kv('r', repo_id))
    auth = None
    if credentials is not None:
        auth = HTTPBasicAuth(credentials[0], credentials[1])
        
    # append file params
    if 'jar' in maven_info:
        form_params.append(encode_file(maven_info['jar']))
    if 'source' in maven_info:
        form_params.append(encode_file(maven_info['source']))
    if 'docs' in maven_info:
        form_params.append(encode_file(maven_info['docs']))
            
    # make the POST request to Nexus REST API
    full_url = '/'.join([repo_url, 'nexus/service/local/artifact/maven/content'])
    req = requests.post(full_url, files=form_params, auth=auth)
    if req.status_code > 299:
        print "Error communicating with Nexus!",
        print "code=" + str(req.status_code) + ", msg=" + req.content

                         


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Easily upload multiple artifacts to a remote Nexus server.')
    parser.add_argument('repodirs', type=str, nargs='+',
                        help='list of repodirs to scan')
    parser.add_argument('--repo-id', type=str, help='Repository ID (in Nexus) to U/L to.', required=True)
    parser.add_argument('--auth',type=str, help='basicauth credentials in the form of username:password.')
    parser.add_argument('--include-artifact', type=str, metavar='REGEX', help='regex to apply to artifactId')
    parser.add_argument('--include-group', type=str, metavar='REGEX', help='regex to apply to groupId')
    parser.add_argument('--repo-url', type=str, required=True, 
                        help="Nexus repo URL (e.g. http://localhost:8081)")

    args = parser.parse_args()
    for repo in args.repodirs:
        i = 0
        for info in m2_maven_info(repo):
            nexus_upload(info, args.repo_url, args.repo_id, credentials=tuple(args.auth.split(':')))
            i += 1
            if i > 10:
                break





