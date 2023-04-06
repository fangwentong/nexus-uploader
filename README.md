# Nexus Uploader

The `nexus-uploader` Python script allows for easy upload of local M2 repositories to a remote Nexus server with just
one command. Some key features include:

- Uploading of common classifiers (sources, javadocs) if available
- Using regex include pattern for artifactIds/groupIds/versions
- Recursively processing local repo by just pointing to the root
- Only uploading artifacts missing on the server (with an option to force upload if needed)

This repository was originally forked
from [Kshekhovtsova/nexus-uploader.py](https://gist.github.com/Kshekhovtsova/b8c8aca31b58e9f766df449e96ad8d3d)
and [omnisis/nexus-uploader.py](https://gist.github.com/omnisis/9ecae6baf161d19206a5420bddffe1fc).

## Installation

Use the following command to install `nexus-uploader` from this repository:

```bash
pip3 install https://github.com/fangwentong/nexus-uploader/archive/master.zip
```

## Usage

The following command can be used to upload local m2 repositories to a Nexus server:

```bash
nexus-uploader repodir1 [repodir2 repodir3] [--repo-url URL] [--repo-id ID] \
              [--auth USERNAME:PASSWORD] [--include-artifact REGEX] \
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

The following command can be used to upload all artifacts from the local m2 repository to a Nexus server with the
following configuration:

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