"""Evidence inventory, not a compliance certificate or a replacement for cloud checks."""
from __future__ import annotations

import os
import platform
from datetime import datetime, timezone

from app import security_runtime, security_audit


def inspect():
    from app.commerce import secure_auth
    checks = []

    def add(key, state, message):
        checks.append({'key': key, 'status': state, 'message': message})

    try:
        configured = security_runtime.load_config()
        options = security_runtime.server_options(activate=False)
        tunnel = bool(configured.get('enabled') and configured.get('tunnel_origin'))
        add('https_config', 'configured' if options.get('ssl_certfile') or tunnel else 'missing',
            'Cloudflare HTTPS隧道已配置，源站仅回环监听；仍需验证公网TLS和WAF实际行为' if tunnel else
            '证书配置可加载；仍需验证公网域名、信任链和实际TLS握手' if options.get('ssl_certfile') else '当前仅本机HTTP，不能作为公网生产部署')
        add('public_media', 'configured' if security_runtime.public_media_base() else 'missing',
            '指定商品JPEG的匿名HTTPS入口已配置，仍需从公网验证图片下载；后台继续要求登录' if security_runtime.public_media_base() else '商品公网图片未启用，上架前需配置并验证')
        add('runtime_enabled', 'configured' if configured.get('enabled') or os.environ.get('SHOP_SECURITY_MODE') == '1' else 'missing',
            '配置检查不代表云服务已经部署或重启')
    except RuntimeError:
        add('https_config', 'missing', '安全部署配置或证书不可用')
    try:
        auth = secure_auth.status()
        enforced = secure_auth.mfa_required()
        shareholder_bypass = secure_auth.shareholder_password_login_enabled()
        complete = (auth.get('configured') and enforced and not shareholder_bypass and secure_auth.PASSWORD_MIN_LENGTH >= 12
                    and auth.get('mfa_user_count') == auth.get('user_count')
                    and auth.get('password_rotation_pending_count') == 0)
        add('identity', 'configured' if complete else 'missing',
            '股东密码入口已启用，完整业务后台存在免MFA访问路径；不能申报所有账号强制MFA' if shareholder_bypass else
            f"最低密码长度{secure_auth.PASSWORD_MIN_LENGTH}；MFA已强制；{auth.get('mfa_user_count',0)}/{auth.get('user_count',0)}个启用账号已绑定；{auth.get('password_rotation_pending_count',0)}个账号待改密。未绑定账号只能完成绑定，不能进入业务页面。" if enforced else
            '当前仅账号密码；需启用MFA并由每位员工完成绑定')
    except Exception:
        add('identity', 'missing', '安全账号存储未就绪或无法解密')
    try:
        from app.security_secrets import inspect_file
        for key, name in [('amazon_secrets', 'commerce_credentials.json'), ('model_secrets', 'model-credentials.enc')]:
            info = inspect_file(security_audit.root() / name)
            envelope = info.get('encrypted') and info.get('configured') and not info.get('error')
            add(key, 'envelope_present_unverified' if envelope else 'missing',
                '加密封装存在；尚未验证解密、备份和云密钥保护' if envelope else '凭据未配置、未加密或无法验证；部署前检查迁移结果')
    except (ImportError, OSError, ValueError):
        add('secret_storage', 'unknown', '凭据加密状态尚未验证')
    add('amazon_ai_egress', 'implemented', '亚马逊及未知来源的客服消息默认转人工，不调用外部文本模型')
    add('security_audit', 'implemented', '安全事件按日保存；集中告警、异地不可篡改副本及日志审查需配置')
    from app.commerce import customer_pii
    add('retention', 'implemented', '收件信息独立加密；发货确认/取消即清理，最长首次采集后30天，重复同步不延长；每分钟清理，过期拒绝读取；迁移包排除地址缓存。历史备份、磁盘与外部副本仍需单独验收。')
    add('pii_access', 'configured' if customer_pii.enabled() else 'missing',
        '收件信息技术开关已启用；仍需核实Amazon受限角色及每个店铺实际返回权限' if customer_pii.enabled() else
        '收件信息暂未开放。获批Direct to Consumer Shipping并完成MFA、磁盘加密及清理验证后，再由部署配置启用。')
    for key, msg in [
        ('cloud_network', '核验云防火墙、安全组、WAF、IDS/IPS、恶意软件防护与网络隔离'),
        ('storage_encryption', '核验数据库、上传文件、所有数据盘、快照和备份的静态加密'),
        ('incident_response', '指定责任人、批准预案、落实24小时通报、半年审查和演练'),
        ('cloud_endpoint', '验证公网HTTPS、证书续期、扫描与修复、非必要端口关闭'),
        ('staff_devices', '核验员工终端加密、15分钟锁屏、防护与离职撤权'),
    ]:
        add(key, 'external_evidence_required', msg)
    return {'checked_at': datetime.now(timezone.utc).isoformat(), 'host_platform': platform.system(),
            'scope': '运行此检查的主机；不是远程云主机或整个组织的审计',
            'amazon_approval': '未验证；本报告不表示亚马逊审核通过', 'ready_for_submission': False, 'checks': checks}
