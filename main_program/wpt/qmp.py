"""Private, inherited QMP connection to the VM (no listening endpoint)."""
import json
import time

from . import WptError


class QMP:
    def __init__(self, sock):
        self.sock = sock
        self.buffer = bytearray()
        self.serial = 0

    def receive(self, deadline):
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WptError("QMP 响应超时。")
            self.sock.settimeout(remaining)
            data = self.sock.recv(65536)
            if not data:
                raise WptError("QMP 连接已关闭。")
            self.buffer.extend(data)
            if len(self.buffer) > 1024 * 1024:
                raise WptError("QMP 响应过大。")
        raw, _, tail = self.buffer.partition(b"\n")
        self.buffer[:] = tail
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("not an object")
            return value
        except ValueError as exc:
            raise WptError("QMP 返回了无效 JSON。") from exc

    def handshake(self, timeout=10):
        greeting = self.receive(time.monotonic() + timeout)
        if "QMP" not in greeting:
            raise WptError("QEMU 未提供 QMP greeting。")
        self.command("qmp_capabilities", timeout=timeout)

    def command(self, name, *, arguments=None, timeout=3):
        self.serial += 1
        ident = self.serial
        request = {"execute": name, "id": ident}
        if arguments is not None:
            request["arguments"] = arguments
        self.sock.settimeout(timeout)
        self.sock.sendall(json.dumps(request).encode() + b"\n")
        deadline = time.monotonic() + timeout
        while True:
            message = self.receive(deadline)
            if message.get("id") != ident:  # Ignore asynchronous QMP events.
                continue
            if "error" in message:
                raise WptError(f"QMP {name} 失败：{message['error']}")
            return message.get("return")
