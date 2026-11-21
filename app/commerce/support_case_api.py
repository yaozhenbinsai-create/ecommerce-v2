"""Read-only official Support API client; separate from buyer messaging/browser login.

Contract: Amazon Support v2025-02-01. Case collection scope is the seller
account, not the selected marketplace. No private/internal model fields,
browser credentials, permission grants, sends or case mutations are used.
"""
import re
from datetime import datetime
from urllib.parse import quote

from . import amazon
from .support_channels import ChannelError

BASE = '/support/2025-02-01/cases'
ROLE = 'Selling Partner Support (Restricted)'
DOCUMENTATION = 'https://developer-docs.amazon/sp-api/docs/support-api'


class CaseApiError(ChannelError):
    def __init__(self, message, *, status=0, request_id='', codes=None, retry_after=0):
        super().__init__(message)
        self.status = status
        self.request_id = request_id
        self.codes = list(codes or [])
        self.retry_after = retry_after


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip() or value in {'.', '..'} or len(value) > 300 or any(ord(c) < 32 for c in value):
        raise CaseApiError(name + '格式无效；未读取其他工单')
    return value


def _pagination(next_token, max_results, sort_order):
    if type(max_results) is not int or not 1 <= max_results <= 50 or not isinstance(sort_order, str) or sort_order not in {'ASC', 'DESC'}:
        raise CaseApiError('Case API 分页参数无效')
    values = {'maxResults': max_results, 'sortOrder': sort_order}
    if next_token is not None:
        if not isinstance(next_token, str) or not next_token or len(next_token) > 10000:
            raise CaseApiError('Case API 同步位置无效；原位置保留')
        values['nextToken'] = next_token
    return values


def _timestamp(value):
    try:
        if not isinstance(value, str) or not value or len(value) > 100 or datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is None:
            raise ValueError()
    except ValueError:
        raise CaseApiError('Case API 原始时间无法确认；未猜测时区或推进同步位置') from None


def _read(target, method, path, **kwargs):
    if not (method == 'POST' and path == BASE or
            method == 'GET' and re.fullmatch(re.escape(BASE) + r'/[^/?#]+(?:/contacts)?', path)):
        raise CaseApiError('Case API 连接仅允许官方列表、详情和沟通记录读取')
    if target.get('environment') != 'production' or target.get('enabled') is not True:
        raise CaseApiError('请选择已启用的正式店铺；不会借用其他账号授权')
    try:
        # listCases uses POST to retrieve a collection. It creates no case.
        response = amazon.request(target, method, path, non_mutating=True, **kwargs)
    except amazon.PlatformError as exc:
        message = ('Case API 未获得访问权限（HTTP403）。请核对开发者档案已批准的 ' + ROLE +
                   ' 权限、应用所选角色及卖家应用授权；不会自动扩大权限。') if exc.status == 403 else str(exc)
        raise CaseApiError(message, status=exc.status, request_id=exc.request_id,
                           codes=[row.get('code', '') for row in exc.errors], retry_after=exc.retry_after) from None
    body = response.get('body')
    if not isinstance(body, dict):
        raise CaseApiError('Case API 回执格式无效；未将本轮标记完成')
    return response


def _page(response, collection, previous_token):
    body = response['body']
    rows, token = body.get(collection), body.get('nextToken')
    if not isinstance(rows, list) or len(rows) > 50 or any(not isinstance(row, dict) for row in rows):
        raise CaseApiError('Case API 列表格式无效；原同步位置保留')
    if token is not None and (not isinstance(token, str) or not token or len(token) > 10000 or token == previous_token):
        raise CaseApiError('Case API 分页回执无法推进；原同步位置保留')
    return {collection: rows, 'next_token': token, 'has_more': token is not None,
            'request_id': response.get('request_id', ''), 'scope': 'seller_account'}


def list_cases(target, *, next_token=None, max_results=10, sort_order='DESC'):
    query = _pagination(next_token, max_results, sort_order)
    result = _page(_read(target, 'POST', BASE, body=query), 'cases', next_token)
    for row in result['cases']:
        _identifier(row.get('caseId'), 'Case 编号')
        if not all(isinstance(row.get(key), str) and row[key] for key in ('subject', 'status', 'createdDate', 'lastUpdatedDate')):
            raise CaseApiError('Case API 工单元数据不完整；未写入片段')
        for key in ('createdDate', 'lastUpdatedDate'):
            _timestamp(row[key])
    return result


def get_case(target, case_id):
    case_id = _identifier(case_id, 'Case 编号')
    response = _read(target, 'GET', BASE + '/' + quote(case_id, safe=''))
    if response['body'].get('caseId') != case_id:
        raise CaseApiError('Case API 回执编号与请求不一致；未借用其他工单')
    if not all(isinstance(response['body'].get(key), str) and response['body'][key] for key in ('subject', 'status', 'createdDate', 'lastUpdatedDate')):
        raise CaseApiError('Case API 详情元数据不完整；未将片段标记完成')
    for key in ('createdDate', 'lastUpdatedDate'):
        _timestamp(response['body'][key])
    return {'case': response['body'], 'request_id': response.get('request_id', ''), 'scope': 'seller_account'}


def list_contacts(target, case_id, *, next_token=None, max_results=10, sort_order='DESC'):
    case_id = _identifier(case_id, 'Case 编号')
    query = _pagination(next_token, max_results, sort_order)
    result = _page(_read(target, 'GET', BASE + '/' + quote(case_id, safe='') + '/contacts', params=query),
                   'contacts', next_token)
    for row in result['contacts']:
        _identifier(row.get('contactId'), '原沟通记录编号')
        if row.get('channel') not in {'EMAIL', 'PHONE', 'CHAT'} or not isinstance(row.get('createdDate'), str):
            raise CaseApiError('Case API 沟通类型或时间无法确认；原同步位置保留')
        _timestamp(row['createdDate'])
    # Keep original channel fields and EXPIRED metadata. Do not invent senders
    # for phone notes/transcripts or treat missing content as an empty message.
    return result
