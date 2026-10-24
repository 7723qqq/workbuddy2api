"""Share TLS configuration while keeping each request's cookies isolated."""
from contextlib import asynccontextmanager
import ssl
import threading

import certifi
import httpx
from admin import request_metrics
from admin.config import settings

_ssl_context = None
_ssl_lock = threading.Lock()


def ssl_context():
    global _ssl_context
    with _ssl_lock:
        if _ssl_context is None:
            # 信任库 = 系统证书库 + certifi：
            #   * 系统证书库覆盖本机第三方代理（如 Steam++ 加速器）自动装进去的根证书。
            #     它每次升级会**重签自己的 CA**，静态信任包会在下一次更新后失效 ——
            #     表现为上游全部 CERTIFICATE_VERIFY_FAILED（2026-10-24 线上即此故障：
            #     号池 5 个号 20 秒内全部降权、全池 503）。
            #   * certifi 保证不把「能不能连通上游」押在操作系统的根证书集合上。
            ctx = ssl.create_default_context()
            ctx.load_verify_locations(cafile=certifi.where())
            _ssl_context = ctx
        return _ssl_context


class _BorrowedTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport):
        self.transport = transport

    async def handle_async_request(self, request):
        # Attach timings to the actual request; no URLs, headers or bodies retained.
        if 'trace' not in request.extensions:
            sync_trace = request_metrics.network_trace()
            async def trace(name, info):
                sync_trace(name, info)
            request.extensions['trace'] = trace
        return await self.transport.handle_async_request(request)

    async def aclose(self):
        # The application owns the pool, not an individual request.
        pass


async def start(app, limits):
    # Reuse a few recent connections instead of doing DNS/TCP/TLS on every
    # mobile call. HTTPX discards expired/closed sockets; do not retry POSTs
    # automatically as a conversation may already have reached the upstream.
    mobile_limits = httpx.Limits(max_connections=limits.max_connections,
                               max_keepalive_connections=4, keepalive_expiry=30)
    app.state.upstream_transport = httpx.AsyncHTTPTransport(limits=mobile_limits, verify=ssl_context())


async def stop(app):
    transport = getattr(app.state, 'upstream_transport', None)
    app.state.upstream_transport = None
    if transport is not None:
        await transport.aclose()


@asynccontextmanager
async def client(request, timeout, limits):
    # Direct route tests can create a Request without an application in scope.
    app = request.scope.get('app') if hasattr(request, 'scope') else getattr(request, 'app', None)
    transport = getattr(getattr(app, 'state', None), 'upstream_transport', None)
    kwargs = {'timeout': timeout, 'limits': limits,
              'trust_env': settings.UPSTREAM_TRUST_ENV}
    if transport is not None:
        kwargs['transport'] = _BorrowedTransport(transport)
    else:
        # 显式给信任库：绕开 SSL_CERT_FILE 指向的静态包（那里面可能是一张已过期的
        # 代理根证书），同时保证 opt-in 走代理时也能验过代理自己的根证书。
        kwargs['verify'] = ssl_context()
    async with httpx.AsyncClient(**kwargs) as connection:
        yield connection
