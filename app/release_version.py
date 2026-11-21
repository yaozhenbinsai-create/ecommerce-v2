"""Immutable code identity at process import and hashes for served resources."""
from __future__ import annotations

import hashlib
from pathlib import Path

APP = Path(__file__).resolve().parent


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _code_hash():
    digest = hashlib.sha256()
    for path in sorted(APP.rglob('*.py')):
        digest.update(path.relative_to(APP).as_posix().encode())
        digest.update(bytes.fromhex(_hash(path)))
    return digest.hexdigest()


BUILD_HASH = _code_hash()


def asset_version(name):
    if not isinstance(name, str) or Path(name).name != name:
        raise ValueError('Invalid resource name')
    path = APP / 'static' / name
    return _hash(path)[:16] if path.is_file() else 'missing'


def manifest():
    return {'build_hash': BUILD_HASH, 'resources': {
        name: asset_version(name) for name in ('commerce.js', 'product-archive.js', 'import-center.js',
                                              'simple-design.js', 'detail-page.js')}}
