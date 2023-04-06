# Nexus Uploader

nexus-uploader is a Python script that allows mirroring local M2 repositories to a remote Nexus server with a single
command. It provides the following features:

- uploading of common classifiers (sources, javadocs) if available
- using regex include pattern for artifactIds/groupIds/versions
- recursively processing local repo, just point to the root
- only upload artifacts missing on the server (with the option to force upload if needed)

## Installation

```bash
pip3 install https://github.com/fangwentong/nexus-uploader/archive/master.zip
```

## Usage

```
nexus-uploader repodir1 [repodir2 repodir3] [--repo-url URL] [--repo-id ID] 
              [--auth USERNAME:PASSWORD] [--include-artifact REGEX] 
              [--include-group REGEX] [--include-version REGEX] [--force-upload] 
```

- `repodir`: Specifies the local m2 repository path to upload to Nexus server.
- `--repo-url`: Specifies the Nexus repo URL, e.g. http://localhost:8081.
- `--repo-id`: Specifies the repository ID (in Nexus) to upload to.
- `--auth`: Provides basicauth credentials in Nexus with the form of username:password.
- `--include-artifact`: Applies the given regex to artifactId.
- `--include-group`: Applies the given regex to groupId.
- `--include-version`: Applies the given regex to version.
- `--force-upload`: Forces upload even if the artifact already exists on the server.

## Example

Upload all artifacts from the local m2 repository to a Nexus server with the following configuration:

```bash
nexus-uploader ~/.m2/repository \
  --repo-url http://localhost:8081 \
  --repo-id maven-releases \
  --auth admin:admin123 \
  --include-artifact ".*" \
  --include-group ".*" \
  --include-version ".*" \
  --force-upload
```

## License

This program is licensed under the MIT License. Please see the [LICENSE](LICENSE) file for details.
