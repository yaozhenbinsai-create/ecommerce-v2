"""Customer messaging channels, independent of the shared support workflow.

Adapters report actual inbound and free-text delivery capabilities. Amazon's
order-action Messaging API is not an inbox or a general conversation API.
Real mailbox/helpdesk adapters can be registered here without changing cases,
regional language selection, AI policies, or the human work queue.
"""
from typing import Protocol


class ChannelError(Exception):
    def __init__(self, message: str, unknown: bool = False):
        super().__init__(message)
        self.unknown = unknown


class SupportChannel(Protocol):
    def capabilities(self, target: dict) -> dict: ...
    def fetch(self, target: dict, cursor: str | None) -> dict: ...
    def send(self, target: dict, thread: dict, text: str, request_id: str) -> dict: ...
    def reconcile(self, target: dict, thread: dict, request_id: str) -> dict | None: ...


# Registration is application code, never an untrusted URL submitted by a user
# or by the language model. Channel credentials belong to their own adapters.
ADAPTERS: dict[str, SupportChannel] = {}


class UnconnectedChannel:
    def __init__(self, platform: str):
        self.platform = platform

    def capabilities(self, target: dict) -> dict:
        amazon = self.platform in {'亚马逊', 'Amazon', 'amazon'}
        reason = (
            '亚马逊公开Messaging API没有完整买家收件箱读取接口，当前无法通过官方API自动收录买家咨询；'
            '卖家支持Case使用独立的官方Support API，需销售伙伴支持权限。回复功能后续接入，当前请在亚马逊后台处理。'
            if amazon else
            '此平台的客服收发通道尚未配置；可先录入客户消息并处理人工工单，实际回复请到所属平台完成。'
        )
        return {
            'platform': self.platform,
            'receive': False,
            'send': False,
            'reconcile': False,
            'reason': reason,
        }

    def fetch(self, target: dict, cursor: str | None = None) -> dict:
        raise ChannelError(self.capabilities(target)['reason'])

    def send(self, target: dict, thread: dict, text: str, request_id: str) -> dict:
        raise ChannelError(self.capabilities(target)['reason'])

    def reconcile(self, target: dict, thread: dict, request_id: str) -> dict | None:
        raise ChannelError('当前客服通道不支持发送结果核对，请在所属平台检查实际消息记录')


def resolve(target: dict) -> SupportChannel:
    from . import store

    # Never route an old conversation snapshot through a newly configured account.
    with store.main().db() as conn:
        account = conn.execute('SELECT platform,seller_id,enabled FROM platform_accounts WHERE id=?', (target['account_id'],)).fetchone()
        if not account:
            raise ChannelError('客服所属账号已不存在，请人工核实')
        if not account['enabled'] or target.get('enabled') is False:
            raise ChannelError('客服所属账号或地区已停用')
        if any(key in target and target[key] != account[key] for key in ('platform', 'seller_id')):
            raise ChannelError('客服所属平台或卖家身份已变化，请人工核实')
        if target.get('id'):
            saved = store.get(conn, 'target', target['id'])
            if any(target.get(key) != saved.get(key) for key in ('account_id', 'seller_id', 'marketplace_id', 'environment', 'enabled')):
                raise ChannelError('客服所属账号地区配置已变化，请刷新并人工核实')
    platform = account['platform']
    return ADAPTERS.get(platform) or UnconnectedChannel(platform)
