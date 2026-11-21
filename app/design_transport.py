"""Single-attempt multimodal transport. Scheduling owns all retries."""
import base64
import hashlib
import io
import json
import mimetypes
import os
import time
from pathlib import Path
import httpx
from PIL import Image,ImageOps
from app.model_retry import ModelFailure
from app.codex_bridge import CodexImagePending, CodexImageContractError
from app import image_receipts, design_spec
from urllib.parse import urlparse


def secure_endpoint(endpoint):
    if os.environ.get('SHOP_SECURITY_MODE') == '1' or os.environ.get('SHOP_PUBLIC_MODE') == '1':
        parsed = urlparse(endpoint)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ModelFailure('安全部署要求模型和图片传输使用有效的 HTTPS 地址', 'local')
    return endpoint


def candidates(main, image=False):
    with main.db() as c:
        kinds=('图片生成','图片精修') if image else ('商品文案','通用')
        plan=c.execute('SELECT model_ids FROM plans WHERE enabled=1 AND plan_type IN (?,?) ORDER BY CASE WHEN plan_type=? THEN 0 ELSE 1 END,updated_at DESC,id DESC LIMIT 1',(*kinds,kinds[0])).fetchone()
        rows={r['id']:dict(r) for r in c.execute('SELECT * FROM models WHERE enabled=1 AND capability=? ORDER BY id',('图片' if image else '文本',))}
    import json
    ids=json.loads(plan['model_ids']) if plan else list(rows)
    result=[rows[i] for i in dict.fromkeys(ids) if i in rows and main.model_is_configured(rows[i])]
    if not result: raise ModelFailure('请配置可用的'+('参考图生成模型' if image else '图文理解模型'),'local')
    return result


def encoded_image(path):
    with Image.open(path) as im:
        # Small supplier JPEGs already contain limited detail. Preserve their
        # actual bytes instead of introducing another lossy compression pass.
        if im.format=='JPEG' and im.mode=='RGB' and max(im.size)<=1400 and im.getexif().get(274,1)==1:
            return base64.b64encode(Path(path).read_bytes()).decode()
        rgba=ImageOps.exif_transpose(im).convert('RGBA')
        # Vision sees transparent product photos on a neutral white surface,
        # never the arbitrary hidden RGB left by removing their alpha channel.
        background=Image.new('RGBA',rgba.size,'white')
        background.alpha_composite(rgba)
        im=background.convert('RGB');im.thumbnail((1400,1400))
        buf=io.BytesIO();im.save(buf,'JPEG',quality=88)
    return base64.b64encode(buf.getvalue()).decode()


def check(response):
    if response.is_success: return
    kind='transient' if response.status_code in {408,425,429} or response.status_code>=500 else 'invalid'
    try: delay=float(response.headers.get('Retry-After','0'))
    except ValueError: delay=0
    # Do not include server HTML or credentials in saved error messages.
    raise ModelFailure(f'模型服务HTTP {response.status_code}',kind,delay)


def read_chat_stream(response):
    """Require an explicit successful finish; a broken stream is never a plan."""
    parts=[];finished=False;started=time.monotonic()
    for line in response.iter_lines():
        if time.monotonic()-started>300:raise ModelFailure('文本流超过300秒，连接已结束')
        if not line.startswith('data:'):continue
        data=line[5:].strip()
        if data=='[DONE]':break
        try:event=json.loads(data)
        except ValueError as exc:raise ModelFailure('文本流事件格式无效','invalid') from exc
        if event.get('error'):raise ModelFailure('文本流中上游返回错误')
        for choice in event.get('choices',[]):
            if choice.get('index',0)!=0:continue
            delta=choice.get('delta',{}).get('content')
            if isinstance(delta,str):parts.append(delta)
            reason=choice.get('finish_reason')
            if reason=='stop':finished=True
            elif reason:raise ModelFailure('文本流未正常完成：'+str(reason),'invalid')
    if not finished:raise ModelFailure('文本流在完成前断开')
    result=''.join(parts)
    if not result.strip():raise ModelFailure('文本流没有正文','invalid')
    return result


def text_output_budget(prompt):
    # One current planning call is scoped to one SKU and its eight plans.
    # Historical multi-product/segmented generators no longer have request paths.
    return 10000 if any('协议：'+version in prompt for version in ('three-stage-eight-plans-v1','three-stage-eight-plans-v2')) else 6000


