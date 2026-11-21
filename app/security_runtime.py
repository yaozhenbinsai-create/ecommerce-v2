"""Explicit public HTTPS deployment configuration, independent of the application DB."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import ssl


def load_config():
    root = Path(os.environ.get('ECOMMERCE2_DATA_DIR', Path(__file__).resolve().parents[1] / 'data')).resolve()
    path = root / 'security_runtime.json'
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or set(data) - {'enabled', 'host', 'port', 'tls_cert', 'tls_key', 'public_url', 'tunnel_origin', 'public_media_enabled', 'mfa_required', 'amazon_pii_enabled', 'shareholder_password_login_enabled'}:
            raise ValueError()
        if type(data.get('enabled')) is not bool:
            raise ValueError()
        for field in ('public_media_enabled', 'mfa_required', 'amazon_pii_enabled', 'shareholder_password_login_enabled'):
            if field in data and type(data[field]) is not bool:
                raise ValueError()
        if data.get('public_media_enabled') and (not data['enabled'] or not (data.get('tunnel_origin') or data.get('public_url'))):
            raise ValueError()
        if 'tunnel_origin' in data:
            validate_public_url(data['tunnel_origin'], 443)
            if data['enabled'] is not True or any(data.get(k) for k in ('tls_cert', 'tls_key', 'public_url')):
                raise ValueError()
        if data.get('public_url'):
            validate_public_url(data['public_url'], data.get('port', 8443))
        return data
    except (OSError, ValueError) as exc:
        raise RuntimeError('安全部署配置损坏；已停止启动，请修复 security_runtime.json') from exc


def validate_public_url(value, port):
    from urllib.parse import urlsplit
    if not isinstance(value, str):
        raise ValueError('PUBLIC_URL必须是文本')
    url = urlsplit(value)
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path not in {'', '/'}
            or (url.port or 443) != port):
        raise ValueError('PUBLIC_URL必须是与监听端口一致的HTTPS域名地址')
    try:
        address = ipaddress.ip_address(url.hostname)
    except ValueError:
        if '.' not in url.hostname or url.hostname == 'localhost':
            raise ValueError('公网图片需要有效公网域名') from None
    else:
        if not address.is_global:
            raise ValueError('公网图片不能使用内网IP')
    return value.rstrip('/')


def public_media_base():
    data = load_config()
    if data.get('enabled') and data.get('tunnel_origin') and data.get('public_media_enabled'):
        return data['tunnel_origin'].rstrip('/') + '/public-media'
    return data['public_url'].rstrip('/') + '/public-media' if data.get('enabled') and data.get('public_url') else ''


def server_options(*, activate=True):
    data = load_config()
    secure = data.get('enabled') is True or os.environ.get('SHOP_SECURITY_MODE') == '1'
    public = os.environ.get('SHOP_PUBLIC_MODE') == '1'
    if public and not secure:
        raise RuntimeError('公网部署必须启用安全登录和 HTTPS')
    if not secure:
        return {'host': '127.0.0.1', 'port': 8000, 'proxy_headers': False,
                'reload': os.environ.get('SHOP_DEV_RELOAD') == '1', 'access_log': False}
    if os.environ.get('SHOP_DEV_RELOAD') == '1':
        raise RuntimeError('安全部署不支持开发自动重载')
    if data.get('tunnel_origin'):
        host = data.get('host', '127.0.0.1')
        port = data.get('port', 8000)
        try:
            if not ipaddress.ip_address(host).is_loopback or type(port) is not int or not 1 <= port <= 65535:
                raise ValueError()
        except (TypeError, ValueError) as exc:
            raise RuntimeError('隧道模式只能监听本机回环地址及有效端口') from exc
        if activate:
            os.environ['SHOP_SECURITY_MODE'] = '1'
            os.environ['SHOP_PUBLIC_MODE'] = '1'
            os.environ['SHOP_TUNNEL_ORIGIN'] = data['tunnel_origin'].rstrip('/')
            from app.commerce import secure_auth
            secure_auth.validate_configuration()
        # Do not enable Uvicorn's generic proxy trust. The application validates
        # the socket peer, exact Host and Cloudflare headers before normalization.
        return {'host': host, 'port': port, 'proxy_headers': False,
                'reload': False, 'access_log': False}
    cert = data.get('tls_cert') or os.environ.get('SHOP_TLS_CERT', '')
    key = data.get('tls_key') or os.environ.get('SHOP_TLS_KEY', '')
    if not cert or not key or not Path(cert).is_absolute() or not Path(key).is_absolute():
        raise RuntimeError('安全部署需要有效 HTTPS 证书和私钥的绝对路径')
    # Verify the certificate/key pair before binding a network socket.
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        if os.name != 'nt' and Path(key).stat().st_mode & 0o077:
            raise ValueError('TLS private key permissions are too broad')
    except (OSError, ValueError, ssl.SSLError) as exc:
        raise RuntimeError('HTTPS 证书、私钥或私钥权限无效；服务未开放') from exc
    host = data.get('host', os.environ.get('SHOP_BIND_HOST', '127.0.0.1'))
    try:
        ipaddress.ip_address(host)
        port = data.get('port', int(os.environ.get('SHOP_PORT', '8443')))
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError()
    except (TypeError, ValueError) as exc:
        raise RuntimeError('服务监听地址或端口无效') from exc
    if activate:
        os.environ['SHOP_SECURITY_MODE'] = '1'
        os.environ['SHOP_PUBLIC_MODE'] = '1'
        if os.name != 'nt':
            os.umask(0o077)
    return {'host': host, 'port': port, 'ssl_certfile': cert, 'ssl_keyfile': key,
            'ssl_version': ssl.PROTOCOL_TLS_SERVER, 'ssl_ciphers': 'ECDHE+AESGCM:ECDHE+CHACHA20',
            'proxy_headers': False, 'reload': False, 'access_log': False}
