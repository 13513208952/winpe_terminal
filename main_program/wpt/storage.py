"""Small no-follow file primitives; never put runtime files in /tmp."""
import json
import os
import stat
import uuid
from pathlib import Path

from . import WptError


def safe_directory(path):
    path = Path(path).absolute()
    for part in reversed((path, *path.parents)):
        if part.is_symlink():
            raise WptError(f"拒绝符号链接目录：{part}")
    if not path.is_dir():
        raise WptError(f"目录不存在：{path}")
    return path


def read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise WptError(f"不是普通文件：{path}")
        text = stream.read(1024 * 1024 + 1)
        if len(text) > 1024 * 1024:
            raise WptError(f"文件过大：{path}")
        try:
            return json.loads(text)
        except (ValueError, UnicodeError) as exc:
            raise WptError(f"无效 JSON：{path}: {exc}") from exc


def atomic_json(path, data, *, created_at=None):
    path = Path(path)
    parent = safe_directory(path.parent)
    temporary = parent / (".write-" + str(uuid.uuid4()))
    fd = None
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            previous = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            previous = None
        if previous is not None:
            if not stat.S_ISREG(previous.st_mode):
                raise WptError(f"拒绝替换非普通文件：{path}")
            if os.geteuid() == 0:
                os.fchown(fd, previous.st_uid, previous.st_gid)
            os.fchmod(fd, stat.S_IMODE(previous.st_mode))
        raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(raw)
            stream.flush()
            if created_at is not None:
                try:
                    os.setxattr(stream.fileno(), "user.winpe_terminal.created_at", created_at.encode("ascii"))
                except OSError as exc:
                    raise WptError(f"记录目录不支持必需的创建时间扩展属性：{parent}: {exc}") from exc
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        dfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            # Some removable filesystems do not support directory fsync.
            try:
                os.fsync(dfd)
            except OSError:
                pass
        finally:
            os.close(dfd)
    finally:
        if fd is not None:
            os.close(fd)
        temporary.unlink(missing_ok=True)
