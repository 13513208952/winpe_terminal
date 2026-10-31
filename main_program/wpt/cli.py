"""User-facing command grammar. No parser action can launch a VM implicitly."""
import copy
import json
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import VERSION, WptError
from .config import Configuration
from .lifecycle import OperationLock, await_stop, request_stop
from .prepare import prepare, verify_state
from .registry import Registry
from .vm import Supervisor, require_root

STOP_COMMANDS = {"terminate", "stop", "shutdown", "end", "exit"}
PACKAGE = "winpe-terminal"
HELP = """winpe_terminal {version}
原创代码：MIT License；第三方许可独立适用。

sudo winpe_terminal prepare [--force] [--accept-microsoft-license]
sudo winpe_terminal start [-/dev/目标分区]
winpe_terminal setting
winpe_terminal reload
winpe_terminal -help
sudo winpe_terminal -status
sudo winpe_terminal <terminate|stop|shutdown|end|exit> [-f] [-x=UUID]
sudo winpe_terminal -unistall

prepare 首次下载并构建 Windows PE 资源，需要交互确认微软许可。
配置重载仅影响后续启动。关闭启动终端会关闭其对应实例。
存储参数必须是 -/dev/...；默认白名单为空，不自动卸载或修复设备。
-f 表示强制关机，可能丢失数据。
-x=UUID 只关闭指定实例；UUID 取 -status 输出的值，可与其他参数并用。
"""


@dataclass(frozen=True)
class Command:
    name: str
    device: str | None = None
    force: bool = False
    target: str | None = None
    argument: str | None = None


def parse(argv):
    if not argv:
        return Command("-help")
    name, *tail = argv
    if name in {"-help", "reload", "-status", "-unistall", "-instances-active"} and not tail:
        return Command(name)
    if name == "prepare":
        flags = set(tail)
        if flags <= {"--force", "--accept-microsoft-license"}:
            return Command(name, force="--force" in flags,
                           argument="--accept-microsoft-license" in flags)
        raise WptError("准备语法：prepare [--force] [--accept-microsoft-license]")
    if name == "setting":
        if not tail:
            return Command(name)
        if len(tail) == 2 and tail[0] == "--apply":
            return Command(name, argument=tail[1])
        raise WptError("设置语法：setting [--apply 文件]（--apply 为内部参数）")
    if name == "start":
        if not tail:
            return Command(name)
        if len(tail) == 1 and tail[0].startswith("-/dev/"):
            path = tail[0][1:]
            if path == os.path.normpath(path) and not any(ord(c) < 32 for c in path):
                return Command(name, device=path)
        raise WptError("启动语法：start [-/dev/目标分区]；不接受裸设备路径或其他参数。")
    if name in STOP_COMMANDS:
        force, target = False, None
        for item in tail:
            if item == "-f" and not force:
                force = True
                continue
            if item.startswith("-x=") and target is None:
                try:
                    target = str(uuid.UUID(item[3:]))
                except ValueError as exc:
                    raise WptError("目标 UUID 格式无效。") from exc
                if target != item[3:]:
                    raise WptError("目标 UUID 必须使用小写、带连字符的标准格式。")
                continue
            raise WptError("关机语法：<子命令> [-f] [-x=UUID]；不接受其他参数。")
        return Command(name, force=force, target=target)
    raise WptError("未知命令或参数；使用 winpe_terminal -help 查看用法。")


def reload_config(configuration):
    data = configuration.load()
    print(f"配置已重新读取并校验：{configuration.path}；仅影响后续启动的实例。")
    return data


def elevate_and_apply(configuration, draft):
    """Save a user-edited draft through a controlled pkexec round trip."""
    handle, name = tempfile.mkstemp(prefix="wpt-setting-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(draft, stream, ensure_ascii=False)
        entry = Path(sys.argv[0]).resolve()
        result = os.spawnv(os.P_WAIT, "/usr/bin/pkexec",
                           ["pkexec", "--disable-internal-agent", str(entry), "setting", "--apply", name])
        return result == 0
    finally:
        os.unlink(name)


def apply_setting(configuration, path):
    """Root side of `setting`: validate the draft, then replace the conffile."""
    require_root()
    draft = json.loads(Path(path).read_text(encoding="utf-8"))
    configuration.save(draft)
    print(f"配置已保存：{configuration.path}")
    return 0


def settings(configuration):
    data = reload_config(configuration)
    if not sys.stdin.isatty() or not sys.stdout.isatty() or os.environ.get("TERM", "dumb") == "dumb":
        print(f"当前终端不支持设置界面，请编辑配置文件：{configuration.path}")
        return 0
    fields = ("iso", "qemu", "memory_mib", "cpus", "boot_timeout_seconds", "shutdown_timeout_seconds")
    draft = copy.deepcopy(data)
    while True:
        print("\nwinpe_terminal 设置（设备白名单需直接编辑 JSON 配置）")
        for index, key in enumerate(fields, 1):
            print(f"  {index}. {key}: {draft[key]}")
        print("  s. 校验并保存    q. 不保存退出")
        try:
            choice = input("> ").strip()
        except EOFError:
            return 0
        if choice == "q":
            return 0
        if choice == "s":
            try:
                if os.geteuid() == 0:
                    configuration.save(draft)
                    print(f"配置已保存：{configuration.path}，仅影响后续启动。")
                elif elevate_and_apply(configuration, draft):
                    print("配置已保存，仅影响后续启动。")
                else:
                    print("未保存：授权被拒绝或失败。")
            except WptError as exc:
                print(f"配置未保存：{exc}")
                continue
            return 0
        if choice.isdecimal() and 1 <= int(choice) <= len(fields):
            key = fields[int(choice) - 1]
            try:
                value = input(f"{key} = ")
                draft[key] = int(value) if type(data[key]) is int else value
            except (ValueError, EOFError):
                print("输入无效，未修改此项。")


def status(registry):
    active = registry.active()
    if not active:
        print("没有正在运行的 WinPE 实例。")
        return 0
    for _, record, supervisor, qemu in active:
        print(f"{record['sequence']}  {record['uuid']}  {record['state']}")
        print(f"  启动：{record.get('started_at')}  时长：{uptime(record.get('started_at'))}")
        print(f"  supervisor PID：{supervisor['pid'] if supervisor else '已丢失'}"
              f"  QEMU PID：{qemu['pid'] if qemu else '未运行'}")
        if qemu:
            print(f"  QEMU RSS：{qemu['rss_bytes'] / 1048576:.1f} MiB"
                  f"  CPU 累计：{qemu['cpu_seconds']:.2f} s")
        print(f"  内存：{record.get('memory_mib')} MiB / {record.get('cpus')} vCPU")
        device = record.get('device')
        print(f"  设备：{device['path'] if device else '未接入'}")
    return 0


def uptime(started_at):
    """Elapsed time since the record's UTC start timestamp, human readable."""
    try:
        started = datetime.fromisoformat(started_at)
    except (TypeError, ValueError):
        return "未知"
    seconds = max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours} 小时 {minutes} 分 {seconds} 秒" if hours else f"{minutes} 分 {seconds} 秒"


