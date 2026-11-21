"""Capabilities and normalized aftersales, scoped to the configured shop/region."""
from fastapi import HTTPException
from . import store as s


class AmazonAftersales:
    message='已接通退货报表读取。退货审批、退款和补发提交尚未接通，请在平台后台办理后刷新核对；不会用本地保存替代平台完成。'

    def actions(self,case):return []


ADAPTERS={'亚马逊':AmazonAftersales()}


def resolve(target,case=None):
    with s.main().db() as c:
        account=c.execute('SELECT platform,enabled FROM platform_accounts WHERE id=?',(target['account_id'],)).fetchone()
    if not account or not account['enabled'] or not target.get('enabled'):raise HTTPException(422,'平台账号或地区已停用')
    if case and (case.get('platform')!=account['platform'] or any(case.get(k)!=target.get(k) for k in ['account_id','marketplace_id','environment'])):
        raise HTTPException(409,'售后所属平台、账号或地区与当前配置不一致，请先核对配置')
    from .environment import require
    require(target['environment'])
    if target['environment']!='production':raise HTTPException(422,'官方静态沙盒不提供真实售后，请选择正式店铺或本地模拟')
    if account['platform'] not in ADAPTERS:raise HTTPException(422,account['platform']+'尚未接入售后接口')
    return ADAPTERS[account['platform']]
