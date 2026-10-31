"""Development-only staging of the Debian layout into a private prefix.

The released package is built with dpkg; this helper exists so the same code
paths can be exercised without root via WPT_PREFIX.
"""
import os
import uuid
from pathlib import Path

from . import WptError
from .config import APP_ID, DEFAULTS
from .registry import now
from .storage import atomic_json

MODULES = ("__init__.py", "storage.py", "paths.py", "config.py", "registry.py",
           "safety.py", "qmp.py", "sac.py", "partition.py", "vm.py",
           "lifecycle.py", "prepare.py", "deployment.py", "cli.py")
SOURCE_FILES = ("winpe_terminal", *("wpt/" + name for name in MODULES))
TOOL_FILES = ("build_winpe.py",)
GUEST_FILES = ("startnet.cmd",)


def stage(source, prefix):
    """Create a throwaway tree that mirrors the installed package."""
    source, prefix = Path(source).resolve(), Path(prefix).absolute()
    if prefix.exists() and any(prefix.iterdir()):
        raise WptError("目标前缀必须不存在或为空；不会覆盖已有内容。")
    from .paths import Layout
    layout = Layout(prefix)
    program = layout.program_dir
    (program / "wpt").mkdir(parents=True)
    (program / "tools").mkdir()
    (layout.at("usr/bin")).mkdir(parents=True, exist_ok=True)
    layout.config_dir.mkdir(parents=True)
    layout.ensure_tree()
    for relative in SOURCE_FILES:
        data = (source / relative).read_bytes()
        target = (layout.at("usr/bin") / "winpe_terminal") if relative == "winpe_terminal" else program / relative
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o755 if relative == "winpe_terminal" else 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    (program / "guest").mkdir()
    for name in GUEST_FILES:
        target = program / "guest" / name
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write((source / "guest" / name).read_bytes())
    for name in TOOL_FILES:
        target = program / "tools" / name
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755)
        with os.fdopen(fd, "wb") as stream:
            stream.write((source / "tools" / name).read_bytes())
    atomic_json(layout.config, DEFAULTS.copy())
    atomic_json(layout.marker, {"app": APP_ID, "id": str(uuid.uuid4()), "created_at": now()})
    return layout