def confirm(message):
    if not sys.stdin.isatty():
        raise WptError("确认操作必须在交互终端完成，不接受管道自动确认。")
    try:
        return input(message + " [输入 yes 确认，其余取消] ").strip() == "yes"
    except EOFError:
        return False


def stop_instances(registry, config):
    """Ask every instance to shut down; force only with explicit consent."""
    selected = {record["uuid"] for _, record, _, _ in registry.active()}
    if not selected:
        return True
    try:
        request_stop(registry)
    except WptError as exc:
        print(exc)
    remaining = await_stop(registry, selected, config["shutdown_timeout_seconds"])
    if not remaining:
        return True
    if not confirm("仍有实例未关机。强制关机并继续卸载？"):
        print("已取消；已发出的正常关机请求不会撤回。")
        return False
    request_stop(registry, force=True)
    if await_stop(registry, selected, 10):
        raise WptError("实例未完全退出，已停止操作；不会释放仍在使用的资源。")
    return True


def dpkg_installed(layout):
    marker = layout.at("var/lib/dpkg/info", PACKAGE + ".list")
    return marker.is_file()


def uninstall(layout, registry, config):
    print(f"卸载目标：{PACKAGE}")
    print("将停止并回收 WinPE 实例，然后由 APT 清除程序、配置、镜像、缓存、实例记录与日志。")
    print("不会删除任何物理分区内容，也不影响系统安装的其他软件。")
    print(f"数据目录：{layout.state_dir}   缓存：{layout.cache_dir}   日志：{layout.logs}")
    if not confirm("确认卸载？"):
        print("已取消卸载。")
        return 0
    with OperationLock(layout.lock, exclusive=True):
        if not stop_instances(registry, config):
            return 0
        if not dpkg_installed(layout):
            raise WptError("未检测到 Debian 软件包记录；请用 apt purge winpe-terminal 处理。")
        print("交由 APT 完成卸载……", flush=True)
    # Never call APT while holding our own lock file inside the package data.
    return os.spawnv(os.P_WAIT, "/usr/bin/apt-get", ["apt-get", "purge", PACKAGE])


def main(layout, argv=None):
    try:
        command = parse(list(sys.argv[1:] if argv is None else argv))
        if command.name == "-help":
            print(HELP.format(version=VERSION))
            return 0
        configuration = Configuration(layout)
        if command.name == "reload":
            reload_config(configuration)
            return 0
        if command.name == "setting":
            if command.argument:
                return apply_setting(configuration, command.argument)
            return settings(configuration)
        require_root()
        marker = configuration.ensure_installation()
        registry = Registry(layout, marker["id"])
        if command.name == "prepare":
            return prepare(layout, configuration, force=command.force,
                           pre_accepted=bool(command.argument))
        if command.name == "-instances-active":
            return verify_state(layout)
        if command.name == "start":
            with OperationLock(layout.lock) as guard:
                config = configuration.load()
                return Supervisor(configuration, config, registry).run(
                    command.device, startup_complete=guard.release)
        if command.name == "-status":
            return status(registry)
        try:
            config = configuration.load()
        except (WptError, OSError, ValueError) as exc:
            from .config import DEFAULTS
            config = DEFAULTS.copy()
            print(f"配置无法读取，管理操作采用默认超时，不阻止安全关机：{exc}", file=sys.stderr)
        if command.name in STOP_COMMANDS:
            selected = request_stop(registry, target=command.target, force=command.force)
            remaining = await_stop(registry, selected, 10 if command.force else config["shutdown_timeout_seconds"])
            if remaining:
                raise WptError("正常关机未完成或清理超时；未擅自强制关闭。可查询状态后显式使用 -f。")
            print(f"已关闭 {len(selected)} 个实例。")
            return 0
        return uninstall(layout, registry, config)
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    except (WptError, OSError, ValueError, KeyError) as exc:
        print(f"winpe_terminal：{exc}", file=sys.stderr)
        return 1
