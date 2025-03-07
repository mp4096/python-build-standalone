# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import gzip
import hashlib
import http.client
import io
import multiprocessing
import os
import pathlib
import stat
import subprocess
import sys
import tarfile
import zipfile
import urllib.error
import urllib.request

import yaml
import zstandard

from .downloads import DOWNLOADS
from .logging import log


def get_targets(yaml_path: pathlib.Path):
    """Obtain the parsed targets YAML file."""
    with yaml_path.open("rb") as fh:
        return yaml.load(fh, Loader=yaml.SafeLoader)


def get_target_settings(yaml_path: pathlib.Path, target: str):
    """Obtain the settings for a named target."""
    return get_targets(yaml_path)[target]


def supported_targets(yaml_path: pathlib.Path):
    """Obtain a set of named targets that we can build."""
    targets = set()

    for target, settings in get_targets(yaml_path).items():
        for platform in settings["host_platforms"]:
            if sys.platform == "linux" and platform == "linux64":
                targets.add(target)
            elif sys.platform == "darwin" and platform == "macos":
                targets.add(target)

    return targets


def target_needs(yaml_path: pathlib.Path, target: str, python_version: str):
    """Obtain the dependencies needed to build the specified target."""
    settings = get_targets(yaml_path)[target]

    needs = set(settings["needs"])

    # We only ship libedit linked readline extension on 3.10+ to avoid a GPL
    # dependency. Ditto for gdbm.
    if not python_version.startswith(("3.8", "3.9")):
        needs.discard("gdbm")
        needs.discard("readline")

    return needs


def release_tag_from_git():
    return (
        subprocess.check_output(
            [
                "git",
                "log",
                "-n",
                "1",
                "--date=format:%Y%m%dT%H%M",
                "--pretty=format:%ad",
            ]
        )
        .strip()
        .decode("ascii")
    )


def hash_path(p: pathlib.Path):
    h = hashlib.sha256()

    with p.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break

            h.update(chunk)

    return h.hexdigest()


def get_target_support_file(
    search_dir, prefix, python_version, host_platform, target_triple
):
    candidates = [
        search_dir / ("%s.%s.%s" % (prefix, python_version, target_triple)),
        search_dir / ("%s.%s.%s" % (prefix, python_version, host_platform)),
    ]

    for path in candidates:
        if path.exists():
            return path

    raise Exception(
        "Could not find support file %s for (%s, %s, %s)",
        prefix,
        python_version,
        host_platform,
        target_triple,
    )


def write_if_different(p: pathlib.Path, data: bytes):
    """Write a file if it is missing or its content is different."""
    if p.exists():
        with p.open("rb") as fh:
            existing = fh.read()
        write = existing != data
    else:
        write = True

    if write:
        with p.open("wb") as fh:
            fh.write(data)


def write_triples_makefiles(
    targets, dest_dir: pathlib.Path, support_search_dir: pathlib.Path
):
    """Write out makefiles containing make variable settings derived from config."""
    dest_dir.mkdir(parents=True, exist_ok=True)

    for triple, settings in targets.items():
        for host_platform in settings["host_platforms"]:
            for python in settings["pythons_supported"]:
                makefile_path = dest_dir / (
                    "Makefile.%s.%s.%s" % (host_platform, triple, python)
                )

                lines = []
                for need in settings.get("needs", []):
                    lines.append("NEED_%s := 1\n" % need.upper())

                image_suffix = settings.get("docker_image_suffix", "")

                lines.append("DOCKER_IMAGE_BUILD := build%s\n" % image_suffix)
                lines.append("DOCKER_IMAGE_XCB := xcb%s\n" % image_suffix)

                for support_file in (
                    "disabled-static-modules",
                    "required-extensions",
                    "static-modules",
                ):
                    path = get_target_support_file(
                        support_search_dir, support_file, python, host_platform, triple
                    )
                    lines.append(
                        "PYTHON_SUPPORT_FILES := $(PYTHON_SUPPORT_FILES) %s\n" % path
                    )

                write_if_different(makefile_path, "".join(lines).encode("ascii"))


def write_package_versions(dest_path: pathlib.Path):
    """Write out versions of packages to files in a directory."""
    dest_path.mkdir(parents=True, exist_ok=True)

    for k, v in DOWNLOADS.items():
        p = dest_path / ("VERSION.%s" % k)
        content = "%s_VERSION := %s\n" % (k.upper().replace("-", "_"), v["version"])
        write_if_different(p, content.encode("ascii"))


class IntegrityError(Exception):
    """Represents an integrity error when downloading a URL."""


