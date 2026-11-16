"""Fail-closed block-device selection. No unmount, repair, or formatting code."""
import array
import fcntl
import json
import os
import stat
import subprocess
from pathlib import Path

from . import WptError

BLKGETSIZE64 = 0x80081272
NTFS_TYPES = {"ntfs", "ntfs3"}


def session_user():
    """Name of the user who started the program, for udisks-style paths."""
    import pwd
    for key in ("SUDO_USER", "PKEXEC_USER"):
        name = os.environ.get(key, "").strip()
        if name:
            return name
    uid = os.environ.get("SUDO_UID") or os.environ.get("PKEXEC_UID")
    try:
        return pwd.getpwuid(int(uid)).pw_name if uid else pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, ValueError):
        return ""


def host_mount_path(node):
    """Where this partition is (or would be) mounted on the host.

    The program refuses to run against a mounted partition, so in the normal
    case there is no live mount point. Recording the udisks2 convention
    (/run/media/<user>/<label>) still tells the user where the volume appears
    when they mount it again. Anything that cannot be established stays None.
    """
    points = [p for p in (node.get("mountpoints") or []) if p]
    if points:
        return ",".join(points)
    label = (node.get("label") or "").strip()
    user = session_user()
    if label and user:
        return f"/run/media/{user}/{label}"
    return None


