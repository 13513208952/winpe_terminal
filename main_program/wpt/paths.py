"""Filesystem layout: the single place that decides where WPT reads and writes.

Debian layout:
    /usr/bin/winpe_terminal             command entry
    /usr/lib/winpe-terminal/            private program files
    /etc/winpe-terminal/config.json     conffile
    /var/lib/winpe-terminal/            state: assets, instances, marker
    /var/cache/winpe-terminal/          re-creatable: downloads, build, tmp
    /var/log/winpe-terminal/            logs
    /usr/share/doc/winpe-terminal/      documentation and licences

WPT_PREFIX relocates the whole tree, which lets the package be exercised
without root (tests, staging, containers). Production runs use "/".
"""
import os
from pathlib import Path


class Layout:
    def __init__(self, prefix=None):
        value = os.environ.get("WPT_PREFIX", "/") if prefix is None else str(prefix)
        self.prefix = Path(value).absolute()

    def at(self, *parts):
        return self.prefix.joinpath(*parts)

    # Configuration -----------------------------------------------------
    @property
    def config_dir(self):
        return self.at("etc/winpe-terminal")

    @property
    def config(self):
        return self.config_dir / "config.json"

    # State -------------------------------------------------------------
    @property
    def state_dir(self):
        return self.at("var/lib/winpe-terminal")

    @property
    def assets(self):
        return self.state_dir / "assets"

    @property
    def iso(self):
        return self.assets / "winpe.iso"

    @property
    def receipt(self):
        return self.assets / "winpe-build.json"

    @property
    def instances(self):
        return self.state_dir / "instances"

    @property
    def marker(self):
        return self.state_dir / "installation.json"

    @property
    def lock(self):
        return self.state_dir / ".operation.lock"

    # Cache (safe to delete at any time) --------------------------------
    @property
    def cache_dir(self):
        return self.at("var/cache/winpe-terminal")

    @property
    def downloads(self):
        return self.cache_dir / "downloads"

    @property
    def build(self):
        return self.cache_dir / "build"

    @property
    def tmp(self):
        return self.cache_dir / "tmp"

    # Logs --------------------------------------------------------------
    @property
    def logs(self):
        return self.at("var/log/winpe-terminal")

    # Program and documentation -----------------------------------------
    @property
    def program_dir(self):
        return self.at("usr/lib/winpe-terminal")

    @property
    def doc_dir(self):
        return self.at("usr/share/doc/winpe-terminal")

    def ensure_tree(self):
        """Create the runtime directories this program owns, if missing."""
        for path in (self.state_dir, self.assets, self.instances, self.cache_dir,
                     self.downloads, self.build, self.tmp, self.logs):
            path.mkdir(parents=True, exist_ok=True)
