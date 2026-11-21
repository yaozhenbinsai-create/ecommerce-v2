"""Private failed image receipts and safe, structured provider rejections."""
import base64
import gzip
import hashlib
import io
import json
import re
import uuid
import zlib
from pathlib import Path

from app import security_secrets

DEFINITE_REJECTIONS = {
    'invalid_api_key', 'authentication_error', 'insufficient_quota', 'model_not_found',
    'invalid_request_error', 'invalid_model', 'permission_denied', 'content_policy_violation',
    'unsupported_model', 'unsupported_parameter',
}
RETRYABLE_REJECTIONS = {'rate_limit_exceeded', 'rate_limit_error'}
PROVIDER_FAILURE_TYPES = {'upstream_error', 'api_error', 'server_error',
                          'internal_server_error', 'service_unavailable', 'overloaded_error'}
UNCERTAIN_OUTCOME = re.compile(r'\b(?:timed?\s*out|timeout|deadline|interrupted|accepted|pending|processing|in\s+progress|unknown|cancelled|canceled|aborted|incomplete)\b', re.I)


def rejection(body):
    """Explicit error-only provider failures use normal, finite model failover."""
    if not isinstance(body, dict) or any(body.get(k) for k in ('data', 'output', 'images', 'url', 'b64_json', 'id', 'task_id', 'request_id')):
        return None
    error = body.get('error')
    if not isinstance(error, dict):
        return None
    if any(error.get(k) for k in ('id','task_id','request_id','url','data','output','images','b64_json')):
        return None
    for field in ('code', 'type'):
        code = error.get(field)
        if isinstance(code, str) and code in DEFINITE_REJECTIONS | RETRYABLE_REJECTIONS:
            return code, 'transient' if code in RETRYABLE_REJECTIONS else 'invalid'
    # Compatible gateways sometimes wrap failed upstream responses in HTTP
    # 200. Interpret the typed error envelope, not a growing list of phrases.
    # A reported failure can have incurred a provider charge; retain its
    # receipt/attempt and follow the configured bounded model chain. A timeout
    # or pending/accepted response still needs its original outcome reconciled.
    kind=error.get('type')
    message=error.get('message')
    if kind in PROVIDER_FAILURE_TYPES and isinstance(message,str) and message.strip():
        outcome=' '.join(str(error.get(k) or '') for k in ('type','code','message','status')).replace('_',' ').replace('-',' ')
        if not UNCERTAIN_OUTCOME.search(outcome):
            return kind,'transient'
    return None


