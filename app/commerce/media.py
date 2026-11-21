from __future__ import annotations
import hashlib
import io
import os
from pathlib import Path
from urllib.parse import urlparse
from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError
from . import store as s

MAX_BYTES=20*1024*1024
MAX_PIXELS=40_000_000


def root():
    p=s.main().DATA_ROOT/'commerce_media'
    p.mkdir(parents=True,exist_ok=True)
    return p


def ingest(raw,filename='',sku_id=None, *, source_path=None):
    if not raw or len(raw)>MAX_BYTES: raise HTTPException(422,'图片为空或超过20MiB')
    try:
        with Image.open(io.BytesIO(raw)) as im:
            if im.format not in {'JPEG','PNG','WEBP'} or im.width*im.height>MAX_PIXELS:
                raise HTTPException(422,'仅支持JPEG/PNG/WebP，图片不得超过4000万像素')
            im.verify()
        with Image.open(io.BytesIO(raw)) as im:
            im=ImageOps.exif_transpose(im).convert('RGB')
            width,height=im.size
            out=io.BytesIO();im.save(out,'JPEG',quality=95)
            content=out.getvalue()
    except (UnidentifiedImageError,OSError,ValueError,Image.DecompressionBombError):
        raise HTTPException(422,'图片无法解码')
    digest=hashlib.sha256(content).hexdigest();key=digest+'.jpg';p=root()/key
    original_digest=hashlib.sha256(raw).hexdigest()
    originals=s.main().DATA_ROOT/'commerce_originals';originals.mkdir(parents=True,exist_ok=True)
    original=originals/original_digest
    if not original.exists():
        import uuid
        temp=originals/(original_digest+'.'+uuid.uuid4().hex+'.tmp')
        try:
            linked=False
            if source_path is not None:
                source=Path(source_path)
                boundary=(s.main().DATA_ROOT/'design_outputs').resolve()
                if source.is_file() and not source.is_symlink() and source.resolve().is_relative_to(boundary):
                    with source.open('rb') as handle:matches=hashlib.file_digest(handle,'sha256').hexdigest()==original_digest
                    if matches:
                        try:os.link(source,temp);linked=True
                        except OSError:pass  # Cross-volume or unsupported filesystem: retain original bytes.
            if not linked:temp.write_bytes(raw)
            os.replace(temp,original)
        finally:
            temp.unlink(missing_ok=True)
    if not p.exists():
        import uuid
        temp=root()/(digest+'.'+uuid.uuid4().hex+'.tmp')
        temp.write_bytes(content);os.replace(temp,p)
    with s.transaction() as c:
        if sku_id is not None:s.get(c,'sku',sku_id)
        existing=s.by_key(c,'media',digest)
        if existing:
            if existing.get('status')=='archived':return s.save(c,'media',existing|{'status':'saved'})
            return existing
        return s.put(c,'media',digest,{'key':key,'sha256':digest,'width':width,'height':height,'bytes':len(content),
          'original_sha256':original_digest,'filename':Path(filename.replace('\\','/')).name[:160],'sku_id':sku_id,'status':'saved','url':'/api/commerce/media/file/'+key})


def path(key):
    import re
    if not re.fullmatch(r'[a-f0-9]{64}\.jpg',key):raise HTTPException(404)
    p=root()/key
    if not p.is_file() or p.is_symlink() or p.resolve().parent != root().resolve():raise HTTPException(404)
    return p


def locator(asset,required=True):
    from app.security_runtime import public_media_base
    with s.main().db() as c: config=s.by_key(c,'settings','global') or {}
    base=public_media_base() or os.environ.get('MEDIA_PUBLIC_BASE_URL') or config.get('media_public_base_url','')
    parsed=urlparse(base)
    import ipaddress
    valid=parsed.scheme=='https' and bool(parsed.hostname) and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
    if parsed.hostname:
        try: valid=valid and ipaddress.ip_address(parsed.hostname).is_global
        except ValueError: valid=valid and parsed.hostname not in {'localhost'} and '.' in parsed.hostname
    if not valid:
        if required:raise HTTPException(422,'尚未配置公网HTTPS图片地址；本地图片已保存')
        return ''
    return base.rstrip('/')+'/'+asset['key']


def verify_public(asset, url=None, **context):
    """Anonymous HTTPS GET with certificate, DNS, MIME, length and hash checks."""
    import httpx
    import time
    from .network import public_stream
    from app.error_log import record
    started = time.monotonic()
    status = None
    try:
        url = url or locator(asset)
        with public_stream('GET', url, timeout=30) as response:
            status = response.status_code
            if status != 200 or response.headers.get('content-type', '').split(';')[0].strip().lower() != 'image/jpeg':
                raise HTTPException(422, f'商品图片公网地址未返回JPEG图片（HTTP {status}）；请检查域名、443安全组及是否被登录页拦截')
            digest = hashlib.sha256()
            length = 0
            for chunk in response.iter_bytes():
                length += len(chunk)
                if length > MAX_BYTES:
                    raise HTTPException(422, '公网图片超过20MiB')
                digest.update(chunk)
            if not length or digest.hexdigest() != asset['sha256']:
                raise HTTPException(422, '公网图片内容与上架图片不一致，请核对图片地址')
        return url
    except (httpx.RequestError, HTTPException, OSError) as exc:
        record('listing.image_download_failed', exc, asset_id=asset.get('id'), image_key=asset['key'],
               image_url=url, http_status=status, elapsed_ms=round((time.monotonic()-started)*1000), **context)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(422, '无法通过公网HTTPS下载商品图片，请检查DNS、证书和服务器443端口；详情已记录到错误日志') from exc


def verify_submission(bodies, **context):
    # Verify exactly the URLs in the outgoing payload before any platform write.
    checked = set()
    for sku, body in bodies.items():
        for name, values in body.get('attributes', {}).items():
            if not (name == 'main_product_image_locator' or name.startswith('other_product_image_locator_')):
                continue
            for value in values:
                url = value.get('media_location', '')
                if url in checked:
                    continue
                key = urlparse(url).path.rsplit('/', 1)[-1]
                with s.main().db() as c:
                    asset = s.by_key(c, 'media', key.removesuffix('.jpg'))
                if not asset or url != locator(asset):
                    raise HTTPException(422, '提交图片不是当前系统的有效公网商品图片地址')
                verify_public(asset, url, seller_sku=sku, **context)
                checked.add(url)
