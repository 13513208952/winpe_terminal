"""Persistent instance history and PID-reuse-resistant process identities."""
import contextlib
import datetime
import fcntl
import os
import pwd
import re
import signal
import sys
import uuid
from pathlib import Path

from . import WptError
from .storage import atomic_json, read_json, safe_directory

RECORD_NAME = re.compile(r"[1-9][0-9]*_[0-9a-f-]{36}\.txt\Z")
TERMINAL_STATES = {"stopped", "failed"}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def process_info(pid):
    if type(pid) is not int or pid < 2:
        return None
    try:
        base = Path("/proc") / str(pid)
        fields = (base / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        args = [v.decode(errors="replace") for v in (base / "cmdline").read_bytes().split(b"\0") if v]
        return {
            "pid": pid,
            "ticks": int(fields[19]),
            "rss_bytes": int(fields[21]) * os.sysconf("SC_PAGE_SIZE"),
            "cpu_seconds": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
            "uid": base.stat().st_uid,
            "cwd": os.readlink(base / "cwd"),
            "args": args,
        }
    except (OSError, ValueError, IndexError):
        return None


def launcher_path():
    """Absolute path of the program entry that is running right now."""
    return str(Path(sys.argv[0]).resolve())


def matches(record, role):
    if record.get("boot_id") != boot_id():
        return None
    info = process_info(record.get(f"{role}_pid"))
    if not info or info["ticks"] != record.get(f"{role}_ticks"):
        return None
    if role == "supervisor":
        launch = record.get("launcher")
        scripts = [Path(a) if Path(a).is_absolute() else Path(info["cwd"]) / a
                   for a in info["args"] if Path(a).name == "winpe_terminal"]
        if info["uid"] != 0 or "start" not in info["args"]:
            return None
        if launch:
            if not any(str(p.resolve()) == launch for p in scripts):
                return None
        elif not scripts:
            return None
    else:
        args = info["args"]
        if "-uuid" not in args:
            return None
        index = args.index("-uuid")
        if index + 1 >= len(args) or args[index + 1] != record["uuid"]:
            return None
    return info


def notify(record, *, force=False):
    """Open a pidfd BEFORE verifying identity, then signal precisely that process."""
    pid = record.get("supervisor_pid")
    if type(pid) is not int or pid < 2:
        return False
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return False
    try:
        if not matches(record, "supervisor"):
            return False
        signal.pidfd_send_signal(fd, signal.SIGUSR2 if force else signal.SIGUSR1)
        return True
    finally:
        os.close(fd)


class Registry:
    def __init__(self, layout, installation_id):
        self.layout = layout
        self.directory = safe_directory(layout.instances)
        self.installation_id = installation_id

    @contextlib.contextmanager
    def locked(self):
        fd = os.open(self.directory / ".registry.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def create(self, user, device, config):
        with self.locked():
            sequence_file = self.directory / ".sequence.json"
            try:
                sequence = read_json(sequence_file)["last"]
                if type(sequence) is not int or sequence < 0:
                    raise WptError("实例序号记录损坏。")
            except FileNotFoundError:
                sequence = 0
            # Reserve before creating the record; failed attempts never reuse an ID.
            sequence += 1
            atomic_json(sequence_file, {"last": sequence})
            ident, created = str(uuid.uuid4()), now()
            supervisor = process_info(os.getpid())
            record = {
                "schema": 1, "installation_id": self.installation_id,
                "sequence": sequence, "uuid": ident, "created_at": created,
                "started_at": created, "user": user, "device": device,
                "mount_directory": device.get("mount_directory") if device else None, "state": "starting", "boot_id": boot_id(),
                "supervisor_pid": os.getpid(), "supervisor_ticks": supervisor["ticks"],
                "launcher": launcher_path(),
                "qemu_pid": None, "qemu_ticks": None,
                "memory_mib": config["memory_mib"], "cpus": config["cpus"],
            }
            path = self.directory / f"{sequence}_{ident}.txt"
            atomic_json(path, record, created_at=created)
            if os.geteuid() == 0 and type(user.get("uid")) is int:
                owner = pwd.getpwuid(user["uid"])
                os.chown(path, owner.pw_uid, owner.pw_gid, follow_symlinks=False)
            return path, record

    def update(self, path, record, **changes):
        record.update(changes, updated_at=now())
        with self.locked():
            atomic_json(path, record, created_at=record["created_at"])

    def records(self):
        result = []
        with self.locked():
            for path in self.directory.glob("*.txt"):
                if not RECORD_NAME.fullmatch(path.name):
                    continue
                record = read_json(path)
                try:
                    ident = str(uuid.UUID(record["uuid"]))
                    seq = record["sequence"]
                    if type(seq) is not int or seq < 1 or path.name != f"{seq}_{ident}.txt":
                        raise ValueError("record name mismatch")
                    if record["installation_id"] != self.installation_id:
                        raise ValueError("installation mismatch")
                    if record["state"] not in {"starting", "running", "stopping", *TERMINAL_STATES}:
                        raise ValueError("unknown state")
                except (KeyError, ValueError, TypeError) as exc:
                    raise WptError(f"实例记录无效：{path}") from exc
                result.append((path, record))
        return sorted(result, key=lambda item: item[1]["sequence"])

    def active(self):
        active = []
        for path, record in self.records():
            # Trust process identity, not just a persistent state string.
            supervisor = matches(record, "supervisor")
            qemu = matches(record, "qemu")
            if supervisor or qemu:
                active.append((path, record, supervisor, qemu))
        return active