def _saved_response(main, task):
    """One private receipt integrity check for retry and retirement callers."""
    remote = task.get('remote') or {}
    if not isinstance(remote, dict):
        return None
    metadata = remote.get('image_receipt') or {}
    if not isinstance(metadata, dict):
        return None
    key = metadata.get('key', '')
    root = getattr(main, 'DATA_ROOT', None)
    if (not isinstance(root, (str, Path)) or not isinstance(key, str)
            or not re.fullmatch(r'[a-f0-9]{32}', key)
            or metadata.get('protocol') != 'private-image-receipt-v1'
            or metadata.get('storage') != 'encrypted' or metadata.get('complete') is not True
            or type(metadata.get('bytes')) is not int or not 0 < metadata['bytes'] <= 32 * 1024 * 1024
            or metadata.get('recoverable_handle_present') is not False):
        return None
    status = metadata.get('http_status')
    if not (type(status) is int and ((metadata.get('reason') == 'unusable_artifact' and status == 200)
            or (metadata.get('reason') == 'http_error' and 400 <= status < 600))):
        return None
    attempt = metadata.get('attempt')
    current = task.get('total_attempts')
    if attempt is not None:
        if type(attempt) is not int or attempt < 1 or attempt != current:return None
    elif task.get('status') != 'archived' and current != 1:
        # Older receipts did not identify their attempt. They cannot prove the
        # latest outcome after a fallback; already retired history stays readable.
        return None
    try:
        path = Path(root) / 'image_receipts' / (key + '.enc')
        for part in (path, path.parent):
            if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():return None
        if not path.resolve().is_relative_to(Path(root).resolve()):return None
        saved = security_secrets.load_json(path)
        original = saved.get('metadata') or {}
        if (not isinstance(original, dict)
                or saved.get('task_id') != task.get('id') or saved.get('run_id') != task.get('run_id')
                or saved.get('complete') is not True
                or any(original.get(k) != metadata.get(k) for k in
                       ('protocol', 'sha256', 'bytes', 'reason', 'http_status', 'recoverable_handle_present', 'attempt'))):
            return None
        compressed = base64.b64decode(saved['body_gzip_b64'], validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            raw = stream.read(metadata['bytes'] + 1)
        if len(raw) != metadata.get('bytes') or hashlib.sha256(raw).hexdigest() != metadata.get('sha256'):
            return None
        return {'body': raw, 'metadata': metadata}
    except (OSError, ValueError, TypeError, KeyError, EOFError, zlib.error,
            security_secrets.SecretStorageError):
        return None
    return None


def saved_rejection(main, task):
    """Retry classification stays limited to explicit failures in HTTP 200 receipts."""
    saved = _saved_response(main, task)
    if not saved or saved['metadata']['reason'] != 'unusable_artifact':return None
    try:
        rejected = rejection(json.loads(saved['body']))
    except (ValueError, UnicodeDecodeError):
        return None
    if rejected:
        code, kind = rejected
        return {'code': code, 'kind': kind, 'receipt_key': saved['metadata']['key'],
                'receipt_sha256': saved['metadata']['sha256']}
    return None


def retirable_failure(main, task):
    """Owner-directed clearing may retire an intact returned failure, never retry it."""
    if task.get('status') not in {'paused', 'archived'} or task.get('kind') not in {'image', 'master'}:
        return False
    saved = _saved_response(main, task)
    if not saved:return False
    try:
        body = json.loads(saved['body'])
    except (ValueError, UnicodeDecodeError):
        return saved['metadata']['reason'] == 'unusable_artifact'
    if saved['metadata']['reason'] == 'http_error':return bool(rejection(body))
    if isinstance(body, dict):
        if any(body.get(k) for k in ('id', 'task_id', 'request_id', 'url', 'output', 'images')):return False
        error = body.get('error')
        if isinstance(error, dict):
            if any(error.get(k) for k in ('id','task_id','request_id','url','data','output','images','b64_json')):return False
            if UNCERTAIN_OUTCOME.search(' '.join(str(error.get(k) or '') for k in ('type','code','message','status'))):return False
        elif isinstance(error, str) and UNCERTAIN_OUTCOME.search(error):return False
    return True


def retain(main, response, task, checkpoint, reason):
    """Never put raw server text, signed URLs, or echoed credentials in public state."""
    raw = response.content
    metadata = {'protocol': 'private-image-receipt-v1', 'http_status': response.status_code,
                'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'reason': reason}
    attempt = task.get('total_attempts')
    if type(attempt) is int and attempt > 0:metadata['attempt'] = attempt
    content_type = response.headers.get('content-type', '').split(';', 1)[0].lower()
    metadata['content_type'] = content_type if content_type in {
        'application/json', 'text/html', 'text/plain', 'text/event-stream',
        'image/png', 'image/jpeg', 'image/webp', 'application/octet-stream'} else 'other'
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        metadata['schema_fields'] = [k for k in ('data', 'error', 'output', 'images', 'id', 'task_id', 'request_id', 'status') if k in body]
        data = body.get('data')
        metadata['recoverable_handle_present'] = bool(
            any(body.get(k) for k in ('id', 'task_id', 'request_id', 'url', 'output', 'images')) or
            (isinstance(data, list) and any(isinstance(x, dict) and x.get('url') for x in data)))
    root = getattr(main, 'DATA_ROOT', None)
    if not isinstance(root, (str, Path)):
        metadata['storage'] = 'unavailable'
    else:
        key = uuid.uuid4().hex
        path = Path(root) / 'image_receipts' / (key + '.enc')
        try:
            # Successful images already have durable output files. Only failed
            # receipts come here; bound pathological responses before encryption.
            compressed = gzip.compress(raw, compresslevel=3) if len(raw) <= 32 * 1024 * 1024 else b''
            complete = bool(compressed) and len(compressed) <= 5 * 1024 * 1024
            payload = {'metadata': metadata, 'task_id': task.get('id'), 'run_id': task.get('run_id'),
                       'complete': complete,
                       'body_gzip_b64': base64.b64encode(compressed).decode() if complete else ''}
            # Request IDs are recoverable evidence but not displayed without review.
            payload['request_headers'] = {k: response.headers[k] for k in ('x-request-id', 'request-id', 'x-amzn-requestid')
                                          if k in response.headers}
            security_secrets.save_json(path, payload)
            metadata.update(storage='encrypted', key=key, complete=complete)
        except (OSError, security_secrets.SecretStorageError):
            metadata['storage'] = 'failed'
    checkpoint({'image_receipt': metadata})
    return metadata