def text(main,model,prompt,paths):
    if main.is_codex_model(model):
        try: return main.get_codex_bridge(main.ROOT).run_text(prompt,model['model_identifier'],images=paths,timeout=600)
        except main.CodexBridgeError as e: raise ModelFailure(str(e),'transient') from e
    key=main.read_credentials().get(str(model['id'])) or main.model_environment_key(model['api_key_env'])
    headers={'Authorization':'Bearer '+key};base=model['base_url'].rstrip('/');protocol=model['protocol']
    enc=[encoded_image(p) for p in paths]
    output_budget=text_output_budget(prompt)
    if protocol=='anthropic':
        endpoint=base+'/messages';headers={'x-api-key':key,'anthropic-version':'2023-06-01'}
        body={'model':model['model_identifier'],'max_tokens':output_budget,'messages':[{'role':'user','content':[{'type':'text','text':prompt}]+[{'type':'image','source':{'type':'base64','media_type':'image/jpeg','data':s}} for s in enc]}]}
    elif protocol=='responses':
        endpoint=base+'/responses';body={'model':model['model_identifier'],'max_output_tokens':output_budget,'input':[{'role':'user','content':[{'type':'input_text','text':prompt}]+[{'type':'input_image','image_url':'data:image/jpeg;base64,'+s,'detail':'high'} for s in enc]}]}
    else:
        endpoint=base+'/chat/completions';body={'model':model['model_identifier'],'max_tokens':output_budget,'messages':[{'role':'user','content':[{'type':'text','text':prompt}]+[{'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+s,'detail':'high'}} for s in enc]}]}
    secure_endpoint(endpoint)
    try:
        if protocol not in {'anthropic','responses'}:
            # Receive output as it is produced instead of waiting for a buffered
            # whole response behind gateways with a roughly 60-second deadline.
            body['stream']=True
            with httpx.stream('POST',endpoint,trust_env=False,headers=headers,json=body,timeout=300) as r:
                check(r)
                if 'text/event-stream' in r.headers.get('content-type',''):
                    return read_chat_stream(r)
                # Some compatible providers ignore stream and return normal JSON.
                r.read();payload=r.json()
        else:
            r=httpx.post(endpoint,trust_env=False,headers=headers,json=body,timeout=300);check(r);payload=r.json()
        if payload.get('status')=='incomplete' or payload.get('stop_reason')=='max_tokens' or any(x.get('finish_reason')=='length' for x in payload.get('choices',[])):
            raise ModelFailure('文本响应被截断','invalid')
        return main.extract_ai_text(payload,protocol)
    except httpx.RequestError as e: raise ModelFailure('文本模型连接中断：'+type(e).__name__) from e


def retain_outbound(task, prompt, paths, checkpoint, contents=None):
    """Compare the actual adapter input bytes; no claim about provider internals."""
    packet=task.get('remote',{}).get('render_request')
    if not packet:return
    if len(paths)!=len(packet['references']):raise ModelFailure('请求附件数量与冻结记录不一致','local')
    blobs=contents if contents is not None else [p.read_bytes() for p in paths]
    references=[{'path':str(path),'sha256':hashlib.sha256(raw).hexdigest(),'role':ref['role']}
                for path,raw,ref in zip(paths,blobs,packet['references'],strict=True)]
    try:design_spec.verify_request(packet,prompt,references)
    except ValueError as exc:raise ModelFailure(str(exc),'local') from exc
    history=dict(task.get('remote',{}).get('outbound_requests') or {})
    history[str(task.get('total_attempts',0))]={'request_sha256':packet['sha256'],
        'prompt_sha256':design_spec.prompt_hash(prompt),'references':references,
        'evidence':'application_adapter_input_not_provider_internal'}
    checkpoint({'outbound_requests':history})


def generate(main,model,prompt,paths,task,checkpoint):
    if main.is_codex_model(model):
        retain_outbound(task,prompt,paths,checkpoint)
        def retain_contract_candidate(error):
            # The provider already produced/billed this image. Save its bytes
            # with the failed input evidence; never turn this into a retry.
            checkpoint({'image_input_contract':error.evidence})
            return Path(error.image_path).read_bytes()
        try:
            path=main.get_codex_bridge(main.ROOT).generate_image(prompt,model['model_identifier'],paths,timeout=420,
                remote=task['remote'].get('codex'),on_submitted=lambda meta:checkpoint({'codex':meta}))
            return Path(path).read_bytes()
        except CodexImagePending as e:raise ModelFailure(str(e),'poll',60) from e
        except CodexImageContractError as e:return retain_contract_candidate(e)
        except main.CodexBridgeError as e:
            if getattr(e,'failure_kind',None)=='invalid':
                raise ModelFailure(str(e),'invalid') from e
            if getattr(e,'interrupted',False):
                # Retrieve once after interruption: completion can race with cancellation.
                if task['remote'].get('codex'):
                    try:
                        saved=main.get_codex_bridge(main.ROOT).generate_image(prompt,model['model_identifier'],paths,remote=task['remote']['codex'])
                        return Path(saved).read_bytes()
                    except CodexImagePending as pending:raise ModelFailure(str(pending),'poll',60) from pending
                    except CodexImageContractError as contract_error:return retain_contract_candidate(contract_error)
                    except main.CodexBridgeError as query_error:
                        if not getattr(query_error,'interrupted',False):
                            raise ModelFailure('查询已提交图片任务时断链，将继续查询','poll',60) from query_error
                checkpoint({'codex_interrupted':True})
            if task['remote'].get('codex') and not task['remote'].get('codex_interrupted'):
                raise ModelFailure('上游已接单，连接中断后继续查询原任务','poll',60) from e
            checkpoint({'codex':None,'codex_interrupted':False})
            raise ModelFailure(str(e),'transient') from e
    key=main.read_credentials().get(str(model['id'])) or main.model_environment_key(model['api_key_env'])
    endpoint=model.get('image_edit_endpoint') or '/images/edits'
    if not endpoint.startswith(('http://','https://')): endpoint=model['base_url'].rstrip('/')+'/'+endpoint.lstrip('/')
    secure_endpoint(endpoint)
    files=[('image' if len(paths)==1 else 'image[]',(p.name,p.read_bytes(),mimetypes.guess_type(p.name)[0] or 'application/octet-stream')) for p in paths]
    retain_outbound(task,prompt,paths,checkpoint,[entry[1][1] for entry in files])
    page=(task.get('payload') or {}).get('page',{})
    size=design_spec.output_size(task.get('kind','image'),page)
    packet=task.get('remote',{}).get('render_request')
    if packet and packet.get('output_size')!=size:raise ModelFailure('请求尺寸与冻结记录不一致','local')
    try:
        r=httpx.post(endpoint,trust_env=False,headers={'Authorization':'Bearer '+key},data={'model':model['model_identifier'],'prompt':prompt,'n':'1','size':size},files=files,timeout=420)
        if r.status_code in {408,504}:
            image_receipts.retain(main,r,task,checkpoint,'http_timeout')
            raise ModelFailure('图片请求超时，提交结果未知；已暂停以避免重复生成','unknown')
        if not r.is_success:
            image_receipts.retain(main,r,task,checkpoint,'http_error')
            check(r)
        # HTTP success can contain a typed provider failure. An explicit error
        # without a pending handle follows normal finite failover; a damaged
        # receipt or ambiguous outcome cannot silently issue another request.
        try:
            body=r.json()
            rejected=image_receipts.rejection(body)
            if rejected:
                code,kind=rejected
                image_receipts.retain(main,r,task,checkpoint,'provider_rejected')
                raise ModelFailure('图片服务返回失败：'+code,kind)
            data=body.get('data') if isinstance(body,dict) else None
            if not isinstance(data,list) or not data or not isinstance(data[0],dict):
                raise ValueError()
            item=data[0]
            if item.get('b64_json'):
                content=base64.b64decode(item['b64_json'],validate=True)
                with Image.open(io.BytesIO(content)) as generated:
                    if generated.format not in {'PNG','JPEG','WEBP'}:
                        raise ValueError()
                    generated.verify()
                return content
            if not isinstance(item.get('url'),str) or not item['url'].strip():
                raise ValueError()
        except (ValueError,TypeError,OSError,SyntaxError,Image.DecompressionBombError):
            image_receipts.retain(main,r,task,checkpoint,'unusable_artifact')
            raise ModelFailure('图片请求已返回，但回执或图片损坏；结果未知，已暂停以避免重复生成','unknown') from None
        if item.get('url'):
            checkpoint({'download_url':item['url']})
            return download(item['url'])
    except (httpx.ConnectError,httpx.ConnectTimeout) as e: raise ModelFailure('图片模型连接失败：'+type(e).__name__) from e
    except httpx.RequestError as e:
        raise ModelFailure('图片请求已发送但结果未知；服务不支持任务查询，已暂停以避免重复生成','unknown') from e


def download(url):
    try:
        # Validate every redirect before following it; HTTPS must not downgrade.
        for _ in range(6):
            secure_endpoint(url)
            r=httpx.get(url,trust_env=False,timeout=120,follow_redirects=False)
            if r.is_redirect:
                from urllib.parse import urljoin
                location=r.headers.get('location')
                if not location:raise ModelFailure('图片下载重定向缺少地址','invalid')
                url=urljoin(url,location)
                continue
            check(r);return r.content
        raise ModelFailure('图片下载重定向过多','invalid')
    except httpx.RequestError as e: raise ModelFailure('图片下载中断，可恢复下载') from e
