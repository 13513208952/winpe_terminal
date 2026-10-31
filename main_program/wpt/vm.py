"""One root supervisor per terminal, one unprivileged QEMU child per supervisor."""
import contextlib
import ctypes
import json
import os
import pwd
import selectors
import signal
import socket
import stat
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path

from . import WptError
from .partition import NBDService, PartitionDisk
from .registry import now, process_info
from .safety import open_target
from .sac import SacConsole, Tail
from .qmp import QMP

_LIBC = ctypes.CDLL(None, use_errno=True)


def require_root():
    if os.geteuid() != 0:
        raise WptError("此操作需要 sudo／root 权限；不会尝试自行提权。")


def initiating_user():
    # Direct root launches still run QEMU without root privileges.
    value = os.environ.get("SUDO_UID", os.environ.get("PKEXEC_UID", "0"))
    try:
        uid = int(value)
        account = pwd.getpwuid(uid if uid > 0 else pwd.getpwnam("nobody").pw_uid)
        original = pwd.getpwuid(uid)
    except (ValueError, KeyError) as exc:
        raise WptError("无法识别启动用户。") from exc
    return account, {"name": original.pw_name, "uid": original.pw_uid}


def child_guard(parent_pid):
    # Called after subprocess has dropped uid/gid, and before exec.
    if _LIBC.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        os._exit(126)
    if os.getppid() != parent_pid:
        os._exit(126)
    os.umask(0o077)


def qemu_arguments(binary, config, ident, iso_fd, qmp_fd, serial_fd, nbd=None):
    args = [str(binary), "-name", "winpe_terminal-" + ident, "-uuid", ident,
            "-machine", "pc,accel=kvm", "-cpu", "host", "-m", str(config["memory_mib"]),
            "-smp", str(config["cpus"]), "-nodefaults", "-no-user-config", "-no-reboot",
            "-display", "none", "-device", "VGA,vgamem_mb=8", "-nic", "none",
            "-boot", "order=d,strict=on",
            "-chardev", f"socket,id=qmp,fd={qmp_fd},server=off", "-mon", "chardev=qmp,mode=control",
            "-chardev", f"socket,id=terminal,fd={serial_fd},server=off",
            "-serial", "chardev:terminal",
            "-chardev", "file,id=firmware,path=/proc/self/fd/2,append=on",
            "-device", "isa-debugcon,iobase=0x402,chardev=firmware",
            "-add-fd", f"fd={iso_fd},set=1,opaque=winpe-iso",
            "-drive", "if=none,id=boot,format=raw,readonly=on,file=/dev/fdset/1",
            "-device", "ide-cd,drive=boot,bus=ide.1"]
    if nbd:
        args += ["-blockdev", f"driver=nbd,node-name=target,server.type=fd,server.str={nbd}",
                 "-device", "ide-hd,drive=target,bus=ide.0"]
    return args


class Terminal:
    def __init__(self, fd=0):
        self.fd, self.saved = fd, None

    def raw(self):
        if self.saved is None:
            self.saved = termios.tcgetattr(self.fd)
            tty.setraw(self.fd, termios.TCSANOW)

    def restore(self):
        if self.saved is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSANOW, self.saved)
            except (OSError, termios.error):
                pass
            self.saved = None


def write_all(fd, data):
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise WptError("终端输出已断开。")
        view = view[count:]


