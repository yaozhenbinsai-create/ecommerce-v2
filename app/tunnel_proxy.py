"""Explicit Cloudflare ingress on loopback; never trust Internet proxy headers.

The host's local processes remain trusted, as they can already read application
data. Only the minimal supervisor health check is available without ingress
headers. Public media remains a separate opt-in.
"""
import ipaddress
import os
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse


def prepare_request(request):
    origin = os.environ.get('SHOP_TUNNEL_ORIGIN')
    if not origin:
        return None
    if os.environ.get('SHOP_SECURITY_MODE') != '1' or os.environ.get('SHOP_PUBLIC_MODE') != '1':
        return JSONResponse({'detail': '隧道模式需要安全登录配置'}, status_code=503)
    from app.security_runtime import validate_public_url
    try:
        validate_public_url(origin, 443)
        peer = ipaddress.ip_address(request.client.host) if request.client else None
        if peer is None or not peer.is_loopback:
            raise ValueError()
        hosts = request.headers.getlist('host')
        if len(hosts) != 1:
            raise ValueError()
        host = hosts[0].lower()
        # The existing Windows supervisor probes this endpoint over loopback.
        if (request.method in {'GET', 'HEAD'} and request.scope['path'] == '/healthz'
                and host in {'127.0.0.1:8000', '127.0.0.1', 'localhost:8000', 'localhost', '[::1]:8000', '[::1]'}
                and not any(k.lower().startswith((b'x-forwarded-', b'cf-')) or k.lower() == b'forwarded'
                            for k, _ in request.scope['headers'])):
            return JSONResponse({'status': 'ok'}, headers={'Cache-Control': 'no-store'})
        expected = urlsplit(origin).hostname.lower()
        if host not in {expected, expected + ':443'}:
            raise ValueError()
        if request.headers.getlist('x-forwarded-proto') != ['https']:
            return JSONResponse({'detail': '请使用 HTTPS 公网地址访问'}, status_code=400)
        clients = request.headers.getlist('cf-connecting-ip')
        if len(clients) != 1:
            raise ValueError()
        client = str(ipaddress.ip_address(clients[0]))
    except (ValueError, TypeError, AttributeError):
        return JSONResponse({'detail': '仅允许指定域名通过本机 Cloudflare 隧道访问'}, status_code=403)
    request.scope['scheme'] = 'https'
    request.scope['server'] = (expected, 443)
    request.scope['client'] = (client, 0)
    request.__dict__.pop('_url', None)
    return None
