"""No VM and no real block device is opened by this suite."""
import copy
import io
import json
import os
import pty
import socket
import struct
import sys
import tempfile
import termios
import threading
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpt import WptError
from wpt.cli import STOP_COMMANDS, main, parse, settings, uninstall
from wpt.config import Configuration, DEFAULTS, validate
from wpt.deployment import stage
from wpt.lifecycle import OperationLock, await_stop, request_stop
from wpt.paths import Layout
from wpt.partition import (MAX_REQUEST, NBD_MAGIC, OPTIONS_MAGIC, REQUEST_MAGIC,
                           SIMPLE_REPLY, NBDService, PartitionDisk, exact, serve)
from wpt.registry import Registry, matches, notify, process_info
from wpt.safety import check_selection
from wpt.storage import atomic_json, read_json, safe_directory
from wpt.vm import Supervisor, Terminal, child_guard, qemu_arguments, require_root
from wpt.qmp import QMP

TEST_ROOT = Path(os.environ["WPT_TEST_ROOT"])  # explicit scratch dir; no machine-specific default
SOURCE = Path(__file__).resolve().parents[1]
IDENT = "a405b8f2-9ef1-47c8-9824-b0847065ce14"


class Scratch(unittest.TestCase):
    def setUp(self):
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="host-", dir=TEST_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.layout = Layout(self.directory)
        self.layout.config_dir.mkdir(parents=True)
        self.layout.ensure_tree()


class GrammarTests(unittest.TestCase):
    def test_simple_commands(self):
        for cmd in ("-help", "setting", "reload", "-status", "-unistall"):
            with self.subTest(cmd=cmd):
                self.assertEqual(parse([cmd]).name, cmd)
        self.assertEqual(parse([]).name, "-help")

    def test_start(self):
        self.assertIsNone(parse(["start"]).device)
        self.assertEqual(parse(["start", "-/dev/sda1"]).device, "/dev/sda1")

    def test_start_rejects_ambiguity(self):
        for args in (["start", "/dev/sda1"], ["start", "-/dev/../sda1"],
                     ["start", "-/dev/sda1", "-/dev/sdb1"], ["start", "--snapshot"],
                     ["start", "-/dev/a\n"], ["-help", "start"], ["-uninstall"]):
            with self.subTest(args=args), self.assertRaises(WptError):
                parse(args)

    def test_stop_aliases_and_order(self):
        for name in STOP_COMMANDS:
            for flags in ([], ["-f"], [f"-x={IDENT}"], ["-f", f"-x={IDENT}"], [f"-x={IDENT}", "-f"]):
                with self.subTest(name=name, flags=flags):
                    command = parse([name, *flags])
                    self.assertEqual(command.force, "-f" in flags)
                    self.assertEqual(command.target, IDENT if any(f.startswith("-x=") for f in flags) else None)

    def test_invalid_stop(self):
        for tail in (["-x"], ["-x", IDENT], ["-f", "-f"], ["-x=1"],
                     [f"-x={IDENT.upper()}"], ["-f", "-x"], ["-x" + IDENT],
                     [f"-x={IDENT}", f"-x={IDENT}"]):
            with self.subTest(tail=tail), self.assertRaises(WptError):
                parse(["stop", *tail])

    def test_root_gating_before_installation(self):
        for cmd in ("start", "-status", "stop", "-unistall"):
            output = io.StringIO()
            with patch("os.geteuid", return_value=1000), redirect_stderr(output):
                self.assertEqual(main(Layout("/does-not-exist"), [cmd]), 1)
            self.assertIn("root", output.getvalue())

    def test_help_without_root_or_installation(self):
        with patch("os.geteuid", return_value=1000), redirect_stdout(io.StringIO()):
            self.assertEqual(main(Layout("/does-not-exist"), ["-help"]), 0)


