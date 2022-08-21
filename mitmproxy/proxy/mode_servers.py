"""
This module defines "server instances", which manage
the TCP/UDP servers spawned by mitmproxy as specified by the proxy mode.

Example:

    mode = ProxyMode.parse("reverse:https://example.com")
    inst = ServerInstance.make(mode, manager_that_handles_callbacks)
    await inst.start()
    # TCP server is running now.
"""
from __future__ import annotations

import asyncio
import errno
import struct
import typing
from abc import ABCMeta, abstractmethod
from contextlib import contextmanager
from typing import ClassVar, Generic, TypeVar, cast, get_args

from mitmproxy import ctx, flow, log, platform
from mitmproxy.connection import Address
from mitmproxy.master import Master
from mitmproxy.net import udp
from mitmproxy.proxy import commands, layers, mode_specs, server
from mitmproxy.proxy.context import Context
from mitmproxy.proxy.layer import Layer
from mitmproxy.utils import asyncio_utils, human


class ProxyConnectionHandler(server.LiveConnectionHandler):
    master: Master

    def __init__(self, master, r, w, options, mode):
        self.master = master
        super().__init__(r, w, options, mode)
        self.log_prefix = f"{human.format_address(self.client.peername)}: "

    async def handle_hook(self, hook: commands.StartHook) -> None:
        with self.timeout_watchdog.disarm():
            # We currently only support single-argument hooks.
            (data,) = hook.args()
            await self.master.addons.handle_lifecycle(hook)
            if isinstance(data, flow.Flow):
                await data.wait_for_resume()  # pragma: no cover

    def log(self, message: str, level: str = "info") -> None:
        x = log.LogEntry(self.log_prefix + message, level)
        asyncio_utils.create_task(
            self.master.addons.handle_lifecycle(log.AddLogHook(x)),
            name="ProxyConnectionHandler.log",
        )


M = TypeVar('M', bound=mode_specs.ProxyMode)


class ServerManager(typing.Protocol):
    connections: dict[tuple, ProxyConnectionHandler]

    @contextmanager
    def register_connection(self, connection_id: tuple, handler: ProxyConnectionHandler):
        ...  # pragma: no cover


class ServerInstance(Generic[M], metaclass=ABCMeta):

    __modes: ClassVar[dict[str, type[ServerInstance]]] = {}

    def __init__(self, mode: M, manager: ServerManager):
        self.mode: M = mode
        self.manager: ServerManager = manager
        self.last_exception: Exception | None = None

    def __init_subclass__(cls, **kwargs):
        """Register all subclasses so that make() finds them."""
        # extract mode from Generic[Mode].
        mode = get_args(cls.__orig_bases__[0])[0]
        if mode != M:
            assert mode.type not in ServerInstance.__modes
            ServerInstance.__modes[mode.type] = cls

    @staticmethod
    def make(
        mode: mode_specs.ProxyMode | str,
        manager: ServerManager,
    ) -> ServerInstance:
        if isinstance(mode, str):
            mode = mode_specs.ProxyMode.parse(mode)
        return ServerInstance.__modes[mode.type](mode, manager)

    @property
    @abstractmethod
    def is_running(self) -> bool:
        pass

    @abstractmethod
    async def start(self) -> None:
        pass

    @abstractmethod
    async def stop(self) -> None:
        pass

    @property
    @abstractmethod
    def listen_addrs(self) -> tuple[Address, ...]:
        pass


class AsyncioServerInstance(ServerInstance[M], metaclass=ABCMeta):
    _server: asyncio.Server | udp.UdpServer | None = None
    _listen_addrs: tuple[Address, ...] = tuple()

    @property
    def is_running(self) -> bool:
        return self._server is not None

    async def start(self):
        assert self._server is None
        host = self.mode.listen_host(ctx.options.listen_host)
        port = self.mode.listen_port(ctx.options.listen_port)
        try:
            self._server = await self.listen(host, port)
            self._listen_addrs = tuple(s.getsockname() for s in self._server.sockets)
        except OSError as e:
            self.last_exception = e
            message = f"{self.log_desc} failed to listen on {host or '*'}:{port} with {e}"
            if e.errno == errno.EADDRINUSE and self.mode.custom_listen_port is None:
                assert self.mode.custom_listen_host is None  # since [@ [listen_addr:]listen_port]
                message += f"\nTry specifying a different port by using `--mode {self.mode.full_spec}@{port + 1}`."
            raise OSError(e.errno, message, e.filename) from e
        except Exception as e:
            self.last_exception = e
            raise
        else:
            self.last_exception = None
        ctx.log.info(f"{self.log_desc} listening at {' and '.join(map(human.format_address, self._listen_addrs))}.")

    async def stop(self):
        assert self._server is not None
        # we always reset _server and _listen_addrs and ignore failures
        server = self._server
        listen_addrs = self._listen_addrs
        self._server = None
        self._listen_addrs = tuple()
        try:
            server.close()
            await server.wait_closed()
        except Exception as e:
            self.last_exception = e
            raise
        else:
            self.last_exception = None
        ctx.log.info(f"Stopped {self.log_desc} at {' and '.join(map(human.format_address, listen_addrs))}.")

    @abstractmethod
    async def listen(self, host: str, port: int) -> asyncio.Server | udp.UdpServer:
        pass

    @property
    @abstractmethod
    def log_desc(self) -> str:
        pass

    @property
    def is_transparent(self) -> bool:
        return False

    @property
    def listen_addrs(self) -> tuple[Address, ...]:
        return self._listen_addrs


