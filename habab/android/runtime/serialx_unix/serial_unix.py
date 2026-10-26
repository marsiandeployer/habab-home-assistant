"""Unix-domain stream transport for the pinned Android serialx runtime.

This file is copied to ``serialx/platforms/serial_unix.py`` and imported from
``serialx/platforms/__init__.py`` by the runtime overlay. It deliberately does
not modify serialx's existing URI handlers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
import errno
import socket
from typing import Any
import urllib.parse

if hasattr(asyncio, "timeout"):
    asyncio_timeout = asyncio.timeout
else:  # pragma: no cover - compatibility for serialx's declared Python >= 3.10
    from async_timeout import timeout as asyncio_timeout

from typing_extensions import Buffer

from serialx.common import (
    BaseSerial,
    BaseSerialTransport,
    ModemPins,
    Parity,
    PortSettingsUpdate,
    StopBits,
    register_uri_handler,
)

DEFAULT_CONNECT_TIMEOUT = 10.0


def _socket_path(uri: str) -> str:
    """Return an absolute filesystem socket path from a unix:// URI."""
    parsed = urllib.parse.urlsplit(uri)
    path = urllib.parse.unquote(parsed.path)
    if (
        parsed.scheme != "unix"
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not path.startswith("/")
        or "\x00" in path
    ):
        raise ValueError("Expected unix:///absolute/filesystem/socket/path")
    # Linux sockaddr_un.sun_path is 108 bytes including the terminating NUL.
    if len(path.encode()) >= 108:
        raise ValueError("Unix-domain socket path must be shorter than 108 bytes")
    return path