class ConfigTests(Scratch):
    def test_default_is_data_only(self):
        self.assertEqual(validate(copy.deepcopy(DEFAULTS))["allowed_devices"], [])
        bad = copy.deepcopy(DEFAULTS)
        bad["extra_args"] = ["-runas", "root"]
        with self.assertRaises(WptError):
            validate(bad)

    def test_strict_types_and_bounds(self):
        for key, value in (("schema", 2), ("memory_mib", True), ("cpus", 0),
                           ("qemu", "relative/qemu"), ("iso", "a\0b"), ("allowed_devices", {})):
            bad = copy.deepcopy(DEFAULTS)
            bad[key] = value
            with self.subTest(key=key), self.assertRaises(WptError):
                validate(bad)

    def test_whitelist_identity(self):
        data = copy.deepcopy(DEFAULTS)
        item = {"path": "/dev/sda1", "serial": "SERIAL", "size_bytes": 8192}
        data["allowed_devices"] = [item]
        validate(data)
        for changes in ({"serial": ""}, {"size_bytes": True}, {"path": "/dev/../x"}, {"x": 1}):
            bad = copy.deepcopy(data)
            bad["allowed_devices"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(WptError):
                validate(bad)
        data["allowed_devices"].append(item.copy())
        with self.assertRaises(WptError):
            validate(data)

    def test_read_and_save(self):
        config = Configuration(self.layout)
        config.save(copy.deepcopy(DEFAULTS))
        self.assertEqual(config.load(), DEFAULTS)
        data = config.load()
        data["cpus"] = 3
        config.save(data)
        self.assertEqual(config.load()["cpus"], 3)

    def test_setting_noninteractive_reports_path(self):
        config = Configuration(self.layout)
        config.save(copy.deepcopy(DEFAULTS))
        output = io.StringIO()
        with redirect_stdout(output), patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(settings(config), 0)
        self.assertIn(str(config.path), output.getvalue())

    def test_settings_rejects_invalid_and_can_cancel(self):
        config = Configuration(self.layout)
        config.save(copy.deepcopy(DEFAULTS))
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True), \
             patch.dict(os.environ, {"TERM": "xterm"}), patch("builtins.input", side_effect=["4", "0", "s", "q"]), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(settings(config), 0)
        self.assertEqual(config.load(), DEFAULTS)


class StorageTests(Scratch):
    def test_metadata_and_atomic_update(self):
        path = self.directory / "record.txt"
        stamp = "2026-01-01T00:00:00+00:00"
        atomic_json(path, {"a": 1}, created_at=stamp)
        self.assertEqual(os.getxattr(path, "user.winpe_terminal.created_at"), stamp.encode())
        atomic_json(path, {"a": 2}, created_at=stamp)
        self.assertEqual(read_json(path), {"a": 2})
        self.assertEqual(os.getxattr(path, "user.winpe_terminal.created_at"), stamp.encode())
        self.assertFalse(list(self.directory.glob(".write-*")))

    def test_metadata_failure_is_not_silenced(self):
        path = self.directory / "record.txt"
        with patch("os.setxattr", side_effect=OSError("unsupported")), self.assertRaises(WptError):
            atomic_json(path, {}, created_at="stamp")
        self.assertFalse(path.exists())
        self.assertFalse(list(self.directory.glob(".write-*")))

    def test_symlink_file_is_never_read_or_replaced(self):
        target = self.directory / "target"
        atomic_json(target, {"safe": 1})
        link = self.directory / "link"
        link.symlink_to(target)
        with self.assertRaises(OSError):
            read_json(link)
        with self.assertRaises(WptError):
            atomic_json(link, {"bad": 1})
        self.assertEqual(read_json(target), {"safe": 1})

    def test_symlink_ancestor_rejected(self):
        (self.directory / "real").mkdir()
        (self.directory / "link").symlink_to(self.directory / "real", target_is_directory=True)
        with self.assertRaises(WptError):
            safe_directory(self.directory / "link")

    def test_invalid_json(self):
        path = self.directory / "bad"
        path.write_text("{")
        with self.assertRaises(WptError):
            read_json(path)


