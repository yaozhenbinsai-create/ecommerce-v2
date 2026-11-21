"""Real shop destinations in the application; isolated fixtures opt in in tests."""
from pathlib import Path
from fastapi import HTTPException
from . import store as s

# Patched by the regression fixture, never exposed through configuration or HTTP.
TESTING = False


def allowed(value):
    root = Path(__file__).resolve().parents[2] / 'data'
    isolated = TESTING and Path(s.main().DATA_ROOT).resolve() != root and not Path(s.main().DB_PATH).resolve().is_relative_to(root)
    return value == 'production' or (isolated and value in {'mock', 'sandbox'})


def require(value):
    if not allowed(value):
        raise HTTPException(422, '旧沙箱和模拟目标已停止执行。请在平台同步中新增真实授权账号地区关联，再重新生成资料；原记录保留查看。')


def require_production(value):
    """Never route a historical/test destination to an Amazon endpoint."""
    if value != 'production':
        raise HTTPException(422, 'Amazon接口仅使用正式店铺；旧沙盒记录保留查看，不再执行网络请求。')


def check_job(c, payload):
    """Also reject persisted jobs queued before the old environments were retired."""
    if payload.get('target_id'):
        require(s.get(c, 'target', payload['target_id'])['environment'])
    for key, kind in [('listing_id', 'listing'), ('shipment_id', 'shipment'), ('order_id', 'order'),
                      ('case_id', 'aftersale'), ('action_id', 'aftersale_action'), ('report_id', 'report'), ('publication_id', 'detail_publication')]:
        if payload.get(key):
            row = s.get(c, kind, payload[key])
            if row.get('environment'):
                require(row['environment'])
            if row.get('target_id'):
                require(s.get(c, 'target', row['target_id'])['environment'])
