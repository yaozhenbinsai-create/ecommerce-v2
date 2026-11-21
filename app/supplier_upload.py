"""Authenticated, resumable transport for supplier workbooks over the tunnel."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
from pathlib import Path

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

CHUNK_BYTES = 384 * 1024
LOCK = threading.RLock()


class Start(BaseModel):
    upload_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    supplier_name: str = Field(min_length=1, max_length=160)
    filename: str = Field(min_length=1, max_length=255)
    default_brand: str = Field(default='', max_length=120)
    default_origin_country: str = Field(default='', max_length=120)
    default_origin_place: str = Field(default='', max_length=160)
    size: int = Field(gt=0, le=30 * 1024 * 1024)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class Chunk(BaseModel):
    content_base64: str = Field(min_length=1, max_length=CHUNK_BYTES * 4 // 3)


def install(app, main):
    def owner(request):
        if getattr(request.state, 'commerce_role', None) != 'admin':
            raise HTTPException(403, '导入供应商资料需要管理员账号')
        return getattr(request.state, 'commerce_username', None) or 'local-admin'

    def session_path(key):
        if not re.fullmatch(r'[a-f0-9]{32}', key):
            raise HTTPException(422, '上传标识无效')
        return main.DATA_ROOT / 'supplier_upload_sessions' / key

    def load(key, request):
        path = session_path(key)
        try:
            state = json.loads((path / 'session.json').read_text(encoding='utf-8'))
        except FileNotFoundError:
            raise HTTPException(404, '上传记录不存在') from None
        if state['owner'] != owner(request):
            raise HTTPException(403, '不能读取其他账号的上传记录')
        return path, state

    @app.post('/api/supplier-imports/upload-session', status_code=201)
    def start(payload: Start, request: Request):
        filename = Path(payload.filename.replace('\\', '/')).name
        if (filename != payload.filename or re.search(r'[<>:"/\\|?*\x00-\x1f]', filename)
                or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', filename)
                or Path(filename).suffix.lower() not in {'.xlsx', '.xlsm', '.csv', '.tsv'}):
            raise HTTPException(422, '文件名或格式无效')
        state = dict(payload.model_dump(), owner=owner(request))
        path = session_path(payload.upload_id)
        with LOCK:
            if path.exists():
                _, existing = load(payload.upload_id, request)
                if {k: existing[k] for k in state} != state:
                    raise HTTPException(409, '上传记录与当前文件不一致')
            else:
                path.mkdir(parents=True)
                (path / 'session.json').write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
            return {'upload_id': payload.upload_id, 'chunk_bytes': CHUNK_BYTES}

    @app.put('/api/supplier-imports/upload-session/{key}/{index}')
    def chunk(key: str, index: int, payload: Chunk, request: Request):
        with LOCK:
            path, state = load(key, request)
            count = (state['size'] + CHUNK_BYTES - 1) // CHUNK_BYTES
            if not 0 <= index < count:
                raise HTTPException(422, '文件分段序号无效')
            try:
                raw = base64.b64decode(payload.content_base64, validate=True)
            except ValueError:
                raise HTTPException(422, '文件分段内容无效') from None
            if len(raw) != min(CHUNK_BYTES, state['size'] - index * CHUNK_BYTES):
                raise HTTPException(422, '文件分段长度不正确')
            destination = path / f'{index}.part'
            if destination.exists():
                if destination.read_bytes() != raw:
                    raise HTTPException(409, '已收到的文件分段内容不同，请重新选择文件')
            else:
                temporary = path / f'{index}.pending'
                temporary.write_bytes(raw)
                temporary.replace(destination)
            return {'index': index, 'bytes': len(raw)}

    @app.post('/api/supplier-imports/upload-session/{key}/finish')
    def finish(key: str, request: Request):
        with LOCK:
            path, state = load(key, request)
            # A lost response or restart must recover the same saved batch.
            with main.db() as c:
                existing = c.execute("SELECT id FROM supplier_imports WHERE json_extract(workbook_snapshot,'$.upload_session_key')=?", (key,)).fetchone()
            if existing:
                return main.get_one('supplier_imports', existing['id'])
            count = (state['size'] + CHUNK_BYTES - 1) // CHUNK_BYTES
            try:
                raw = b''.join((path / f'{i}.part').read_bytes() for i in range(count))
            except FileNotFoundError:
                raise HTTPException(422, '文件尚未传输完整，请继续上传') from None
            if len(raw) != state['size'] or hashlib.sha256(raw).hexdigest() != state['sha256']:
                raise HTTPException(422, '文件完整性校验失败，不能导入')
            fields = {k: state[k] for k in ['supplier_name', 'filename', 'default_brand',
                      'default_origin_country', 'default_origin_place']}
            payload = main.SupplierImportUpload(**fields, content_base64=base64.b64encode(raw).decode())
            return main.save_supplier_upload(payload, upload_session_key=key)