class RegistryTests(Scratch):
    def setUp(self):
        super().setUp()
        (self.directory / "instances").mkdir()
        self.registry = Registry(self.layout, IDENT)

    def test_sequence_uuid_and_history(self):
        first, a = self.registry.create({"uid": 1000, "name": "user"}, None, DEFAULTS)
        second, b = self.registry.create({"uid": 1000, "name": "user"}, None, DEFAULTS)
        self.assertEqual((a["sequence"], b["sequence"]), (1, 2))
        self.assertNotEqual(a["uuid"], b["uuid"])
        self.assertTrue(first.name.endswith(a["uuid"] + ".txt"))
        self.registry.update(first, a, state="stopped")
        self.assertEqual(len(self.registry.records()), 2)
        self.assertEqual(os.getxattr(first, "user.winpe_terminal.created_at").decode(), a["created_at"])
        self.assertEqual(self.registry.active(), [])  # This unittest process is not a supervisor.

    def test_invalid_record_rejected(self):
        path, data = self.registry.create({}, None, DEFAULTS)
        data["uuid"] = str(uuid.uuid4())
        atomic_json(path, data)
        with self.assertRaises(WptError):
            self.registry.records()

    def test_process_identity_not_pid_alone(self):
        _, record = self.registry.create({}, None, DEFAULTS)
        record["boot_id"] = "different-boot"
        self.assertIsNone(matches(record, "supervisor"))
        self.assertIsNone(process_info(-1))
        self.assertIsNotNone(process_info(os.getpid()))

    def test_pidfd_opened_before_identity_check(self):
        record = {"supervisor_pid": 123}
        calls = []
        with patch("os.pidfd_open", side_effect=lambda pid: calls.append("open") or 55), \
             patch("wpt.registry.matches", side_effect=lambda *_: calls.append("check") or {}), \
             patch("os.close", side_effect=lambda fd: calls.append("close")), \
             patch("signal.pidfd_send_signal") as send:
            self.assertFalse(notify(record))
            send.assert_not_called()
        self.assertEqual(calls, ["open", "check", "close"])

    def test_pidfd_notify_modes(self):
        import signal
        for force in (False, True):
            with patch("os.pidfd_open", return_value=55), patch("wpt.registry.matches", return_value={"pid": 123}), \
                 patch("os.close"), patch("signal.pidfd_send_signal") as send:
                self.assertTrue(notify({"supervisor_pid": 123}, force=force))
                send.assert_called_once_with(55, signal.SIGUSR2 if force else signal.SIGUSR1)

    def test_target_not_active_is_error(self):
        with self.assertRaises(WptError):
            request_stop(self.registry, target=IDENT)
        self.assertEqual(request_stop(self.registry), set())
        self.assertEqual(await_stop(self.registry, set(), 0), [])


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.nodes = {
            "/dev/nvme0n1": {"type": "disk", "maj:min": "259:0", "parents": set(), "serial": "SYSTEM"},
            "/dev/nvme0n1p5": {"type": "part", "maj:min": "259:5", "parents": {"/dev/nvme0n1"}},
            "/dev/nvme0n1p3": {"type": "part", "maj:min": "259:3", "parents": {"/dev/nvme0n1"}},
            "/dev/sda": {"type": "disk", "maj:min": "8:0", "parents": set(), "serial": "EXTERNAL"},
            "/dev/sda1": {"type": "part", "maj:min": "8:1", "parents": {"/dev/sda"}, "size": 8192, "ro": False, "mountpoints": []},
        }
        self.allowed = [{"path": "/dev/sda1", "serial": "EXTERNAL", "size_bytes": 8192}]

    def check(self, **kwargs):
        args = dict(path="/dev/sda1", allowed=self.allowed, nodes=self.nodes,
                    protected_devnums={"259:5"}, mounted_devnums=set(), swapped_paths=set(), holders=[])
        args.update(kwargs)
        return check_selection(**args)

    def test_explicit_safe_partition(self):
        self.assertEqual(self.check()["maj:min"], "8:1")

    def test_default_disabled(self):
        with self.assertRaises(WptError):
            self.check(allowed=[])

    def test_mounted_swap_holder_readonly_size_serial(self):
        for kwargs in ({"mounted_devnums": {"8:1"}}, {"swapped_paths": {"/dev/sda1"}}, {"holders": ["dm-0"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(WptError):
                self.check(**kwargs)
        for key, value in (("ro", True), ("size", 9999), ("mountpoints", ["/mounted"])):
            with self.subTest(key=key), patch.dict(self.nodes["/dev/sda1"], {key: value}), self.assertRaises(WptError):
                self.check()
        self.nodes["/dev/sda"]["serial"] = "WRONG"
        with self.assertRaises(WptError):
            self.check()

    def test_same_disk_partition_allowed(self):
        # Dual-boot: Windows sits on the same SSD as the host, and only the
        # host's own partition must stay off limits.
        self.nodes["/dev/nvme0n1p3"].update({"size": 8192, "ro": False, "mountpoints": []})
        allowed = [{"path": "/dev/nvme0n1p3", "serial": "SYSTEM", "size_bytes": 8192}]
        self.assertEqual(self.check(path="/dev/nvme0n1p3", allowed=allowed)["maj:min"], "259:3")

    def test_host_root_partition_is_protected(self):
        self.nodes["/dev/nvme0n1p5"].update({"size": 8192, "ro": False, "mountpoints": []})
        allowed = [{"path": "/dev/nvme0n1p5", "serial": "SYSTEM", "size_bytes": 8192}]
        with self.assertRaisesRegex(WptError, "宿主根"):
            self.check(path="/dev/nvme0n1p5", allowed=allowed)

    def test_installation_disk_is_protected(self):
        with self.assertRaisesRegex(WptError, "安装目录"):
            self.check(protected_devnums={"8:1"})

    def test_whole_disk_rejected(self):
        with self.assertRaisesRegex(WptError, "整盘"):
            self.check(path="/dev/sda", allowed=[{"path": "/dev/sda", "serial": "EXTERNAL", "size_bytes": 8192}])

    def test_unknown_parent_fails_closed(self):
        self.nodes["/dev/sda1"]["parents"] = {"/dev/missing"}
        with self.assertRaises(WptError):
            self.check()


class WireTests(unittest.TestCase):
    def test_qmp_greeting_event_response(self):
        host, guest = socket.socketpair()
        errors = []
        def server():
            try:
                guest.sendall(b'{"QMP":{"version":{}}}\r\n')
                stream = guest.makefile("rb")
                for _ in range(2):
                    request = json.loads(stream.readline())
                    guest.sendall(b'{"event":"POWERDOWN"}\n')
                    guest.sendall(json.dumps({"return": {}, "id": request["id"]}).encode() + b"\n")
            except BaseException as exc:
                errors.append(exc)
            finally:
                guest.close()
        worker = threading.Thread(target=server)
        worker.start()
        with host:
            qmp = QMP(host)
            qmp.handshake()
            self.assertEqual(qmp.command("system_powerdown"), {})
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_qmp_error_is_not_success(self):
        host, guest = socket.socketpair()
        with host, guest:
            guest.sendall(b'{"id":1,"error":{"class":"GenericError"}}\n')
            with self.assertRaises(WptError):
                QMP(host).command("bad")


class PartitionTests(Scratch):
    def setUp(self):
        super().setUp()
        self.fixture = self.directory / "partition-fixture.bin"
        self.fixture.write_bytes(b"A" * 8192)
        self.fd = os.open(self.fixture, os.O_RDWR)
        self.addCleanup(os.close, self.fd)
        self.disk = PartitionDisk(self.fd, 8192, 2048, IDENT)

    def test_mbr_and_start_lba(self):
        boot = self.disk.read(0, 512)
        self.assertEqual(boot[510:512], b"\x55\xaa")
        self.assertEqual(boot[450], 7)
        self.assertEqual(struct.unpack_from("<II", boot, 454), (2048, 16))
        self.assertEqual(self.disk.size, 1048576 + 8192)
        self.assertEqual(self.disk.read(512, 512), bytes(512))

    def test_prefix_read_and_partition_write(self):
        self.assertEqual(self.disk.read(self.disk.offset - 512, 1024), bytes(512) + b"A" * 512)
        self.disk.write(self.disk.offset + 512, b"B" * 512)
        self.disk.flush()
        self.assertEqual(self.fixture.read_bytes(), b"A" * 512 + b"B" * 512 + b"A" * 7168)

    def test_cannot_write_host_prefix_or_change_mapping(self):
        for offset, data in ((512, bytes(512)), (self.disk.offset - 512, bytes(1024)), (0, bytes(512))):
            with self.subTest(offset=offset), self.assertRaises(OSError):
                self.disk.write(offset, data)
        self.assertEqual(self.fixture.read_bytes(), b"A" * 8192)

    def test_synthetic_disk_signature_is_memory_only(self):
        boot = bytearray(self.disk.read(0, 512))
        boot[440:444] = b"\x01\x02\x03\x04"
        self.disk.write(0, boot)
        self.assertEqual(self.disk.read(0, 512)[440:444], boot[440:444])
        self.assertEqual(self.fixture.read_bytes(), b"A" * 8192)

    def test_invalid_ranges_and_mbr_capacity(self):
        for offset, size in ((-512, 512), (1, 512), (0, 1), (self.disk.size, 512), (0, MAX_REQUEST + 512)):
            with self.subTest(offset=offset, size=size), self.assertRaises(OSError):
                self.disk.read(offset, size)
        for size, start in ((8193, 2048), (8192, 0), (8192, 0xFFFFFFFF)):
            with self.assertRaises(WptError):
                PartitionDisk(self.fd, size, start, IDENT)

    def test_nbd_export_and_rw_flush(self):
        host, server = socket.socketpair()
        errors = []
        def run():
            try:
                serve(server, self.disk)
            except BaseException as exc:
                errors.append(exc)
            finally:
                server.close()
        worker = threading.Thread(target=run)
        worker.start()
        with host:
            self.assertEqual(struct.unpack(">QQH", exact(host, 18)), (NBD_MAGIC, OPTIONS_MAGIC, 3))
            host.sendall(struct.pack(">I", 3) + struct.pack(">QII", OPTIONS_MAGIC, 1, 0))
            self.assertEqual(struct.unpack(">QH", exact(host, 10)), (self.disk.size, 13))
            for kind, offset, size, data, error in (
                (0, 0, 512, b"", 0),
                (1, self.disk.offset, 512, b"B" * 512, 0),
                (1, 512, 512, bytes(512), 1),
                (3, 0, 0, b"", 0),
            ):
                host.sendall(struct.pack(">IHHQQI", REQUEST_MAGIC, 0, kind, 7, offset, size) + data)
                magic, err, handle = struct.unpack(">IIQ", exact(host, 16))
                self.assertEqual((magic, handle), (SIMPLE_REPLY, 7))
                self.assertEqual(err, error)
                if kind == 0 and not err:
                    self.assertEqual(exact(host, size), self.disk.read(offset, size))
            host.sendall(struct.pack(">IHHQQI", REQUEST_MAGIC, 0, 2, 0, 0, 0))
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_nbd_go_and_unsupported_structured_reply(self):
        host, server = socket.socketpair()
        def run():
            try:
                serve(server, self.disk)
            finally:
                server.close()
        worker = threading.Thread(target=run)
        worker.start()
        with host:
            exact(host, 18)
            host.sendall(struct.pack(">I", 3))
            host.sendall(struct.pack(">QII", OPTIONS_MAGIC, 8, 0))
            reply = struct.unpack(">QIII", exact(host, 20))
            self.assertEqual(reply[2], 0x80000001)
            data = struct.pack(">IH", 0, 0)
            host.sendall(struct.pack(">QII", OPTIONS_MAGIC, 7, len(data)) + data)
            kinds = []
            while True:
                _, option, kind, size = struct.unpack(">QIII", exact(host, 20))
                self.assertEqual(option, 7)
                exact(host, size)
                kinds.append(kind)
                if kind == 1:
                    break
            self.assertEqual(kinds, [3, 3, 1])
            host.sendall(struct.pack(">IHHQQI", REQUEST_MAGIC, 0, 2, 0, 0, 0))
        worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_nbd_service_uses_no_filesystem_socket(self):
        service = NBDService(self.disk)
        try:
            self.assertEqual(service.connection.getsockname(), "")
            self.assertGreaterEqual(service.client.fileno(), 0)
        finally:
            service.close()


class VmSupportTests(unittest.TestCase):
    def test_qemu_minimal_and_bounded_arguments(self):
        args = qemu_arguments("/usr/bin/qemu", DEFAULTS, IDENT, 7, 8, 9, 10)
        self.assertIn("pc,accel=kvm", args)
        self.assertIn("none", args)
        self.assertNotIn("-net", args)
        self.assertIn("ide-hd,drive=target,bus=ide.0", args)
        self.assertTrue(any("server.type=fd,server.str=10" in arg for arg in args))
        self.assertTrue(any("fd=7,set=1" in arg for arg in args))
        self.assertFalse(any("/dev/sd" in arg for arg in args))

    def test_terminal_is_restored(self):
        master, slave = pty.openpty()
        try:
            before = termios.tcgetattr(slave)
            terminal = Terminal(slave)
            terminal.raw()
            self.assertNotEqual(termios.tcgetattr(slave), before)
            terminal.restore()
            self.assertEqual(termios.tcgetattr(slave), before)
        finally:
            os.close(master)
            os.close(slave)

    def test_hup_marks_terminal_closed_not_other_stop_requests(self):
        import signal
        supervisor = Supervisor(None, DEFAULTS, None)
        supervisor.handle_signal(signal.SIGHUP, None)
        self.assertTrue(supervisor.terminal_closed)
        self.assertEqual(supervisor.request, "graceful")
        supervisor.handle_signal(signal.SIGUSR2, None)
        supervisor.handle_signal(signal.SIGUSR1, None)
        self.assertEqual(supervisor.request, "force")


class StagingTests(Scratch):
    """The development staging helper mirrors the installed layout."""

    def stage(self):
        return stage(SOURCE, self.directory / "prefix")

    def test_stage_layout_matches_package(self):
        layout = self.stage()
        self.assertEqual(layout.at("usr/bin/winpe_terminal").stat().st_mode & 0o777, 0o755)
        self.assertTrue((layout.program_dir / "wpt/cli.py").is_file())
        self.assertTrue((layout.program_dir / "tools/build_winpe.py").is_file())
        self.assertTrue((layout.program_dir / "guest/startnet.cmd").is_file())
        self.assertEqual(Configuration(layout).load(), DEFAULTS)
        self.assertEqual(len(Configuration(layout).installation()["id"]), 36)

    def test_existing_prefix_not_overwritten(self):
        layout = self.stage()
        with self.assertRaises(WptError):
            stage(SOURCE, layout.prefix)

    def test_relative_iso_resolves_against_state_dir(self):
        layout = self.stage()
        config = Configuration(layout)
        self.assertEqual(config.resolve(config.load()["iso"]), layout.iso)

    def test_missing_marker_points_at_prepare(self):
        layout = self.stage()
        layout.marker.unlink()
        with self.assertRaisesRegex(WptError, "prepare"):
            Configuration(layout).installation()


class UninstallTests(Scratch):
    """-unistall must stop instances first and then defer to APT."""

    def registry(self):
        return Registry(self.layout, IDENT)

    def test_cancel_does_not_stop_or_uninstall(self):
        with patch("wpt.cli.confirm", return_value=False), patch("wpt.cli.stop_instances") as stop, \
                patch("wpt.cli.os.spawnv") as spawn, redirect_stdout(io.StringIO()):
            self.assertEqual(uninstall(self.layout, self.registry(), DEFAULTS), 0)
        stop.assert_not_called()
        spawn.assert_not_called()

    def test_declined_force_preserves_everything(self):
        with patch("wpt.cli.confirm", return_value=True), patch("wpt.cli.stop_instances", return_value=False), \
                patch("wpt.cli.os.spawnv") as spawn, redirect_stdout(io.StringIO()):
            self.assertEqual(uninstall(self.layout, self.registry(), DEFAULTS), 0)
        spawn.assert_not_called()

    def test_hand_over_to_apt_purge(self):
        with patch("wpt.cli.confirm", return_value=True), patch("wpt.cli.stop_instances", return_value=True), \
                patch("wpt.cli.dpkg_installed", return_value=True), \
                patch("wpt.cli.os.spawnv", return_value=0) as spawn, redirect_stdout(io.StringIO()):
            self.assertEqual(uninstall(self.layout, self.registry(), DEFAULTS), 0)
        self.assertEqual(spawn.call_args[0][2][:2], ["apt-get", "purge"])

    def test_failed_shutdown_never_calls_apt(self):
        with patch("wpt.cli.confirm", return_value=True), \
                patch("wpt.cli.stop_instances", side_effect=WptError("实例未完全退出")), \
                patch("wpt.cli.os.spawnv") as spawn, redirect_stdout(io.StringIO()), self.assertRaises(WptError):
            uninstall(self.layout, self.registry(), DEFAULTS)
        spawn.assert_not_called()

    def test_without_dpkg_record_refuses(self):
        with patch("wpt.cli.confirm", return_value=True), patch("wpt.cli.stop_instances", return_value=True), \
                patch("wpt.cli.dpkg_installed", return_value=False), \
                patch("wpt.cli.os.spawnv") as spawn, redirect_stdout(io.StringIO()), self.assertRaises(WptError):
            uninstall(self.layout, self.registry(), DEFAULTS)
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
