"""EMS/SAC console driver.

The guest needs no custom program: Windows boots sacdrv.sys (Start=0, Group=EMS)
from the BCD settings written by the image builder, and exposes a Special
Administration Console on COM1. The CMD channel then hosts cmd.exe.
"""
import re

from . import WptError

# SAC repaints with VT100 sequences, so prompts are matched on the last line.
SAC_LINE = re.compile(rb"^(?:SAC|CMD)>$")
SHELL_LINE = re.compile(rb"^(?:[A-Za-z]:\\[^\r\n>]*|CMD)>$")
# `cmd` only creates a channel; the console must switch to it.
CHANNEL_LINE = re.compile(rb"Channel:\s*([A-Za-z0-9_]+)")
# After ch -sn SAC shows channel info and waits for a key to attach.
VIEW_LINE = re.compile(rb"Use any other key to view this channel")
TAIL_BYTES = 8192


class Tail:
    """Bounded copy of recent console output, used only for prompt detection."""

    def __init__(self, limit=TAIL_BYTES):
        self.data = bytearray()
        self.limit = limit

    def feed(self, chunk):
        self.data.extend(chunk)
        if len(self.data) > self.limit:
            del self.data[: -self.limit]

    def last_line(self):
        text = bytes(self.data).replace(b"\x00", b" ").rstrip()
        if not text:
            return b""
        return text.rsplit(b"\n", 1)[-1].strip()

    def is_sac(self):
        return bool(SAC_LINE.match(self.last_line()))

    def is_shell(self):
        return bool(SHELL_LINE.match(self.last_line()))

    def complete_lines(self):
        """Only text up to the last newline: a chunk can end mid-line.

        SAC repaints with cursor writes, so matching a partial line would
        capture a truncated channel name such as "C" instead of "Cmd0001".
        """
        text = bytes(self.data)
        end = text.rfind(b"\n")
        return text[: end + 1] if end >= 0 else b""

    def channel(self):
        """Newest channel name announced on a complete line, if any."""
        found = CHANNEL_LINE.findall(self.complete_lines())
        return found[-1].decode("ascii") if found else None

    def wants_view_key(self):
        return bool(VIEW_LINE.search(bytes(self.data)))


class SacConsole:
    """Minimal writer for the SAC serial channel.

    Output is passed through unchanged; SAC and cmd.exe do their own echo, so
    the host terminal stays in raw mode once the shell channel is active.
    """

    def __init__(self, sock):
        self.sock = sock
        self.pending = bytearray()

    def write(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.pending.extend(data)

    def command(self, text):
        """Submit one SAC or shell command; Enter is CR, as the console expects."""
        self.write(text.encode("utf-8") + b"\r")

    def queued(self):
        return bool(self.pending)

    def flush(self):
        if not self.pending:
            return
        try:
            sent = self.sock.send(self.pending)
        except BlockingIOError:
            return
        del self.pending[:sent]
        if len(self.pending) > 1024 * 1024:
            raise WptError("SAC 通道未及时读取主机输入。")
