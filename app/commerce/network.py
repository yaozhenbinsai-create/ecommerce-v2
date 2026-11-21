"""HTTPS retrieval with DNS validation bound to the actual connection address."""
from contextlib import contextmanager
import ipaddress
import socket

import httpx
from fastapi import HTTPException


def _destination(url):
    try:
        parsed = httpx.URL(url)
        if parsed.scheme != 'https' or not parsed.host or parsed.userinfo:
            raise ValueError()
        addresses = socket.getaddrinfo(parsed.raw_host.decode('ascii'), parsed.port or 443, type=socket.SOCK_STREAM)
        ips = list(dict.fromkeys(str(ipaddress.ip_address(row[4][0])) for row in addresses))
    except (ValueError, TypeError, httpx.InvalidURL):
        raise HTTPException(422, '需要有效的公网HTTPS地址') from None
    except OSError:
        raise HTTPException(422, '图片/文档域名无法解析') from None
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise HTTPException(422, '不允许访问内网地址')
    return parsed, ips


def public_url(url):
    _destination(url)
    return url


class _PublicTransport(httpx.HTTPTransport):
    def __init__(self):
        # Do not pool by pinned IP across different TLS hostnames, or route
        # through environment proxies that resolve the hostname independently.
        super().__init__(trust_env=False, limits=httpx.Limits(max_keepalive_connections=0))

    def handle_request(self, request):
        original, addresses = _destination(request.url)
        # This production host's IPv6 route can connect but stall while reading
        # public media. Prefer verified IPv4 answers, retaining IPv6 fallback
        # and validating every DNS answer before choosing any destination.
        addresses = sorted(addresses, key=lambda address: ipaddress.ip_address(address).version)
        for index, address in enumerate(addresses):
            pinned = httpx.Request(request.method, original.copy_with(host=address),
                                   headers=request.headers, stream=request.stream,
                                   extensions={**request.extensions, 'sni_hostname': original.raw_host.decode('ascii')})
            # Keep the original HTTP Host and TLS certificate hostname while
            # connecting only to a numeric IP from the validated DNS answer.
            try:
                return super().handle_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if index == len(addresses) - 1:
                    raise


@contextmanager
def public_stream(method, url, **kwargs):
    try:
        url = httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError):
        raise HTTPException(422, '需要有效的公网HTTPS地址') from None
    with httpx.Client(transport=_PublicTransport(), trust_env=False, follow_redirects=False) as client:
        with client.stream(method, url, **kwargs) as response:
            yield response


def public_post(url, **kwargs):
    with public_stream('POST', url, **kwargs) as response:
        response.read()
        return response