class Supervisor:
    def __init__(self, configuration, config, registry):
        self.configuration, self.config, self.registry = configuration, config, registry
        self.request = None
        self.resized = True
        self.log_fd = None
        self.guest_ready = False
        self.serial_received = 0

    def log(self, event, **fields):
        if self.log_fd is not None:
            value = {"time": now(), "component": "host", "event": event, **fields}
            os.write(self.log_fd, (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))

    def handle_signal(self, number, _frame):
        if number == signal.SIGHUP:
            self.terminal_closed = True
        if number == signal.SIGWINCH:
            self.resized = True
        elif number == signal.SIGUSR2:
            self.request = "force"
        elif self.request is None:
            self.request = "graceful"

    def run(self, device_path=None, startup_complete=None):
        require_root()
        if not os.isatty(0) or not os.isatty(1):
            raise WptError("start 必须在可交互终端运行；不支持后台或管道启动。")
        if not Path("/dev/kvm").exists():
            raise WptError("/dev/kvm 不存在；不允许静默退回软件模拟。")
        account, user = initiating_user()
        binary = Path(self.config["qemu"])
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise WptError(f"QEMU 不可用：{binary}")
        iso = self.configuration.resolve(self.config["iso"])
        if not iso.is_file():
            raise WptError(f"WinPE 资源尚未准备：{iso}\n请先运行：sudo winpe_terminal prepare")
        terminal, process, nbd, path, record = Terminal(), None, None, None, None
        handlers = {}
        log_path = None
        failed = None
        with contextlib.ExitStack() as stack:
            try:
                for number in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT, signal.SIGUSR1, signal.SIGUSR2, signal.SIGWINCH):
                    handlers[number] = signal.signal(number, self.handle_signal)
                iso_fd = os.open(iso, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                stack.callback(os.close, iso_fd)
                if not stat.S_ISREG(os.fstat(iso_fd).st_mode):
                    raise WptError("ISO 必须是普通文件。")
                device, device_fd = None, None
                if device_path:
                    device_fd, device = open_target(device_path, self.config, self.configuration.layout.state_dir)
                    stack.callback(os.close, device_fd)
                path, record = self.registry.create(user, device, self.config)
                if device:
                    disk = PartitionDisk(device_fd, device["size_bytes"], device["start_sector"], record["uuid"])
                    nbd = NBDService(disk)
                    stack.callback(nbd.close)
                qmp_host, qmp_child = socket.socketpair()
                serial_host, serial_child = socket.socketpair()
                for sock in (qmp_host, qmp_child, serial_host, serial_child):
                    stack.callback(sock.close)
                log_path = self.configuration.layout.logs / (record["uuid"] + ".log")
                log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_APPEND, 0o600)
                stack.callback(os.close, log_fd)
                self.log_fd = log_fd
                log_owner = pwd.getpwuid(user["uid"])
                os.fchown(log_fd, log_owner.pw_uid, log_owner.pw_gid)
                args = qemu_arguments(binary, self.config, record["uuid"], iso_fd,
                                      qmp_child.fileno(), serial_child.fileno(), nbd.client.fileno() if nbd else None)
                self.log("launch", uuid=record["uuid"], memory_mib=self.config["memory_mib"], iso=str(iso), device=device, qemu_args=args)
                print(f"启动实例 {record['sequence']} / {record['uuid']}；等待 WinPE 终端桥接……", flush=True)
                print(f"启动日志：{log_path}", flush=True)
                parent_pid = os.getpid()
                process = subprocess.Popen(
                    args, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd,
                    pass_fds=(iso_fd, qmp_child.fileno(), serial_child.fileno()) + ((nbd.client.fileno(),) if nbd else ()),
                    close_fds=True, start_new_session=True,
                    user=account.pw_uid, group=account.pw_gid,
                    extra_groups=os.getgrouplist(account.pw_name, account.pw_gid),
                    preexec_fn=lambda: child_guard(parent_pid),
                    cwd="/", env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LC_ALL": "C"},
                )
                if nbd:
                    nbd.start()
                qmp_child.close()
                serial_child.close()
                info = process_info(process.pid)
                if not info:
                    raise WptError("QEMU 在初始化期间退出。")
                self.registry.update(path, record, qemu_pid=process.pid, qemu_ticks=info["ticks"])
                qmp = QMP(qmp_host)
                qmp.handshake()
                self.log("qmp_connected", status=qmp.command("query-status"))
                if startup_complete:
                    startup_complete()
                self.interact(process, serial_host, qmp, terminal, path, record, nbd)
                if not self.guest_ready and self.request is None:
                    raise WptError("虚拟机在终端桥接就绪之前退出；QEMU 正常退出不等于 WinPE 桥接启动成功。")
                if process.returncode != 0 and self.request != "force":
                    raise WptError(f"QEMU 异常退出，返回码 {process.returncode}。")
            except BaseException as exc:
                failed = str(exc) or type(exc).__name__
                self.log("error", message=failed)
                raise
            finally:
                # Restore the terminal even if waiting for a dying child fails.
                terminal.restore()
                # Even SIGKILL of this supervisor kills QEMU via PDEATHSIG.
                # On catchable exits, reap first, then release target descriptors.
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                if nbd is not None:
                    nbd.close()
                for number, handler in handlers.items():
                    signal.signal(number, handler)
                if path is not None:
                    self.registry.update(path, record, state="failed" if failed else "stopped",
                                         stopped_at=now(), error=failed,
                                         qemu_returncode=process.returncode if process else None,
                                         guest_ready=self.guest_ready,
                                         forced=bool(process and process.returncode is not None and process.returncode < 0),
                                         terminal_closed=getattr(self, "terminal_closed", False))
                self.log("exit", returncode=process.returncode if process else None, guest_ready=self.guest_ready,
                         serial_received=self.serial_received, terminal_closed=getattr(self, "terminal_closed", False))
                self.log_fd = None
                if failed and log_path:
                    print(f"\nQEMU 日志：{log_path}", file=sys.stderr)
        return 0

    def interact(self, process, serial, qmp, terminal, path, record, nbd):
        """Wait for the SAC console, enter the CMD channel, then pass through I/O.

        Boot output is deliberately not shown: winload paints a full-screen
        progress display over the serial line. Only SAC and shell traffic is
        forwarded to the user's terminal.
        """
        serial.setblocking(False)
        console, tail = SacConsole(serial), Tail()
        state, stopping, shutdown_deadline = "boot", False, None
        shell_deadline = None
        shutdown_stage, shutdown_sent_at = None, 0.0
        self.guest_shell = False
        switched, view_key_sent = set(), False
        boot_deadline = time.monotonic() + self.config["boot_timeout_seconds"]
        next_snapshot = time.monotonic() + 10
        with selectors.DefaultSelector() as selector:
            selector.register(serial, selectors.EVENT_READ, "serial")
            selector.register(0, selectors.EVENT_READ, "terminal")
            while process.poll() is None:
                current = time.monotonic()
                if nbd and nbd.error:
                    raise WptError("分区代理失败：" + nbd.error)
                if current >= next_snapshot and (state != "shell" or stopping):
                    try:
                        status = qmp.command("query-status", timeout=1)
                    except (WptError, OSError) as exc:
                        status = {"error": str(exc)}
                    proc = process_info(process.pid)
                    self.log("boot_snapshot", qmp=status, serial_received=self.serial_received,
                             state=state, rss_bytes=proc["rss_bytes"] if proc else None,
                             cpu_seconds=proc["cpu_seconds"] if proc else None)
                    next_snapshot = current + 10
                if self.request == "force":
                    process.kill()
                    process.wait(timeout=10)
                    return
                if self.request == "graceful" and not stopping:
                    stopping = True
                    shutdown_deadline = current + self.config["shutdown_timeout_seconds"]
                    self.registry.update(path, record, state="stopping")
                    shutdown_stage = self.guest_shutdown(console, qmp, state)
                    shutdown_sent_at = current
                    self.log("shutdown_request", stage=shutdown_stage, channel=state)
                elif stopping and shutdown_stage and current - shutdown_sent_at >= 10:
                    # The guest may miss the first request (channel not yet
                    # reading input). Retry the clean path before giving up;
                    # never fall back to a forced kill here.
                    shutdown_stage = self.guest_shutdown(console, qmp, state, stage=shutdown_stage)
                    shutdown_sent_at = current
                    self.log("shutdown_request", stage=shutdown_stage, channel=state, retry=True)
                if stopping and current >= shutdown_deadline:
                    # Explicit stop without -f must NEVER silently become forced.
                    if self.request == "graceful":
                        if getattr(self, "terminal_closed", False):
                            self.request = "force"
                            continue
                        shutdown_deadline = current + self.config["shutdown_timeout_seconds"]
                if state == "boot" and not stopping and current >= boot_deadline:
                    screenshot = str(self.configuration.layout.logs / (record["uuid"] + "-boot.ppm"))
                    try:
                        qmp.command("screendump", arguments={"filename": screenshot})
                        self.log("boot_screen", filename=screenshot)
                    except (WptError, OSError) as exc:
                        self.log("boot_screen_failed", message=str(exc))
                    raise WptError("等待 WinPE 的 SAC 控制台超时；请查看启动日志确认 EMS 是否启用。")
                if state in ("sac-await", "switch-await") and shell_deadline and current >= shell_deadline:
                    # The CMD channel did not confirm; keep the console usable
                    # rather than failing the whole session.
                    self.log("shell_prompt_unconfirmed")
                    self.enter_shell(state="shell", terminal=terminal, path=path, record=record, stopping=stopping)
                    state = "shell"
                selector.modify(serial, selectors.EVENT_READ | (selectors.EVENT_WRITE if console.queued() else 0), "serial")
                for key, mask in selector.select(0.2):
                    if key.data == "terminal":
                        try:
                            data = os.read(0, 16384)
                        except OSError:
                            data = b""
                        if not data:
                            self.terminal_closed = True
                            self.request = self.request or "graceful"
                            selector.unregister(0)
                        elif state == "shell" and not stopping:
                            console.write(data)
                        elif state == "sac" and not stopping:
                            console.write(data)
                    else:
                        if mask & selectors.EVENT_WRITE:
                            console.flush()
                        if mask & selectors.EVENT_READ:
                            data = serial.recv(65536)
                            if not data:
                                if process.poll() is None:
                                    process.wait(timeout=3)
                                return
                            if self.serial_received == 0:
                                self.log("first_serial_data", bytes=len(data), prefix_hex=data[:128].hex())
                            self.serial_received += len(data)
                            tail.feed(data)
                            if state == "boot":
                                if tail.is_sac():
                                    self.log("sac_ready")
                                    self.guest_ready = True
                                    self.registry.update(path, record, state="stopping" if stopping else "running",
                                                         guest={"console": "ems-sac", "channel": "SAC"})
                                    console.command("cmd")
                                    shell_deadline = time.monotonic() + 20
                                    state = "sac-await"
                            elif state in ("sac-await", "switch-await"):
                                try:
                                    write_all(1, data)
                                except OSError:
                                    self.terminal_closed = True
                                    self.request = self.request or "graceful"
                                # `cmd` only creates a channel; switch input and
                                # output to it, otherwise typing still goes to SAC.
                                if state == "switch-await" and not view_key_sent and tail.wants_view_key():
                                    # SAC lists the channel, then attaches only
                                    # after a keystroke. Enter keeps cmd.exe happy.
                                    console.write(b"\r")
                                    view_key_sent = True
                                    self.log("channel_attach")
                                channel = tail.channel()
                                if channel and channel not in switched:
                                    switched.add(channel)
                                    view_key_sent = False
                                    console.command("ch -sn " + channel)
                                    self.log("channel_switch", channel=channel)
                                    shell_deadline = time.monotonic() + 20
                                    state = "switch-await"
                                if tail.is_shell():
                                    self.log("shell_ready", line=tail.last_line().decode("utf-8", "replace"))
                                    self.guest_shell = True
                                    self.enter_shell(state="shell", terminal=terminal, path=path,
                                                     record=record, stopping=stopping)
                                    state = "shell"
                            elif state in ("shell", "sac"):
                                try:
                                    write_all(1, data)
                                except OSError:
                                    self.terminal_closed = True
                                    self.request = self.request or "graceful"
                                if state == "shell" and tail.is_sac():
                                    # cmd.exe exited; SAC is still usable.
                                    self.log("shell_exited")
                                    self.registry.update(path, record, guest={"console": "ems-sac", "channel": "SAC"})
                                    state = "sac"
                                elif state == "sac":
                                    channel = tail.channel()
                                    if channel and channel not in switched:
                                        switched.add(channel)
                                        console.command("ch -sn " + channel)
                                        self.log("channel_switch", channel=channel)
                                        shell_deadline = time.monotonic() + 20
                                        state = "switch-await"
                                    elif tail.is_shell():
                                        self.enter_shell(state="shell", terminal=terminal, path=path,
                                                         record=record, stopping=stopping)
                                        state = "shell"

    def guest_shutdown(self, console, qmp, state, stage=None):
        """Ask WinPE to leave cleanly, escalating only between clean methods."""
        if state == "shell" and stage is None:
            console.command("wpeutil shutdown")
            return "wpeutil"
        if state == "shell" and stage == "wpeutil":
            console.command("wpeutil shutdown")
            return "wpeutil-retry"
        with contextlib.suppress(WptError, OSError):
            qmp.command("system_powerdown")
        return "qmp-powerdown"

    def enter_shell(self, *, state, terminal, path, record, stopping):
        terminal.raw()
        self.registry.update(path, record, state="stopping" if stopping else "running",
                             guest={"console": "ems-sac", "channel": "CMD"})