def inventory():
    try:
        result = subprocess.run(
            ["/usr/bin/lsblk", "--json", "--bytes", "--output",
             "PATH,TYPE,PKNAME,MAJ:MIN,SIZE,RO,MOUNTPOINTS,SERIAL,LABEL,FSTYPE"],
            check=True, capture_output=True, text=True, timeout=10,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
        tree = json.loads(result.stdout)["blockdevices"]
        nodes = {}

        def visit(node, parent=None):
            path = node["path"]
            old = nodes.get(path)
            parents = set(old["parents"]) if old else set()
            if parent:
                parents.add(parent)
            if node.get("pkname"):
                parents.add("/dev/" + node["pkname"].removeprefix("/dev/"))
            nodes[path] = {**node, "parents": parents}
            for child in node.get("children", []):
                visit(child, path)

        for node in tree:
            visit(node)
        return nodes
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        raise WptError(f"无法可靠读取设备关系，拒绝透传：{exc}") from exc


def ancestors(path, nodes):
    found, pending = set(), [path]
    while pending:
        current = pending.pop()
        if current in found:
            continue
        if current not in nodes:
            raise WptError(f"设备父子关系不完整：{current}")
        found.add(current)
        pending.extend(nodes[current]["parents"])
    return found


def ntfs_paths(nodes):
    """Return every partition that lsblk positively identifies as NTFS."""
    paths = sorted(path for path, node in nodes.items()
                   if node.get("type") == "part"
                   and str(node.get("fstype") or "").lower() in NTFS_TYPES)
    if not paths:
        raise WptError("未检测到 NTFS 分区；不会启动空的自动修复会话。")
    return paths


def check_safe_partition(path, nodes, protected_devnums, mounted_devnums, swapped_paths, holders):
    node = nodes.get(path)
    if node is None or node["type"] != "part":
        raise WptError("仅支持已划定的分区，不允许整盘或未知设备。")
    family = ancestors(path, nodes)
    if not protected_devnums <= {info["maj:min"] for info in nodes.values()}:
        raise WptError("无法将宿主根文件系统或安装目录映射到块设备，拒绝透传。")
    # Sharing a physical disk with the host is allowed: dual-boot users keep
    # Windows and Linux on one SSD. The host's live partitions stay off limits.
    if node["maj:min"] in protected_devnums:
        raise WptError("目标就是宿主根文件系统或安装目录所在分区，禁止接入。")
    if node.get("ro"):
        raise WptError("目标设备只读。")
    if any(nodes[p]["maj:min"] in mounted_devnums or any(nodes[p].get("mountpoints") or []) for p in family):
        raise WptError("目标已挂载；不会自动卸载宿主文件系统。")
    if path in swapped_paths or holders:
        raise WptError("目标正用于 swap 或被其他块设备持有。")
    return node, family


def check_selection(path, allowed, nodes, protected_devnums, mounted_devnums, swapped_paths, holders):
    """Apply the explicit-start whitelist and the shared live-device policy."""
    item = next((entry for entry in allowed if entry["path"] == path), None)
    if item is None:
        raise WptError("目标不在精确设备白名单中；默认禁用物理设备透传。")
    node, family = check_safe_partition(
        path, nodes, protected_devnums, mounted_devnums, swapped_paths, holders)
    if int(node["size"]) != item["size_bytes"]:
        raise WptError("目标容量与白名单不一致。")
    serials = {nodes[p].get("serial", "").strip() for p in family if nodes[p].get("serial")}
    if serials != {item["serial"].strip()}:
        raise WptError("目标序列号缺失、歧义或与白名单不一致。")
    return node


def check_ntfs_selection(path, nodes, protected_devnums, mounted_devnums, swapped_paths, holders):
    """Apply automatic-chkdsk policy without consulting the manual whitelist."""
    node, _ = check_safe_partition(
        path, nodes, protected_devnums, mounted_devnums, swapped_paths, holders)
    if str(node.get("fstype") or "").lower() not in NTFS_TYPES:
        raise WptError("自动修复目标不再是 NTFS 分区，拒绝继续。")
    return node


def inspect_target(path, config, home, *, automatic_ntfs=False):
    path = str(Path(path).resolve(strict=True))
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISBLK(info.st_mode):
        raise WptError("指定对象不是块设备。")
    nodes = inventory()
    protected_devnums = set()
    for filesystem in (Path("/"), Path(home)):
        dev = filesystem.stat().st_dev
        protected_devnums.add(f"{os.major(dev)}:{os.minor(dev)}")
    mounts = {line.split()[2] for line in Path("/proc/self/mountinfo").read_text().splitlines()}
    swaps = set()
    for line in Path("/proc/swaps").read_text().splitlines()[1:]:
        swaps.add(os.path.realpath(line.split()[0]))
    devnum = f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"
    holder_path = Path("/sys/dev/block") / devnum / "holders"
    holders = list(holder_path.iterdir())  # Missing sysfs is a hard failure.
    if automatic_ntfs:
        node = check_ntfs_selection(path, nodes, protected_devnums, mounts, swaps, holders)
    else:
        node = check_selection(path, config["allowed_devices"], nodes, protected_devnums, mounts, swaps, holders)
    if node["maj:min"] != devnum:
        raise WptError("设备身份在检查期间发生变化。")
    return path, node, info.st_rdev


def open_target(path, config, home, *, automatic_ntfs=False):
    canonical, node, rdev = inspect_target(path, config, home, automatic_ntfs=automatic_ntfs)
    try:
        fd = os.open(canonical, os.O_RDWR | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise WptError(f"无法取得设备独占权：{canonical}: {exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISBLK(st.st_mode) or st.st_rdev != rdev:
            raise WptError("打开后的设备身份不匹配。")
        size = array.array("Q", [0])
        fcntl.ioctl(fd, BLKGETSIZE64, size, True)
        if size[0] != int(node["size"]):
            raise WptError("设备实际容量与检查结果不一致。")
        inspect_target(canonical, config, home, automatic_ntfs=automatic_ntfs)
        sysdev = (Path("/sys/dev/block") / f"{os.major(rdev)}:{os.minor(rdev)}").resolve(strict=True)
        start_sector = int((sysdev / "start").read_text().strip())
        logical_sector = int((sysdev.parent / "queue/logical_block_size").read_text().strip())
        if logical_sector != 512:
            raise WptError("当前仅支持 512 字节逻辑扇区的分区。")
        serial = None if automatic_ntfs else next(
            item["serial"] for item in config["allowed_devices"] if item["path"] == canonical)
        return fd, {"path": canonical, "size_bytes": size[0], "start_sector": start_sector,
                    "serial": serial, "mount_directory": host_mount_path(node)}
    except BaseException:
        os.close(fd)
        raise