def secure_download_stream(url, size, sha256):
    """Securely download a URL to a stream of chunks.

    If the integrity of the download fails, an IntegrityError is
    raised.
    """
    h = hashlib.sha256()
    length = 0

    with urllib.request.urlopen(url) as fh:
        if not url.endswith(".gz") and fh.info().get("Content-Encoding") == "gzip":
            fh = gzip.GzipFile(fileobj=fh)

        while True:
            chunk = fh.read(65536)
            if not chunk:
                break

            h.update(chunk)
            length += len(chunk)

            yield chunk

    digest = h.hexdigest()

    if length != size or digest != sha256:
        raise IntegrityError(
            "integrity mismatch on %s: wanted size=%d, sha256=%s; got size=%d, sha256=%s"
            % (url, size, sha256, length, digest)
        )


def download_to_path(url: str, path: pathlib.Path, size: int, sha256: str):
    """Download a URL to a filesystem path, possibly with verification."""

    # We download to a temporary file and rename at the end so there's
    # no chance of the final file being partially written or containing
    # bad data.
    print("downloading %s to %s" % (url, path))

    if path.exists():
        good = True

        if path.stat().st_size != size:
            print("existing file size is wrong; removing")
            good = False

        if good:
            if hash_path(path) != sha256:
                print("existing file hash is wrong; removing")
                good = False

        if good:
            print("%s exists and passes integrity checks" % path)
            return

        path.unlink()

    tmp = path.with_name("%s.tmp" % path.name)

    for _ in range(5):
        try:
            try:
                with tmp.open("wb") as fh:
                    for chunk in secure_download_stream(url, size, sha256):
                        fh.write(chunk)

                break
            except IntegrityError:
                tmp.unlink()
                raise
        except http.client.HTTPException as e:
            print("HTTP exception; retrying: %s" % e)
        except urllib.error.URLError as e:
            print("urllib error; retrying: %s" % e)
    else:
        raise Exception("download failed after multiple retries")

    tmp.rename(path)
    print("successfully downloaded %s" % url)


def download_entry(key: str, dest_path: pathlib.Path, local_name=None) -> pathlib.Path:
    entry = DOWNLOADS[key]
    url = entry["url"]

    local_name = local_name or url[url.rindex("/") + 1 :]

    local_path = dest_path / local_name
    download_to_path(url, local_path, entry["size"], entry["sha256"])

    return local_path


def create_tar_from_directory(fh, base_path: pathlib.Path, path_prefix=None):
    with tarfile.open(name="", mode="w", fileobj=fh) as tf:
        for root, dirs, files in os.walk(base_path):
            dirs.sort()

            for f in sorted(files):
                full = base_path / root / f
                rel = full.relative_to(base_path)
                if path_prefix:
                    rel = pathlib.Path(path_prefix) / rel
                tf.add(full, rel)


def extract_tar_to_directory(source: pathlib.Path, dest: pathlib.Path):
    with tarfile.open(source, "r") as tf:
        tf.extractall(dest)


def extract_zip_to_directory(source: pathlib.Path, dest: pathlib.Path):
    with zipfile.ZipFile(source, "r") as zf:
        zf.extractall(dest)


# 2021-01-01T00:00:00
DEFAULT_MTIME = 1609488000


def normalize_tar_archive(data: io.BytesIO) -> io.BytesIO:
    """Normalize the contents of a tar archive.

    We want tar archives to be as deterministic as possible. This function will
    take tar archive data in a buffer and return a new buffer containing a more
    deterministic tar archive.
    """
    members = []

    with tarfile.open(fileobj=data) as tf:
        for ti in tf:
            # We don't care about directory entries. Tools can handle this fine.
            if ti.isdir():
                continue

            filedata = tf.extractfile(ti)
            if filedata is not None:
                filedata = io.BytesIO(filedata.read())

            members.append((ti, filedata))

    # Sort the archive members. We put PYTHON.json first so metadata can
    # be read without reading the entire archive.
    def sort_key(v):
        if v[0].name == "python/PYTHON.json":
            return 0, v[0].name
        else:
            return 1, v[0].name

    members.sort(key=sort_key)

    # Normalize attributes on archive members.
    for entry in members:
        ti = entry[0]

        # The pax headers attribute takes priority over the other named
        # attributes. To minimize potential for our assigns to no-op, we
        # clear out the pax headers. We can't reset all the pax headers,
        # as this would nullify symlinks.
        for a in ("mtime", "uid", "uname", "gid", "gname"):
            try:
                ti.pax_headers.__delattr__(a)
            except AttributeError:
                pass

        ti.pax_headers = {}

        ti.mtime = DEFAULT_MTIME
        ti.uid = 0
        ti.uname = "root"
        ti.gid = 0
        ti.gname = "root"

        # Give user/group read/write on all entries.
        ti.mode |= stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP

        # If user executable, give to group as well.
        if ti.mode & stat.S_IXUSR:
            ti.mode |= stat.S_IXGRP

    dest = io.BytesIO()
    with tarfile.open(fileobj=dest, mode="w") as tf:
        for (ti, filedata) in members:
            tf.addfile(ti, filedata)

    dest.seek(0)

    return dest


