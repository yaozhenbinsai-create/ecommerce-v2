"""Keep Amazon customer/order text out of unreviewed external AI providers."""
from app import security_audit

NON_AMAZON_PLATFORMS = {'自营商城'}


def amazon_context(context):
    identity = context.get('identity')
    # Missing source is not permission to send customer messages elsewhere.
    if not isinstance(identity, dict):
        return True
    platform = identity.get('platform')
    return not isinstance(platform, str) or platform.strip() not in NON_AMAZON_PLATFORMS


def local_support_decision(context):
    if not amazon_context(context):
        return None
    security_audit.emit('amazon_support_ai_egress', 'blocked')
    return {'action': 'escalate', 'reply': '',
            'reason': '亚马逊客户数据未授权发送给外部AI服务，已转人工处理',
            'confidence': 1, 'knowledge_ids': [], 'language': context['language']}
