"""Bounded error-only diagnostic JSONL, with request correlation and redaction."""
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading
import traceback
from urllib.parse import urlsplit, urlunsplit
import uuid

CONTEXT = ContextVar('error_context', default=None)
LOCK = threading.RLock()
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 5
PRIVATE = re.compile(r'password|passwd|secret|token|authorization|cookie|private.?key|api.?key|signature|credential|policy|^input$|^body$|^headers$', re.I)


def clean_text(value):
    text = str(value)
    text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[PRIVATE KEY REMOVED]', text, flags=re.S)
    def url(match):
        try:
            p = urlsplit(match.group())
            host = p.hostname or ''
            if ':' in host:
                host = '[' + host + ']'
            return urlunsplit((p.scheme, host + (':' + str(p.port) if p.port else ''), p.path, '', ''))
        except ValueError:
            return '[URL REMOVED]'
    text = re.sub(r'https?://[^\s<>"\']+', url, text)
    text = re.sub(r'(?i)\bBearer\s+[^\s,;"\']+', 'Bearer [REDACTED]', text)
    text = re.sub(r'(?i)(["\']?(?:password|passwd|client_secret|refresh_token|access_token|authorization|cookie|api_key|x-amz-security-token|x-amz-signature)["\']?\s*[:=]\s*)("[^"]*"|\x27[^\x27]*\x27|[^\s,;]+)', r'\1[REDACTED]', text)
    text = re.sub(r'\b(?:sk-[A-Za-z0-9_-]{12,}|Atzr\|[^\s"\']+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)', '[REDACTED]', text)
    return text[:16000] + (' [TRUNCATED]' if len(text) > 16000 else '')


def sanitize(value):
    if isinstance(value, dict):
        return {str(k): '[REDACTED]' if PRIVATE.search(str(k)) else sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value[:200]]
    if isinstance(value, str):
        return clean_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return clean_text(value)


def folder():
    if os.environ.get('SHOP_ERROR_LOG_DIR'):
        return Path(os.environ['SHOP_ERROR_LOG_DIR'])
    main = sys.modules.get('app.main')
    root = Path(getattr(main, 'DATA_ROOT', os.environ.get('ECOMMERCE2_DATA_DIR', Path(__file__).resolve().parents[1] / 'data')))
    return root / 'error_logs'


def record(event, exc=None, **details):
    context = CONTEXT.get()
    error_id = uuid.uuid4().hex
    item = {'time_utc': datetime.now(timezone.utc).isoformat(), 'level': 'ERROR', 'event': event,
            'error_id': error_id, 'pid': os.getpid(), **({k: v for k, v in context.items() if k != 'logged'} if context else {}), **details}
    if exc is not None:
        item['exception_type'] = type(exc).__name__
        item['message'] = getattr(exc, 'detail', None) or str(exc)
        # No frame locals or source lines: preserve the full call path without secrets.
        chain = []
        seen = set()
        current = exc
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            chain.append({'type': type(current).__name__, 'message': getattr(current, 'detail', None) or str(current),
                          'frames': [{'file': f.filename, 'line': f.lineno, 'function': f.name} for f in traceback.extract_tb(current.__traceback__)]})
            current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
        item['exception_chain'] = chain
    try:
        line = json.dumps(sanitize(item), ensure_ascii=False, allow_nan=False) + '\n'
        with LOCK:
            root = folder()
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = root / 'errors.jsonl'
            if root.is_symlink() or path.is_symlink():
                raise OSError('Error log cannot be a symlink')
            if path.exists() and path.stat().st_size + len(line.encode('utf-8')) > MAX_BYTES:
                for index in range(BACKUPS, 0, -1):
                    source = root / (f'errors.jsonl.{index - 1}' if index > 1 else 'errors.jsonl')
                    destination = root / f'errors.jsonl.{index}'
                    if source.is_symlink() or destination.is_symlink():
                        raise OSError('Error log rotation cannot use symlinks')
                    if source.exists():
                        os.replace(source, destination)
            fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'a', encoding='utf-8') as handle:
                handle.write(line)
        if context is not None:
            context['logged'] = True
    except Exception:
        # Logging must not replay a platform write or conceal its original result.
        print('ERROR: diagnostic log could not be written; check disk space and permissions.', file=sys.stderr)
    return error_id


class ErrorHandler(logging.Handler):
    def emit(self, entry):
        record('python.' + entry.name, entry.exc_info[1] if entry.exc_info else None, message=entry.getMessage())


def configure():
    root = logging.getLogger()
    if not any(isinstance(handler, ErrorHandler) for handler in root.handlers):
        root.addHandler(ErrorHandler(level=logging.ERROR))


class ErrorMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        context = {'request_id': uuid.uuid4().hex, 'method': scope['method'], 'request_path': scope['path'], 'logged': False}
        reset = CONTEXT.set(context)
        status = 0
        error_body = bytearray()
        async def capture(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
                message = dict(message, headers=[*message.get('headers', []), (b'x-request-id', context['request_id'].encode())])
            if message['type'] == 'http.response.body' and status >= 400 and len(error_body) < 65536:
                error_body.extend(message.get('body', b'')[:65536-len(error_body)])
            await send(message)
        try:
            await self.app(scope, receive, capture)
            if status >= 400 and not context['logged']:
                details = {}
                try:
                    parsed = json.loads(error_body)
                    if isinstance(parsed, dict):
                        details = {k:v for k,v in parsed.items() if k in {'detail','message','error','code'}}
                except (ValueError, UnicodeError):
                    pass
                record('http.failed', http_status=status, response_error=details)
        except Exception as exc:
            record('http.unhandled', exc, http_status=500)
            raise
        finally:
            CONTEXT.reset(reset)


def install(app):
    from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException
    configure()
    app.add_middleware(ErrorMiddleware)
    @app.exception_handler(HTTPException)
    async def handled(request, exc):
        record('http.rejected', exc, http_status=exc.status_code)
        return await http_exception_handler(request, exc)
    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        record('http.invalid_request', http_status=422, errors=[{k: v for k, v in e.items() if k in {'loc', 'msg', 'type'}} for e in exc.errors()])
        return await request_validation_exception_handler(request, exc)