class TcpServerInstance(AsyncioServerInstance[M], metaclass=ABCMeta):

    @abstractmethod
    def make_top_layer(self, context: Context) -> Layer:
        pass

    async def handle_tcp_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        connection_id = (
            "tcp",
            writer.get_extra_info("peername"),
            writer.get_extra_info("sockname"),
        )
        handler = ProxyConnectionHandler(
            ctx.master, reader, writer, ctx.options, self.mode
        )
        if self.is_transparent:
            assert platform.original_addr is not None
            socket = writer.get_extra_info("socket")
            try:
                handler.layer.context.server.address = platform.original_addr(socket)
            except Exception as e:
                ctx.log.error(f"Transparent mode failure: {e!r}")
        handler.layer = self.make_top_layer(handler.layer.context)
        with self.manager.register_connection(connection_id, handler):
            await handler.handle_client()

    async def listen(self, host: str, port: int) -> asyncio.Server:
        return await asyncio.start_server(
            self.handle_tcp_connection,
            host,
            port,
        )


class RegularInstance(TcpServerInstance[mode_specs.RegularMode]):
    log_desc = "HTTP(S) proxy"

    def make_top_layer(self, context: Context) -> Layer:
        return layers.modes.HttpProxy(context)


class UpstreamInstance(TcpServerInstance[mode_specs.UpstreamMode]):
    log_desc = "HTTP(S) proxy (upstream mode)"

    def make_top_layer(self, context: Context) -> Layer:
        return layers.modes.HttpUpstreamProxy(context)


class TransparentInstance(TcpServerInstance[mode_specs.TransparentMode]):
    log_desc = "Transparent proxy"
    is_transparent = True

    def make_top_layer(self, context: Context) -> Layer:
        return layers.modes.TransparentProxy(context)


class ReverseInstance(TcpServerInstance[mode_specs.ReverseMode]):
    @property
    def log_desc(self) -> str:
        return f"Reverse proxy to {self.mode.data}"

    def make_top_layer(self, context: Context) -> Layer:
        return layers.modes.ReverseProxy(context)


class Socks5Instance(TcpServerInstance[mode_specs.Socks5Mode]):
    log_desc = "SOCKS v5 proxy"

    def make_top_layer(self, context: Context) -> Layer:
        return layers.modes.Socks5Proxy(context)


class UdpServerInstance(AsyncioServerInstance[M], metaclass=ABCMeta):

    @abstractmethod
    def make_top_layer(self, context: Context) -> Layer:
        pass

    @abstractmethod
    def make_connection_id(
        self,
        data: bytes,
        remote_addr: Address,
        local_addr: Address,
    ) -> tuple | None:
        pass

    async def listen(self, host: str, port: int) -> udp.UdpServer:
        return await udp.start_server(
            self.handle_udp_datagram,
            host,
            port,
            transparent=self.is_transparent
        )

    def handle_udp_datagram(
        self,
        transport: asyncio.DatagramTransport,
        data: bytes,
        remote_addr: Address,
        local_addr: Address,
    ) -> None:
        connection_id = self.make_connection_id(transport, data, remote_addr, local_addr)
        if connection_id is None:
            return
        if connection_id not in self.manager.connections:
            reader = udp.DatagramReader()
            writer = udp.DatagramWriter(transport, remote_addr, reader)
            handler = ProxyConnectionHandler(
                ctx.master, reader, writer, ctx.options, self.mode
            )
            handler.client.transport_protocol = "udp"
            handler.timeout_watchdog.CONNECTION_TIMEOUT = 20
            if self.is_transparent:
                handler.layer.context.server.address = local_addr
            handler.layer.context.server.transport_protocol = "udp"
            handler.layer = self.make_top_layer(handler.layer.context)

            # pre-register here - we may get datagrams before the task is executed.
            self.manager.connections[connection_id] = handler
            asyncio.create_task(self.handle_udp_connection(connection_id, handler))
        else:
            handler = self.manager.connections[connection_id]
            reader = cast(udp.DatagramReader, handler.transports[handler.client].reader)
        reader.feed_data(data, remote_addr)

    async def handle_udp_connection(self, connection_id: tuple, handler: ProxyConnectionHandler) -> None:
        with self.manager.register_connection(connection_id, handler):
            await handler.handle_client()


class DnsInstance(UdpServerInstance[mode_specs.DnsMode]):
    log_desc = "DNS server"

    def is_transparent(self) -> bool:
        return self.mode.data == "transparent"

    def make_top_layer(self, context: Context) -> Layer:
        context.server.address = (self.mode.data or "resolve-local", 53)
        return layers.DNSLayer(context)

    def make_connection_id(
        self,
        data: bytes,
        remote_addr: Address,
        local_addr: Address,
    ) -> tuple | None:
        try:
            dns_id = struct.unpack_from("!H", data, 0)
        except struct.error:
            ctx.log.info(
                f"Invalid DNS datagram received from {human.format_address(remote_addr)}."
            )
            return None
        else:
            return ("udp", dns_id, remote_addr, local_addr)


class UdpInstance(UdpServerInstance[mode_specs.UdpMode]):
    """Wrapper for a TcpServerInstance that also supports UDP."""

    def __init__(self, mode: mode_specs.UdpMode, manager: ServerManager):
        super().__init__(mode.inner_mode, manager)
        self.inner_server = cast(TcpServerInstance, ServerInstance.make(mode.inner_mode))

    def is_transparent(self) -> bool:
        return self.inner_server.is_transparent

    def log_desc(self) -> str:
        return f"{self.inner_server.log_desc} (UDP)"

    def make_top_layer(self, context: Context) -> Layer:
        return self.inner_server.make_top_layer(context)

    def make_connection_id(
        self,
        data: bytes,
        remote_addr: Address,
        local_addr: Address,
    ) -> tuple | None:
        return ("udp", remote_addr, local_addr)
