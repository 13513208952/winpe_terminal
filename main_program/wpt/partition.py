"""Expose ONE partition as an MBR disk over private, bounded NBD.

The original start LBA is preserved. Prefix sectors are synthetic; they never
map to the parent disk. No partition-table write can change the host mapping.
"""
import errno
import os
import socket
import struct
import threading
import uuid

from . import WptError

NBD_MAGIC = 0x4E42444D41474943
OPTIONS_MAGIC = 0x49484156454F5054
REPLY_MAGIC = 0x3E889045565A9
REQUEST_MAGIC = 0x25609513
SIMPLE_REPLY = 0x67446698
MAX_REQUEST = 8 * 1024 * 1024


def exact(sock, size):
    data = bytearray()
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise EOFError("NBD disconnected")
        data.extend(part)
    return bytes(data)


class PartitionDisk:
    def __init__(self, fd, size, start_sector, ident):
        sectors = size // 512
        if size <= 0 or size % 512 or start_sector < 1 or start_sector + sectors > 0xFFFFFFFF:
            raise WptError("仅支持 512 字节逻辑扇区、MBR 可表达范围内的分区。")
        self.fd, self.offset, self.length = fd, start_sector * 512, size
        self.size = self.offset + size
        self.mbr = bytearray(512)
        signature = uuid.UUID(ident).int & 0xFFFFFFFF or 1
        struct.pack_into("<I", self.mbr, 440, signature)
        self.mbr[446:462] = struct.pack("<B3sB3sII", 0, b"\xfe\xff\xff", 7, b"\xfe\xff\xff", start_sector, sectors)
        self.mbr[510:512] = b"\x55\xaa"

    def bounds(self, offset, size):
        if offset < 0 or size < 0 or offset % 512 or size % 512 or size > MAX_REQUEST or offset + size > self.size:
            raise OSError(errno.EINVAL, "invalid disk range")

    def read(self, offset, size):
        self.bounds(offset, size)
        result = bytearray(size)
        if offset < 512:
            n = min(size, 512 - offset)
            result[:n] = self.mbr[offset:offset + n]
        start, end = max(offset, self.offset), min(offset + size, self.size)
        if start < end:
            data = os.pread(self.fd, end - start, start - self.offset)
            if len(data) != end - start:
                raise OSError(errno.EIO, "short partition read")
            result[start - offset:end - offset] = data
        return bytes(result)

    def write(self, offset, data):
        self.bounds(offset, len(data))
        if offset == 0 and len(data) == 512:
            # Windows may update the synthetic disk signature, never its mapping.
            if data[446:512] != self.mbr[446:512]:
                raise OSError(errno.EPERM, "partition-table changes are forbidden")
            self.mbr[:446] = data[:446]
            return
        if offset < self.offset:
            raise OSError(errno.EPERM, "writes outside the selected partition are forbidden")
        view = memoryview(data)
        position = offset - self.offset
        while view:
            written = os.pwrite(self.fd, view, position)
            if written <= 0:
                raise OSError(errno.EIO, "short partition write")
            position += written
            view = view[written:]

    def flush(self):
        os.fsync(self.fd)


def option_reply(sock, option, kind, payload=b""):
    sock.sendall(struct.pack(">QIII", REPLY_MAGIC, option, kind, len(payload)) + payload)


def negotiate(sock, disk):
    sock.sendall(struct.pack(">QQH", NBD_MAGIC, OPTIONS_MAGIC, 3))
    flags, = struct.unpack(">I", exact(sock, 4))
    if not flags & 1 or flags & ~3:
        raise WptError("NBD 客户端标志无效。")
    for _ in range(32):
        magic, option, size = struct.unpack(">QII", exact(sock, 16))
        if magic != OPTIONS_MAGIC or size > 65536:
            raise WptError("无效 NBD 协商。")
        payload = exact(sock, size)
        if option == 2:  # ABORT
            option_reply(sock, option, 1)
            raise EOFError("NBD aborted")
        if option == 1:  # EXPORT_NAME
            if payload not in (b"", b"winpe"):
                raise WptError("未知 NBD export。")
            sock.sendall(struct.pack(">QH", disk.size, 13) + (b"" if flags & 2 else bytes(124)))
            return
        if option in (6, 7):  # INFO / GO
            if len(payload) < 6:
                raise WptError("无效 NBD GO。")
            n, = struct.unpack_from(">I", payload)
            if n > len(payload) - 6 or payload[4:4 + n] not in (b"", b"winpe"):
                raise WptError("无效 NBD export 名称。")
            count, = struct.unpack_from(">H", payload, 4 + n)
            if len(payload) != 6 + n + 2 * count:
                raise WptError("无效 NBD info 列表。")
            option_reply(sock, option, 3, struct.pack(">HQH", 0, disk.size, 13))
            option_reply(sock, option, 3, struct.pack(">HIII", 3, 512, 4096, MAX_REQUEST))
            option_reply(sock, option, 1)
            if option == 7:
                return
        else:
            option_reply(sock, option, 0x80000001)  # Unsupported; no structured replies.
    raise WptError("NBD 协商次数超限。")


def serve(sock, disk):
    negotiate(sock, disk)
    sock.settimeout(None)
    while True:
        magic, flags, kind, handle, offset, size = struct.unpack(">IHHQQI", exact(sock, 28))
        if magic != REQUEST_MAGIC or size > MAX_REQUEST:
            raise WptError("无效 NBD 请求。")
        if kind == 2:  # DISCONNECT
            return
        payload = exact(sock, size) if kind == 1 else b""
        error, result = 0, b""
        try:
            if flags & ~1 or (flags and kind != 1):
                raise OSError(errno.EINVAL, "unsupported request flags")
            if kind == 0:
                result = disk.read(offset, size)
            elif kind == 1:
                disk.write(offset, payload)
                if flags & 1:
                    disk.flush()
            elif kind == 3 and not size:
                disk.flush()
            else:
                raise OSError(errno.EINVAL, "unsupported NBD command")
        except OSError as exc:
            error = exc.errno or errno.EIO
        sock.sendall(struct.pack(">IIQ", SIMPLE_REPLY, error, handle) + (result if not error else b""))


class NBDService:
    """A socketpair inherited only by QEMU; no listening endpoint or filesystem."""
    def __init__(self, disk):
        self.disk = disk
        self.connection, self.client = socket.socketpair()
        self.closed = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        # Called after Popen: avoid forking a root process with active threads.
        self.client.close()
        self.thread.start()

    def run(self):
        try:
            self.connection.settimeout(15)
            serve(self.connection, self.disk)
        except (OSError, EOFError, WptError) as exc:
            if not self.closed.is_set():
                self.error = str(exc)
        finally:
            self.connection.close()

    def close(self):
        self.closed.set()
        self.client.close()
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        if self.thread.is_alive():
            self.thread.join(timeout=2)
        self.connection.close()
