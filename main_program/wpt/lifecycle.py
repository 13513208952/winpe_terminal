"""Cross-command serialization and shutdown orchestration."""
import fcntl
import os
import signal
import time
from pathlib import Path

from . import WptError
from .registry import matches, notify


class OperationLock:
    def __init__(self, path, exclusive=False):
        self.fd = os.open(Path(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def release(self):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release()


def signal_orphan(record):
    pid = record.get("qemu_pid")
    if type(pid) is not int or pid < 2:
        return False
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return False
    try:
        if not matches(record, "qemu"):
            return False
        signal.pidfd_send_signal(fd, signal.SIGKILL)
        return True
    finally:
        os.close(fd)


def request_stop(registry, *, target=None, force=False):
    selected = [item for item in registry.active() if target is None or item[1]["uuid"] == target]
    if target is not None and not selected:
        raise WptError(f"没有找到仍在运行的目标实例：{target}")
    for _, record, supervisor, qemu in selected:
        if supervisor:
            try:
                notify(record, force=force)
            except ProcessLookupError:
                pass
        elif qemu:
            if not force:
                raise WptError(f"实例 {record['uuid']} 的监督进程已丢失；需显式 -f 才能回收。")
            signal_orphan(record)
    return {item[1]["uuid"] for item in selected}


def await_stop(registry, selected, timeout):
    deadline = time.monotonic() + timeout
    while True:
        remaining = [item for item in registry.active() if item[1]["uuid"] in selected]
        if not remaining or time.monotonic() >= deadline:
            return remaining
        time.sleep(0.1)
