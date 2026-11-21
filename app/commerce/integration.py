"""Read-only, secret-free account and marketplace onboarding diagnostics."""
from urllib.parse import urlparse
from fastapi import HTTPException
from . import store as s, amazon, locales, environment, support_channels


def inspect(ident):
    from .targets import MARKETPLACES
    with s.main().db() as c:
        target = s.get(c, 'target', ident)
        account = c.execute('SELECT * FROM platform_accounts WHERE id=?', (target['account_id'],)).fetchone()
        settings = s.by_key(c, 'settings', 'global') or {}
    checks = []
    def add(key, title, ok, message, scope='connection'):
        checks.append(dict(key=key, title=title, status='configured' if ok else 'missing', message=message, scope=scope))
    platform = account['platform'] if account else ''
    add('adapter', '平台适配器', platform == '亚马逊', '已接入 Amazon SP-API' if platform == '亚马逊' else '该平台尚未开发接口适配器')
    add('environment', '真实店铺环境', target['environment'] == 'production', '使用真实卖家接口；旧测试目标需另建正式关联')
    add('enabled', '账号与地区启用', bool(account and account['enabled'] and target.get('enabled')), '在平台同步中启用账号和地区')
    add('seller_id', 'Seller ID', bool(account and account['seller_id'].strip() and target.get('seller_id') == account['seller_id']), '填写卖家 ID；修改后重新保存账号地区关联')
    market = next((m for m in MARKETPLACES if m['marketplace_id'] == target['marketplace_id']), None)
    add('marketplace', 'Marketplace ID / 接口区域', bool(market and market['region'] == target.get('region')), '选择店铺已开通的国家地区，系统按 NA / EU / FE 选择接口')
    if platform == '亚马逊':
        status = amazon.credential_status(target['account_id'])
        for key, title in [('client_id', 'LWA Client ID'), ('client_secret', 'LWA Client Secret'), ('refresh_token', '店铺授权 Refresh Token')]:
            add(key, title, status['fields'][key], f"来源：{status['source']}。" + (status['error'] or '在配置授权中填写；空白保留原值。'))
        rotation = status.get('rotation', {})
        add('credential_rotation', '应用密钥轮换', rotation.get('status') == 'scheduled', rotation.get('message','请核对轮换日期'), 'maintenance')
    try:
        language = locales.resolve(target)
        add('language', '资料语言', True, locales.LANGUAGES[language], 'listing')
    except HTTPException:
        add('language', '资料语言', False, '选择地区默认语言或指定输出语言', 'listing')
    from app.security_runtime import public_media_base
    base = urlparse(public_media_base() or settings.get('media_public_base_url', ''))
    add('media', '公网商品图片地址', base.scheme == 'https' and bool(base.hostname), '经营设置中填写稳定的 HTTPS 图片根地址；还需确认平台可以访问', 'listing')
    add('live_writes', '平台写入开关', bool(settings.get('live_writes_enabled')), '只读联调可保持关闭；实际提交商品与物流前启用', 'write')
    from . import customer_pii
    add('recipient_access', '线下发货收件信息', customer_pii.enabled() and bool(target.get('include_recipient')),
        '需Direct to Consumer Shipping受限角色，部署启用后再开启此店铺的收件信息。地址仅限授权人员在线查看，不进入普通导出。', 'orders')
    try:
        support = support_channels.resolve(target).capabilities(target)
    except (support_channels.ChannelError, HTTPException):
        support = {'receive': False, 'send': False, 'reason': '客服所属账号地区已停用或身份发生变化，请核实配置'}
    support_message = support.get('reason') or '客服通道已配置，实际收发仍需使用对应店铺验证'
    add('support_channel', '客服收发通道', bool(support.get('receive') and support.get('send')), support_message, 'support')
    return {'target_id':ident, 'account_name':account['account_name'] if account else '', 'platform':platform,
            'environment':target['environment'], 'endpoint':('https://' + amazon.REGIONS.get(target.get('region'), '')) if platform == '亚马逊' and target['environment'] == 'production' else '',
            'checks':checks, 'connection_configured':all(v['status'] == 'configured' for v in checks if v['scope'] == 'connection'),
            'verified':bool(target.get('verified')) and target['environment'] == 'production',
            'last_verified_at':target.get('last_sync_at'), 'runtime_available':environment.allowed(target['environment']),
            'capabilities':({'listing':'已实现，需商品刊登权限与类目资料', 'orders':'已实现待发货订单读取和物流提交，需对应权限',
                            'detail':'已实现 Amazon A+，需账号和 ASIN 具备 A+ 资格', 'aftersales':'仅接入退货报表读取；退款、退货审批、补发接口尚未接入'} if platform == '亚马逊' else {}) | {'support': support_message},
            'message':'此检查仅核对配置是否齐备，不调用平台，不表示授权、商品资格或图片公网连通已验证。'}
