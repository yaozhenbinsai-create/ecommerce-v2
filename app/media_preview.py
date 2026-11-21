"""Small private browser previews; original source and model inputs stay intact."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from fastapi import HTTPException, Request, Response
from fastapi.responses import FileResponse
from PIL import Image, ImageOps, UnidentifiedImageError

WIDTHS = {320, 640, 960, 1280}
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.avif', '.bmp', '.tif', '.tiff'}
_LOCKS = tuple(threading.Lock() for _ in range(16))
_ENCODE_BUDGET = threading.BoundedSemaphore(2)
_MAX_PIXELS = 40_000_000
_CACHE_LIMIT = 256 * 1024 * 1024
_CACHE_LOCK = threading.Lock()
_LAST_TRIM = {}


def _roots():
    from app import main
    return main.DATA_ROOT, {'uploads': main.UPLOAD_ROOT, 'design-outputs': main.DESIGN_OUTPUT_ROOT,
                            'commerce-media': main.DATA_ROOT / 'commerce_media'}


def _source(kind: str, path: str):
    data_root, roots = _roots()
    # Backslashes, drive names, UNC paths and parent components are rejected on
    # every platform, including when a reverse proxy has decoded the URL.
    relative = Path(path)
    if (kind not in roots or not path or '\\' in path or ':' in path or '\x00' in path
            or relative.is_absolute() or '..' in relative.parts):
        raise HTTPException(404, '图片不存在')
    if kind == 'commerce-media' and not re.fullmatch(r'[a-f0-9]{64}\.jpg', path):
        raise HTTPException(404, '图片不存在')
    root = Path(roots[kind]).resolve()
    source = (root / relative).resolve()
    if not source.is_relative_to(root) or source.suffix.lower() not in IMAGE_SUFFIXES or not source.is_file():
        raise HTTPException(404, '图片不存在')
    return Path(data_root), source


def _key(source: Path, width: int, height: int | None):
    info = source.stat()
    identity = f'webp-v2\0{source}\0{info.st_size}\0{info.st_mtime_ns}\0{info.st_ctime_ns}\0{width}\0{height}'
    return hashlib.sha256(identity.encode('utf-8')).hexdigest()


def _trim(cache: Path, keep: Path):
    """Bound only generated previews, never supplier or publication images."""
    now = time.monotonic()
    if now - _LAST_TRIM.get(cache, float('-inf')) < 300 or not _CACHE_LOCK.acquire(False):
        return
    try:
        _LAST_TRIM[cache] = now
        entries = []
        for path in cache.iterdir():
            if re.fullmatch(r'[a-f0-9]{64}\.webp', path.name) and not path.is_symlink():
                try:
                    info = path.stat()
                    if path.is_file():entries.append((info.st_mtime, info.st_size, path))
                except OSError:
                    continue
        total = sum(item[1] for item in entries)
        for modified, size, path in sorted(entries):
            if total <= _CACHE_LIMIT:break
            if path == keep or time.time() - modified < 60:continue
            try:
                path.unlink(); total -= size
            except OSError:
                continue  # An image currently streaming on Windows is retried later.
    except OSError:
        pass  # Cache maintenance cannot turn a usable preview into a page error.
    finally:
        _CACHE_LOCK.release()


def _preview(data_root: Path, source: Path, width: int, height: int | None = None):
    key = _key(source, width, height)
    cache = data_root.resolve() / 'image_previews'
    if cache.is_symlink():
        raise HTTPException(503, '图片预览目录不可用')
    cache.mkdir(parents=True, exist_ok=True)
    if cache.resolve() != cache:
        raise HTTPException(503, '图片预览目录不可用')
    target = cache / (key + '.webp')
    if target.is_symlink():
        raise HTTPException(503, '图片预览文件不可用')
    with _LOCKS[int(key[:2], 16) % len(_LOCKS)]:
        if not target.is_file():
            with _ENCODE_BUDGET:
                try:
                    with Image.open(source) as original:
                        if original.width * original.height > _MAX_PIXELS:
                            raise HTTPException(413, '图片尺寸过大，请打开原图查看')
                        preview = ImageOps.exif_transpose(original)
                        limit_height = height or max(1, round(preview.height * width / preview.width))
                        preview.thumbnail((width, limit_height), Image.Resampling.LANCZOS)
                        preview = preview.convert('RGBA' if 'A' in preview.getbands() or 'transparency' in preview.info else 'RGB')
                        handle, temporary = tempfile.mkstemp(prefix=key + '-', suffix='.tmp', dir=cache)
                        try:
                            with os.fdopen(handle, 'wb') as output:
                                preview.save(output, format='WEBP', quality=80, method=4)
                            os.replace(temporary, target)
                        finally:
                            if os.path.exists(temporary):
                                os.unlink(temporary)
                except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
                    raise HTTPException(422, '无法为此图片创建预览，请打开原图查看') from None
    _trim(cache, target)
    return target, '"' + key + '"'


def install(app):
    # The normal access middleware authenticates and authorizes both GET and HEAD
    # before this handler runs. This prefix is never a public-media exception.
    @app.get('/image-previews/{kind}/{path:path}')
    @app.head('/image-previews/{kind}/{path:path}')
    def image_preview(request: Request, kind: str, path: str, width: int = 640, height: int | None = None):
        if width not in WIDTHS or height is not None and height not in WIDTHS:
            raise HTTPException(400, '预览宽度支持 320、640、960 或 1280')
        data_root, source = _source(kind, path)
        etag = '"' + _key(source, width, height) + '"'
        headers = {'Cache-Control': 'private, no-cache', 'ETag': etag, 'X-Content-Type-Options': 'nosniff'}
        validators = (part.strip().removeprefix('W/') for part in request.headers.get('if-none-match', '').split(','))
        if any(value in {'*', etag} for value in validators):
            return Response(status_code=304, headers=headers)
        target, _ = _preview(data_root, source, width, height)
        return FileResponse(target, media_type='image/webp', headers=headers)