def compress_python_archive(
    source_path: pathlib.Path, dist_path: pathlib.Path, basename: str
):
    dest_path = dist_path / ("%s.tar.zst" % basename)
    temp_path = dist_path / ("%s.tar.zst.tmp" % basename)

    print("compressing Python archive to %s" % dest_path)

    try:
        with source_path.open("rb") as ifh, temp_path.open("wb") as ofh:
            params = zstandard.ZstdCompressionParameters.from_level(
                22, strategy=zstandard.STRATEGY_BTULTRA2
            )
            cctx = zstandard.ZstdCompressor(compression_params=params)
            cctx.copy_stream(ifh, ofh, source_path.stat().st_size)

        temp_path.rename(dest_path)
    except Exception:
        temp_path.unlink()
        raise

    print("%s has SHA256 %s" % (dest_path, hash_path(dest_path)))

    return dest_path


def add_licenses_to_extension_entry(entry):
    """Add licenses keys to a ``extensions`` entry for JSON distribution info."""

    have_licenses = False
    licenses = set()
    license_paths = set()
    license_public_domain = None

    have_local_link = False

    for link in entry["links"]:
        name = link["name"]
        if "path_static" in link or "path_dynamic" in link:
            have_local_link = True

        for key, value in DOWNLOADS.items():
            if name not in value.get("library_names", []):
                continue

            # Don't add licenses annotations if they aren't defined. This leaves
            # things as "unknown" to consumers.
            if "licenses" not in value:
                continue

            have_licenses = True
            licenses |= set(value["licenses"])
            license_paths.add("licenses/%s" % value["license_file"])
            license_public_domain = value.get("license_public_domain", False)

    if have_local_link and not have_licenses:
        raise Exception(
            "missing license for local library for extension entry: %s" % entry
        )

    if not have_licenses:
        return

    entry["licenses"] = sorted(licenses)
    entry["license_paths"] = sorted(license_paths)
    entry["license_public_domain"] = license_public_domain


def add_env_common(env):
    """Adds extra keys to environment variables."""

    cpu_count = multiprocessing.cpu_count()
    env["NUM_CPUS"] = "%d" % cpu_count
    env["NUM_JOBS_AGGRESSIVE"] = "%d" % max(cpu_count + 2, cpu_count * 2)

    if "CI" in os.environ:
        env["CI"] = "1"

    env_path = os.path.expanduser("~/.python-build-standalone-env")
    try:
        with open(env_path, "r") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("#"):
                    continue

                key, value = line.split("=", 1)

                print("adding %s from %s" % (key, env_path))
                env[key] = value
    except FileNotFoundError:
        pass

    # Proxy sccache settings.
    for k, v in os.environ.items():
        if k.startswith("SCCACHE_"):
            env[k] = v

    # Proxy cloud provider credentials variables to enable sccache to
    # use stores in those providers.
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        if k in os.environ:
            env[k] = os.environ[k]


def exec_and_log(args, cwd, env):
    p = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        bufsize=1,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    for line in iter(p.stdout.readline, b""):
        log(line.rstrip())

    p.wait()

    if p.returncode:
        if "PYBUILD_BREAK_ON_FAILURE" in os.environ:
            import pdb

            pdb.set_trace()

        print("process exited %d" % p.returncode)
        sys.exit(p.returncode)


def validate_python_json(info):
    """Validate a PYTHON.json file for problems.

    Raises an exception if an issue is detected.
    """

    for name, variants in info["build_info"]["extensions"].items():
        for ext in variants:
            variant = ext["variant"]

            local_links = set()

            for link in ext["links"]:
                if "path_static" in link:
                    local_links.add(link["path_static"])
                if "path_dynamic" in link:
                    local_links.add(link["path_dynamic"])

                if not local_links and "framework" not in link and "system" not in link:
                    raise Exception(
                        "Invalid link entry for extension %s[%s]: link type not defined"
                        % (name, variant)
                    )

            if (
                local_links
                and not ext.get("licenses")
                and not ext.get("license_public_domain")
            ):
                raise Exception(
                    "Missing license annotations for extension %s[%s] for library files %s"
                    % (name, variant, ", ".join(sorted(local_links)))
                )
