"""Strict data-only configuration: no shell, plugins, or arbitrary QEMU options."""
import os
from pathlib import Path

from . import WptError
from .storage import atomic_json, read_json, safe_directory

APP_ID = "org.winpe-terminal.installation.v1"
DEFAULTS = {
    "schema": 1,
    "qemu": "/usr/bin/qemu-system-x86_64",
    "iso": "assets/winpe.iso",
    "memory_mib": 1024,
    "cpus": 2,
    "boot_timeout_seconds": 180,
    "shutdown_timeout_seconds": 30,
    "allowed_devices": [],
}


def validate(data):
    if not isinstance(data, dict) or set(data) != set(DEFAULTS):
        raise WptError("配置字段必须与 schema 1 完全匹配；不接受未知字段。")
    for key, low, high in (
        ("schema", 1, 1), ("memory_mib", 256, 4096), ("cpus", 1, 8),
        ("boot_timeout_seconds", 5, 1800), ("shutdown_timeout_seconds", 1, 600),
    ):
        value = data[key]
        if type(value) is not int or not low <= value <= high:
            raise WptError(f"{key} 必须是 {low} 到 {high} 的整数。")
    for key in ("qemu", "iso"):
        value = data[key]
        if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
            raise WptError(f"{key} 必须是非空路径。")
    if not Path(data["qemu"]).is_absolute():
        raise WptError("qemu 必须使用绝对路径。")
    devices = data["allowed_devices"]
    if not isinstance(devices, list):
        raise WptError("allowed_devices 必须为列表。")
    seen = set()
    for item in devices:
        if not isinstance(item, dict) or set(item) != {"path", "serial", "size_bytes"}:
            raise WptError("每个 allowed_devices 项必须且仅包含 path、serial、size_bytes。")
        path, serial, size = item["path"], item["serial"], item["size_bytes"]
        if not isinstance(path, str) or not path.startswith("/dev/") or Path(path).as_posix() != os.path.normpath(path):
            raise WptError("设备白名单必须使用规范的 /dev/ 绝对路径。")
        if any(ord(c) < 32 for c in path):
            raise WptError("设备路径含控制字符。")
        if path in seen:
            raise WptError("设备白名单路径重复。")
        if not isinstance(serial, str) or not serial.strip() or type(size) is not int or size <= 0:
            raise WptError("设备白名单必须包含非空序列号和精确的正整数字节容量。")
        seen.add(path)
    return data


class Configuration:
    """The conffile in /etc plus the state marker in /var/lib."""

    def __init__(self, layout):
        self.layout = layout
        self.path = layout.config

    def load(self):
        safe_directory(self.layout.config_dir)
        try:
            return validate(read_json(self.path))
        except FileNotFoundError as exc:
            raise WptError(f"配置不存在：{self.path}；软件包可能未正确安装。") from exc

    def save(self, data):
        atomic_json(self.path, validate(data))

    def resolve(self, value):
        """Relative paths are relative to the state directory, not /usr."""
        path = Path(value)
        return path if path.is_absolute() else self.layout.state_dir / path

    def installation(self):
        try:
            marker = read_json(self.layout.marker)
            if marker["app"] != APP_ID:
                raise ValueError("wrong application")
            import uuid
            uuid.UUID(marker["id"])
            return marker
        except FileNotFoundError:
            raise WptError("尚未初始化运行状态；请先以 root 运行 winpe_terminal prepare。") from None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise WptError(f"运行状态记录无效：{self.layout.marker}") from exc

    def ensure_installation(self):
        """Create the state marker on first privileged use.

        This is deliberately not done in a dpkg maintainer script: the marker
        is runtime state, it must survive upgrades, and dpkg should not own it.
        """
        try:
            return self.installation()
        except WptError:
            pass
        import uuid
        from .registry import now
        from .storage import atomic_json
        self.layout.ensure_tree()
        marker = {"app": APP_ID, "id": str(uuid.uuid4()), "created_at": now()}
        atomic_json(self.layout.marker, marker)
        return marker
