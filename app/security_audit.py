"""Minimal security event log; never accepts request bodies, tokens or customer data."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
from datetime import datetime, timezone
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

LOCK = threading.RLock()
LABEL = re.compile(r'^[a-zA-Z0-9_.:-]{1,80}$')


def root():
    main = sys.modules.get('app.main')
    return Path(getattr(main, 'DATA_ROOT', os.environ.get('ECOMMERCE2_DATA_DIR', Path(__file__).resolve().parents[1] / 'data')))


def emit(event, outcome, actor=None):
    """Fail closed on storage failure; actor is pseudonymized, no free-form details."""
    if not isinstance(event, str) or not LABEL.fullmatch(event) or not isinstance(outcome, str) or not LABEL.fullmatch(outcome):
        raise ValueError('Invalid security event label')
    now = datetime.now(timezone.utc)
    record = {'time': now.isoformat(), 'event': event, 'outcome': outcome}
    if actor is not None:
        record['actor_hash'] = hashlib.sha256(str(actor).encode('utf-8')).hexdigest()
    folder = root() / 'security_audit'
    with LOCK:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        filename = folder / (now.strftime('%Y-%m-%d') + '.jsonl')
        if folder.is_symlink() or filename.is_symlink():
            raise OSError('Security audit path cannot be a symbolic link')
        fd = os.open(filename, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, ensure_ascii=True, separators=(',', ':')) + '\n')
            handle.flush()
            os.fsync(handle.fileno())


def install(app):
    """Audit authenticated business-data access without logging URLs or payloads."""
    @app.middleware('http')
    async def audit_access(request, call_next):
        response = await call_next(request)
        actor = getattr(request.state, 'commerce_username', None)
        if actor and (request.url.path.startswith('/api/') or
                      request.url.path.startswith(('/uploads/', '/design-outputs/', '/production-materials/'))):
            outcome = 'success' if response.status_code < 400 else 'denied' if response.status_code in {401, 403} else 'failed'
            operation = 'read' if request.method in {'GET', 'HEAD'} else 'change'
            try:
                await run_in_threadpool(emit, 'business_data.' + operation, outcome, actor)
            except OSError:
                # The route may already have committed a change, or returned its
                # own controlled audit error. Never expose the original body or
                # replace that failure with an unhandled plain-text 500.
                from app.commerce.access import _secure_headers
                failed = _secure_headers(JSONResponse(
                    {'detail': '安全审计存储暂不可用；本次操作结果未确认，请刷新核对状态后再操作'},
                    status_code=503,
                ))
                failed.headers['Pragma'] = 'no-cache'
                for header in ('Content-Security-Policy', 'Permissions-Policy',
                               'Cross-Origin-Opener-Policy', 'Cross-Origin-Resource-Policy'):
                    if header in response.headers:
                        failed.headers[header] = response.headers[header]
                return failed
        return response
