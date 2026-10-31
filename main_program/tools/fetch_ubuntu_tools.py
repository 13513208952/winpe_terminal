#!/usr/bin/python3 -I
"""Fetch signed-index Ubuntu packages into a private prefix, never apt install.

Package sizes and SHA512 come from the host's existing apt indices. Nothing is
installed globally and no apt index/cache is updated. HTTPS only.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import shlex
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

PACKAGES = (
    "qemu-system-x86", "qemu-system-common", "qemu-system-data", "seabios",
    "libpmem1", "libfdt1", "librdmacm1t64", "libslirp0", "libndctl6", "libdaxctl1",
    "msitools", "libmsi-1.0-0", "libgsf-1-114", "libgsf-1-common", "wimtools", "libwim15t64",
    "libhivex-bin", "libhivex0",
)


def checksum(path):
    digest = hashlib.sha512()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(item, directory):
    url, name, size, expected = item
    target = directory / name
    if target.is_file() and target.stat().st_size == size and checksum(target) == expected:
        return target
    temporary = target.with_name(name + ".part")
    try:
        with urllib.request.urlopen(url, timeout=90) as source, temporary.open("wb") as output:
            if urllib.parse.urlsplit(source.url).scheme != "https":
                raise ValueError("redirected away from HTTPS")
            total = 0
            while block := source.read(1024 * 1024):
                total += len(block)
                if total > size:
                    raise ValueError("package larger than authenticated index size")
                output.write(block)
        if temporary.stat().st_size != size or checksum(temporary) != expected:
            raise ValueError("package size/SHA512 mismatch: " + name)
        temporary.replace(target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--gui", action="store_true", help="Also fetch the optional GTK/SDL boot-viewer modules")
    parser.add_argument("--prune-downloads", action="store_true",
                        help="Delete the .deb files after extraction (keeps manifest.json)")
    args = parser.parse_args()
    packages = PACKAGES + (("qemu-system-gui", "qemu-system-modules-opengl") if args.gui else ())
    root = args.root.absolute()
    downloads, prefix = root / "downloads/ubuntu", root / "tools/ubuntu"
    downloads.mkdir(parents=True, exist_ok=True)
    prefix.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "LC_ALL": "C"}
    result = subprocess.run(["/usr/bin/apt-get", "-o", "Dir::Cache::pkgcache=", "-o", "Dir::Cache::srcpkgcache=",
                             "--print-uris", "download", *packages], check=False, text=True, capture_output=True, env=env)
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"apt-get exited {result.returncode}")
    items = []
    for line in result.stdout.splitlines():
        parts = shlex.split(line)
        if len(parts) != 4:
            continue
        uri, name, size, digest = parts
        parsed = urllib.parse.urlsplit(uri)
        if parsed.hostname not in {"archive.ubuntu.com", "security.ubuntu.com"} or not digest.startswith("SHA512:"):
            raise ValueError("Unexpected repository/hash; inspect indices before downloading.")
        if Path(name).name != name or not name.endswith(".deb"):
            raise ValueError("unsafe package filename")
        uri = urllib.parse.urlunsplit(parsed._replace(scheme="https"))
        items.append((uri, name, int(size), digest.split(":", 1)[1]))
    if len(items) != len(packages):
        raise ValueError("incomplete package list")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        paths = list(executor.map(lambda item: download(item, downloads), items))
    for package in paths:
        print("extract:", package.name, flush=True)
        subprocess.run(["/usr/bin/dpkg-deb", "--extract", str(package), str(prefix)], check=True)
    (root / "downloads/ubuntu/manifest.json").write_text(json.dumps(items, indent=2) + "\n")
    if args.prune_downloads:
        for package in paths:
            package.unlink(missing_ok=True)
        print("removed downloaded .deb files to keep the install directory small")
    print("Local tool prefix:", prefix)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Tool fetch failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