class UnixSerial(BaseSerial):
    """Synchronous serial interface over an AF_UNIX stream socket."""

    def __init__(
        self,
        *args: Any,
        connect_timeout: float | None = DEFAULT_CONNECT_TIMEOUT,
        **kwargs: Any,
    ) -> None:
        path = kwargs.get("path")
        if path is None and args:
            path = args[0]
        if path is None:
            raise ValueError("A unix:// filesystem socket path is required")
        self._socket_path = _socket_path(str(path))
        super().__init__(*args, **kwargs)
        self._connect_timeout = connect_timeout
        self._socket: socket.socket | None = None

    @property
    def is_open(self) -> bool:
        return self._socket is not None

    @property
    def connect_timeout(self) -> float | None:
        return self._connect_timeout

    @contextmanager
    def _socket_timeout(self, timeout: float | None) -> Generator[None, None, None]:
        assert self._socket is not None
        original_timeout = self._socket.gettimeout()
        self._socket.settimeout(timeout)
        try:
            yield
        finally:
            if self._socket is not None:
                self._socket.settimeout(original_timeout)

    def _effective_socket_timeout(self) -> float | None:
        if self._read_timeout is None:
            return self._write_timeout
        if self._write_timeout is None:
            return self._read_timeout
        return min(self._read_timeout, self._write_timeout)

    def _open(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(self._connect_timeout)
            connection.connect(self._socket_path)
            connection.settimeout(self._effective_socket_timeout())
        except BaseException:
            connection.close()
            raise
        self._socket = connection

    def _reconfigure_port(self, update: PortSettingsUpdate) -> None:
        if self._socket is not None:
            self._socket.settimeout(self._effective_socket_timeout())

    def _set_modem_pins(self, modem_pins: ModemPins) -> None:
        # A byte stream has no DTR/RTS control channel.
        pass

    def _get_modem_pins(self) -> ModemPins:
        return ModemPins()

    def num_unread_bytes(self) -> int:
        return 0

    def num_unwritten_bytes(self) -> int:
        return 0

    def _reset_read_buffer(self) -> None:
        with self._socket_timeout(0):
            while self._socket is not None:
                try:
                    if not self._socket.recv(1024):
                        break
                except BlockingIOError:
                    break

    def _reset_write_buffer(self) -> None:
        pass

    def _flush(self) -> None:
        pass

    def _write(self, data: Buffer, *, timeout: float | None) -> int:
        assert self._socket is not None
        payload = bytes(data)
        with self._socket_timeout(timeout):
            self._socket.sendall(payload)
        return len(payload)

    def _readinto(self, data: Buffer, *, timeout: float | None) -> int:
        assert self._socket is not None
        target = memoryview(data).cast("B")
        try:
            with self._socket_timeout(timeout):
                count = self._socket.recv_into(target)
        except (TimeoutError, BlockingIOError):
            return 0
        if count == 0:
            self._mark_broken(OSError(errno.EIO, "Unix socket closed by peer"))
            self._check_broken()
        return count

    def _close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None


class _UnixProxyProtocol(asyncio.Protocol):
    """Forward stream callbacks to UnixSerialTransport."""

    def __init__(self, serial_transport: UnixSerialTransport) -> None:
        self._serial_transport = serial_transport

    def data_received(self, data: bytes) -> None:
        self._serial_transport._data_received(data)

    def pause_writing(self) -> None:
        self._serial_transport._pause_writing()

    def resume_writing(self) -> None:
        self._serial_transport._resume_writing()

    def connection_lost(self, exc: Exception | None) -> None:
        self._serial_transport._stream_connection_lost()
        self._serial_transport._connection_lost(exc)


class UnixSerialTransport(BaseSerialTransport):
    """Async serial transport over an AF_UNIX stream socket."""

    transport_name = "unix"

    def __init__(
        self, loop: asyncio.AbstractEventLoop, protocol: asyncio.Protocol
    ) -> None:
        super().__init__(loop, protocol)
        self._stream_transport: asyncio.Transport | None = None
        self._stream_connection_lost_waiter: asyncio.Future[None] | None = None

    async def _connect(  # type: ignore[override]
        self,
        *,
        path: str,
        baudrate: int,
        parity: Parity = Parity.NONE,
        stopbits: StopBits | int | float = StopBits.ONE,
        xonxoff: bool = False,
        rtscts: bool = False,
        byte_size: int = 8,
        connect_timeout: float | None = DEFAULT_CONNECT_TIMEOUT,
        **kwargs: Any,
    ) -> None:
        self._serial = UnixSerial(
            path=path,
            baudrate=baudrate,
            parity=parity,
            stopbits=stopbits,
            xonxoff=xonxoff,
            rtscts=rtscts,
            byte_size=byte_size,
            connect_timeout=connect_timeout,
        )
        self._extra["serial"] = self._serial
        self._stream_connection_lost_waiter = self._loop.create_future()

        async with asyncio_timeout(connect_timeout):
            stream_transport, _ = await self._loop.create_unix_connection(
                lambda: _UnixProxyProtocol(self), path=self._serial._socket_path
            )

        self._stream_transport = stream_transport
        if self._connection_lost_called:
            stream_transport.close()
            if self._stream_connection_lost_waiter is not None:
                await self._stream_connection_lost_waiter
            self._stream_transport = None
            return
        self._call_protocol_connection_made()

    def _data_received(self, data: bytes) -> None:
        self._protocol.data_received(data)

    def _pause_writing(self) -> None:
        self._protocol.pause_writing()

    def _resume_writing(self) -> None:
        self._protocol.resume_writing()

    def _connection_lost(self, exc: Exception | None) -> None:
        if self._connection_lost_called:
            return
        self._closing = True
        self._stream_transport = None
        if not self._user_initiated_close:
            broken = exc or OSError(errno.EIO, "Unix socket closed by peer")
            self._mark_broken(broken)
        self._call_protocol_connection_lost(exc)

    def _stream_connection_lost(self) -> None:
        waiter = self._stream_connection_lost_waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    def write(self, data: bytes | bytearray | memoryview) -> None:
        self._check_broken()
        assert self._stream_transport is not None
        self._stream_transport.write(data)

    def pause_reading(self) -> None:
        assert self._stream_transport is not None
        self._stream_transport.pause_reading()

    def resume_reading(self) -> None:
        assert self._stream_transport is not None
        self._stream_transport.resume_reading()

    def abort(self) -> None:
        if self._connection_lost_called:
            return
        self._closing = True
        self._mark_user_closed()
        if self._stream_transport is not None:
            self._stream_transport.abort()
        else:
            self._connection_lost(None)

    def close(self) -> None:
        if self._connection_lost_called:
            return
        self._closing = True
        self._mark_user_closed()
        self._arm_close_timeout()
        if self._stream_transport is not None:
            self._stream_transport.close()
        else:
            self._connection_lost(None)

    async def _flush(self) -> None:
        pass

    async def _get_modem_pins(self) -> ModemPins:
        return ModemPins()

    async def _set_modem_pins(self, modem_pins: ModemPins) -> None:
        pass

    def get_write_buffer_size(self) -> int:
        if self._stream_transport is not None:
            return self._stream_transport.get_write_buffer_size()
        return 0

    def get_write_buffer_limits(self) -> tuple[int, int]:
        if self._stream_transport is None:
            return (0, 0)
        return self._stream_transport.get_write_buffer_limits()

    def set_write_buffer_limits(
        self, high: int | None = None, low: int | None = None
    ) -> None:
        if self._stream_transport is None:
            raise RuntimeError("Transport not connected")
        self._stream_transport.set_write_buffer_limits(high=high, low=low)

    def can_write_eof(self) -> bool:
        return (
            self._stream_transport.can_write_eof()
            if self._stream_transport is not None
            else False
        )

    async def _reconfigure_port(self, update: PortSettingsUpdate) -> None:
        pass


register_uri_handler(
    scheme="unix://",
    unique_scheme="unix://",
    sync_cls=UnixSerial,
    async_transport_cls=UnixSerialTransport,
    connect_kwargs=frozenset({"connect_timeout"}),
)
