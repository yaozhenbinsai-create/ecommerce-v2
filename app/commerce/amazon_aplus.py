"""Amazon A+ adapter. Content is uploaded separately from listing attributes."""
import base64
import hashlib
import io
from urllib.parse import quote,urlsplit,parse_qsl,urlunsplit
import httpx
from PIL import Image,ImageOps
from . import amazon
from .network import public_post
from app.error_log import record, clean_text

BASE='/aplus/2020-11-01'


def call(target,method,path,body=None,**params):
    # These search APIs encode the original filters in the token and require
    # pageToken to be the only query parameter on later pages.
    query=({'pageToken':params['pageToken']} if params.get('pageToken') and path in {'/contentDocuments','/contentPublishRecords'}
           else {'marketplaceId':target['marketplace_id'],**params})
    try:result=amazon.request(target,method,BASE+path,query,body)
    except amazon.PlatformError as exc:
        if path=='/contentAsinValidations':exc.unknown=False
        details='；'.join(clean_text(str(row.get('message',''))+' '+str(row.get('details',''))).strip()
                         for row in exc.errors if isinstance(row,dict))[:1400]
        if details:exc.args=(str(exc)+'：'+details,)
        record('amazon.aplus_error', target_id=target.get('id'), account_id=target.get('account_id'),
               path=path, http_status=exc.status, platform_request_id=exc.request_id, errors=exc.errors)
        raise
    if result['body'].get('errors'):
        message='；'.join(str(e.get('message') or e.get('code')) for e in result['body']['errors'])
        raise amazon.PlatformError('图文详情：'+message[:1200],422)
    return result['body']


def resource(key,suffix=''):return '/contentDocuments/'+quote(key,safe='')+suffix


def prepare_image(path):
    # Fit the entire existing section inside the standard module, without cropping.
    with Image.open(path) as original:
        if original.width*original.height>40_000_000:raise amazon.PlatformError('详情分段图片过大',422)
        fitted=ImageOps.contain(ImageOps.exif_transpose(original).convert('RGB'),(970,600),Image.Resampling.LANCZOS)
        canvas=Image.new('RGB',(970,600),'white');canvas.paste(fitted,((970-fitted.width)//2,(600-fitted.height)//2))
        output=io.BytesIO();canvas.save(output,'JPEG',quality=95)
        return output.getvalue()


def upload(target,raw):
    try:
        response=amazon.request(target,'POST','/uploads/2020-11-01/uploadDestinations/aplus/2020-11-01/contentDocuments',
            {'marketplaceIds':target['marketplace_id'],'contentMD5':base64.b64encode(hashlib.md5(raw).digest()).decode(),'contentType':'image/jpeg'})['body']
    except amazon.PlatformError as exc:
        exc.unknown=False
        raise
    dest=response.get('payload') or {};url=dest.get('url','');parts=urlsplit(url)
    if (not dest.get('uploadDestinationId') or parts.scheme != 'https' or parts.username or parts.password
            or not (parts.hostname or '').endswith(('.amazonaws.com','.amazonaws.com.cn'))):
        raise amazon.PlatformError('平台未返回有效的详情图片上传地址',422)
    fields=dict(parse_qsl(parts.query,keep_blank_values=True))
    if not any(k.lower()=='policy' for k in fields):raise amazon.PlatformError('平台详情图片上传表单缺少签名策略',422)
    headers={k:v for k,v in (dest.get('headers') or {}).items() if k.lower() not in {'authorization','cookie','host','content-type','content-length','x-amz-access-token','proxy-authorization'}}
    try:
        response=public_post(urlunsplit((parts.scheme,parts.netloc,parts.path,'','')),data=fields,
            files={'file':('detail.jpg',raw,'image/jpeg')},headers=headers,timeout=60)
    except httpx.RequestError as exc:
        record('amazon.aplus_upload_connection_failed', exc, target_id=target.get('id'), account_id=target['account_id'],
               upload_host=parts.hostname, image_bytes=len(raw))
        raise amazon.PlatformError('详情图片上传中断，可重试上传',503) from exc
    if not response.is_success:
        # Read only diagnostic S3 fields; signed forms and credential headers never enter the log.
        from xml.etree import ElementTree
        details = {}
        try:
            xml = ElementTree.fromstring(response.content[:65536])
            details = {name:xml.findtext(name) for name in ['Code','Message','RequestId']}
        except ElementTree.ParseError:
            pass
        record('amazon.aplus_upload_failed', target_id=target.get('id'), account_id=target['account_id'],
               http_status=response.status_code, upload_host=parts.hostname, image_bytes=len(raw),
               platform_request_id=response.headers.get('x-amz-request-id'), errors=details)
        raise amazon.PlatformError('详情图片上传失败（HTTP '+str(response.status_code)+'）',response.status_code)
    return dest['uploadDestinationId']


def document(name,locale,uploads,title):
    return {'name':name,'contentType':'EBC','locale':locale,'contentModuleList':[
        {'contentModuleType':'STANDARD_HEADER_IMAGE_TEXT','standardHeaderImageText':{'block':{'image':{
            'uploadDestinationId':upload_id,'altText':(title or 'Product')[:100],
            'imageCropSpecification':{'size':{'width':{'value':970,'units':'pixels'},'height':{'value':600,'units':'pixels'}}}
        }}}} for upload_id in uploads]}


def resolve_asins(target,draft):
    asins=[]
    for sku in draft['skus']:
        asin=sku.get('asin')
        if not asin:
            try:body=amazon.get_listing(target,sku['seller_sku'])['body']
            except amazon.PlatformError as exc:
                if exc.status==404:return []
                raise
            asin=next((r.get('asin') for r in body.get('summaries',[]) if r.get('marketplaceId')==target['marketplace_id']),None)
        if not asin:return []
        asins.append(asin)
    return sorted(set(asins))


def find_document(target,name):
    matches=[];token=None;seen=set()
    for _ in range(100):
        body=call(target,'GET','/contentDocuments',**({'pageToken':token} if token else {}))
        matches.extend(r['contentReferenceKey'] for r in body.get('contentMetadataRecords',[]) if r.get('contentMetadata',{}).get('name')==name)
        token=body.get('nextPageToken')
        if not token:return list(set(matches))
        if token in seen:break
        seen.add(token)
    raise amazon.PlatformError('详情文档分页异常，请稍后核对',409)


def normalized_locale(value):return str(value or '').replace('_','-').lower()


def published(target,key,asins,locale):
    for asin in asins:
        token=None;seen=set();found=False
        for _ in range(100):
            body=call(target,'GET','/contentPublishRecords',asin=asin,**({'pageToken':token} if token else {}))
            if any(r.get('contentReferenceKey')==key and r.get('asin')==asin and r.get('marketplaceId')==target['marketplace_id'] and normalized_locale(r.get('locale'))==normalized_locale(locale) for r in body.get('publishRecordList',[])):
                found=True;break
            token=body.get('nextPageToken')
            if not token:break
            if token in seen:raise amazon.PlatformError('详情发布记录分页异常',409)
            seen.add(token)
        if not found:return False
    return bool(asins)
