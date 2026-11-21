"""Opt-in public access to normalized product JPEGs, never source uploads."""
import re
from fastapi import HTTPException
from fastapi.responses import FileResponse
from app import security_runtime

PATTERN = re.compile(r'/public-media/([a-f0-9]{64}\.jpg)')


def allowed(request):
    return (request.method in {'GET', 'HEAD'} and PATTERN.fullmatch(request.url.path)
            and request.url.scheme == 'https' and bool(security_runtime.public_media_base()))


def install(app):
    @app.api_route('/public-media/{key}', methods=['GET', 'HEAD'])
    def product_image(key: str):
        from app.commerce import media
        if not security_runtime.public_media_base():
            raise HTTPException(404, '公网图片服务未启用')
        path = media.path(key)
        return FileResponse(path, media_type='image/jpeg', headers={
            'Cache-Control': 'public, max-age=86400, immutable',
            'X-Content-Type-Options': 'nosniff',
        })
