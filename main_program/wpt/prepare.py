"""`prepare`: obtain the official WinPE components and build the local image.

The privileged entry only orchestrates. Downloads, extraction and the image
build run as the invoking user inside /var/cache/winpe-terminal; only the
verified result is published into /var/lib/winpe-terminal/assets by root.
"""
import json
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

from . import WptError
from .storage import atomic_json, read_json

# Executable -> Debian package that provides it.
REQUIRED_TOOLS = {
    "curl": "curl",
    "7z": "7zip",
    "xorriso": "xorriso",
    "msiinfo": "msitools",
    "wimlib-imagex": "wimtools",
    "hivexsh": "libhivex-bin",
    "hivexml": "libhivex-bin",
}

LICENSE_NOTE = """\
Windows PE 由微软的 Windows ADK 附加组件提供，受微软许可约束，不能随本项目分发。
本项目只从微软官方来源下载组件，在本机提取并构建镜像，不执行微软安装器。

官方说明：https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/winpe-intro
ADK 下载： https://learn.microsoft.com/en-us/windows-hardware/get-started/adk-install
"""


def missing_tools():
    missing = []
    for tool, package in REQUIRED_TOOLS.items():
        if shutil.which(tool) is None:
            missing.append((tool, package))
    return missing


def builder_path():
    return Path(__file__).resolve().parent.parent / "tools" / "build_winpe.py"


def invoking_account():
    """The unprivileged user who asked for this, or a hard error."""
    uid = os.environ.get("SUDO_UID") or os.environ.get("PKEXEC_UID")
    try:
        account = pwd.getpwuid(int(uid))
    except (TypeError, ValueError, KeyError):
        raise WptError("无法确定发起 prepare 的普通用户；请用 sudo 运行。") from None
    if account.pw_uid == 0:
        raise WptError("prepare 需要一个非 root 用户来执行下载与构建。")
    return account


def accept_license(pre_accepted=False):
    """Never accept the licence silently, but allow an explicit flag."""
    print(LICENSE_NOTE, flush=True)
    if pre_accepted:
        print("已通过 --accept-microsoft-license 确认接受该许可。", flush=True)
        return True
    print("你需要在资源准备前自行阅读并接受对应版本的微软许可。", flush=True)
    if not sys.stdin.isatty():
        raise WptError("许可确认必须在交互终端完成，或显式传入 --accept-microsoft-license。")
    prompt = "输入 accept 表示你已阅读并接受该许可，其他任何输入取消： "
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        answer = sys.stdin.readline().strip()
    except (EOFError, OSError):
        return False
    return answer == "accept"


def prepare_cache(layout, account):
    """Cache directories belong to the unprivileged builder."""
    for path in (layout.cache_dir, layout.downloads, layout.build, layout.tmp):
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, account.pw_uid, account.pw_gid)
        os.chmod(path, 0o750)


def existing_state(layout):
    """Describe an already prepared image, if its receipt still matches."""
    try:
        receipt = read_json(layout.receipt)
    except (OSError, WptError):
        return None
    iso = layout.iso
    if not iso.is_file():
        return None
    import hashlib
    digest = hashlib.sha256()
    with iso.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != receipt.get("iso_sha256"):
        return None
    return receipt


def run_builder(layout, account, force):
    staged = layout.build / "publish"
    if staged.exists():
        shutil.rmtree(staged)
    staged.mkdir(parents=True)
    os.chown(staged, account.pw_uid, account.pw_gid)
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": account.pw_dir,
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(layout.tmp),
    }
    args = [sys.executable, "-I", str(builder_path()),
            "--downloads", str(layout.downloads),
            "--build", str(layout.build),
            "--output", str(staged),
            "--accept-microsoft-license"]
    if force:
        args.append("--force")
    print("以普通用户执行下载与构建：", account.pw_name, flush=True)
    try:
        subprocess.run(["/usr/sbin/runuser", "--user", account.pw_name, "--",
                        "/usr/bin/env", *[f"{k}={v}" for k, v in env.items()],
                        *args], check=True)
    except subprocess.CalledProcessError as exc:
        raise WptError(f"构建失败（退出码 {exc.returncode}）；缓存已保留，可重新运行 prepare 续传。") from None
    return staged


def cleanup_build(layout):
    """Remove scratch images: the download cache is what is worth keeping."""
    import shutil
    for pattern in ("winpe-*", "publish", "adk", "winpe-addon"):
        for path in layout.build.glob(pattern):
            shutil.rmtree(path, ignore_errors=True)


def publish(layout, staged):
    """Move the verified image into the state directory as root."""
    import hashlib
    try:
        receipt = read_json(staged / "winpe-build.json")
    except (OSError, WptError) as exc:
        raise WptError(f"构建结果缺少回执：{exc}") from None
    iso = staged / "winpe.iso"
    if not iso.is_file():
        raise WptError("构建结果缺少 ISO。")
    digest = hashlib.sha256()
    with iso.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != receipt.get("iso_sha256"):
        raise WptError("构建出的 ISO 与回执哈希不一致，拒绝发布。")
    layout.assets.mkdir(parents=True, exist_ok=True)
    for source, target in ((iso, layout.iso), (staged / "winpe-build.json", layout.receipt)):
        temporary = target.with_name("." + target.name + ".new")
        shutil.copyfile(source, temporary)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    directory = os.open(layout.assets, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return receipt


def prepare(layout, configuration, force=False, pre_accepted=False):
    configuration.load()  # validate before doing any work
    missing = missing_tools()
    if missing:
        names = "、".join(f"{tool}（{package}）" for tool, package in missing)
        raise WptError(f"缺少必需工具：{names}。请先修复软件包依赖。")
    existing = existing_state(layout)
    if existing and not force:
        print(f"镜像已就绪：{layout.iso}", flush=True)
        print(f"  SHA256：{existing['iso_sha256']}")
        print(f"  构建时间：{existing.get('built_at', '未知')}")
        print("如需重新构建，请加 --force。")
        return 0
    account = invoking_account()
    print(f"下载与构建将以用户 {account.pw_name} 执行，产物发布需要 root。", flush=True)
    if not accept_license(pre_accepted):
        print("未接受许可，已取消。")
        return 1
    prepare_cache(layout, account)
    # One scratch set at a time: a previous failed run is never needed, the
    # verified downloads are what makes a retry cheap.
    cleanup_build(layout)
    staged = run_builder(layout, account, force)
    receipt = publish(layout, staged)
    cleanup_build(layout)
    print(f"镜像已发布：{layout.iso}", flush=True)
    print(f"  SHA256：{receipt['iso_sha256']}")
    print("下一步：sudo winpe_terminal start")
    return 0


def verify_state(layout):
    """Exit status for maintainer scripts: 0 when no instance is running."""
    from .config import Configuration
    from .registry import Registry
    configuration = Configuration(layout)
    try:
        marker = configuration.installation()
    except WptError:
        return 0
    active = Registry(layout, marker["id"]).active()
    if not active:
        return 0
    for _, record, _, _ in active:
        print(f"实例仍在运行：{record['sequence']} {record['uuid']} {record['state']}",
              file=sys.stderr)
    return 3
