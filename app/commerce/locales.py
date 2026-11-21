"""Listing language is resolved from the configured account/marketplace target."""
from fastapi import HTTPException

LANGUAGES=dict([
    ('en_US','英语（美国）'),('en_CA','英语（加拿大）'),('fr_CA','法语（加拿大）'),
    ('es_MX','西班牙语（墨西哥）'),('pt_BR','葡萄牙语（巴西）'),('en_GB','英语（英国）'),
    ('de_DE','德语（德国）'),('fr_FR','法语（法国）'),('it_IT','意大利语'),('es_ES','西班牙语（西班牙）'),
    ('nl_NL','荷兰语'),('nl_BE','荷兰语（比利时）'),('fr_BE','法语（比利时）'),('en_IE','英语（爱尔兰）'),
    ('sv_SE','瑞典语'),('pl_PL','波兰语'),('ar_AE','阿拉伯语（阿联酋）'),('ar_SA','阿拉伯语（沙特）'),
    ('ar_EG','阿拉伯语（埃及）'),('en_IN','英语（印度）'),('hi_IN','印地语'),('tr_TR','土耳其语'),
    ('en_ZA','英语（南非）'),('ja_JP','日语'),('en_AU','英语（澳大利亚）'),('en_SG','英语（新加坡）'),
    ('zh_CN','简体中文'),('zh_TW','繁体中文（台湾）'),('zh_HK','繁体中文（香港）'),('ko_KR','韩语'),
])
DEFAULTS=dict(zip([
    'ATVPDKIKX0DER','A2EUQ1WTGCTBG2','A1AM78C64UM0Y8','A2Q3Y263D00KWC','A1F83G8C2ARO7P',
    'A1PA6795UKMFR9','A13V1IB3VIYZZH','APJ6JRA9NG5V4','A1RKKUPIHCS9HS','A1805IZSGTT6HS',
    'AMEN7PMS3EDWL','A28R8C7NBKEWEA','A2NODRKZP88ZB9','A1C3SOZRARQ6R3','A2VIGQ35RCS4UG',
    'A17E79C6D8DWNP','A21TJRUUN4KGV','A33AVAJ2PDY3EV','ARBP9OOSHTCHU','AE08WJ6YKNBMC',
    'A1VC38T7YXB528','A39IBJ37TRP1C6','A19VAU5U5O7RUS',
],['en_US','en_CA','es_MX','pt_BR','en_GB','de_DE','fr_FR','it_IT','es_ES','nl_NL','nl_BE','en_IE',
   'sv_SE','pl_PL','ar_AE','ar_SA','en_IN','tr_TR','ar_EG','en_ZA','ja_JP','en_AU','en_SG']))
ALIASES={'en':'en_US','zh':'zh_CN','de':'de_DE','fr':'fr_FR','it':'it_IT','es':'es_ES','pt':'pt_BR',
         'nl':'nl_NL','sv':'sv_SE','pl':'pl_PL','ar':'ar_SA','ja':'ja_JP','tr':'tr_TR','ko':'ko_KR','hi':'hi_IN'}


def normalize(value):
    code=str(value or '').strip().replace('-','_')
    code=ALIASES.get(code,code)
    if code not in LANGUAGES:raise HTTPException(422,'请选择已支持的国家地区语言')
    return code


def resolve(target):
    language=target.get('language','');mode=target.get('language_mode')
    # The original directory assigned en to every marketplace. Treat that legacy
    # default as automatic; explicit language overrides now have their own mode.
    if mode=='marketplace' or not language or (mode is None and language=='en'):
        remote=next((x.get('marketplace',{}).get('defaultLanguageCode') for x in target.get('connection',{}).get('marketplaces',[]) if x.get('marketplace',{}).get('id')==target.get('marketplace_id')),None)
        code=normalize(remote) if remote else DEFAULTS.get(target.get('marketplace_id'))
        if not code:
            if language:code=normalize(language)
            elif target.get('environment')=='mock':code='en_US'
            else:raise HTTPException(422,'自定义地区需要明确配置上架资料语言')
    else:code=normalize(language)
    return code


def settings(target):
    code=resolve(target)
    return {'language':code,'language_tag':code,'language_name':LANGUAGES[code],
            'marketplace_id':target['marketplace_id'],'target_id':target.get('id'),
            'account_id':target.get('account_id'),'environment':target.get('environment'),'country_region':target.get('name','')}


def configure(data):
    result=dict(data)
    if result.get('language_mode')=='marketplace' or not result.get('language'):
        result['language_mode']='marketplace'
        result['language']=resolve({**result,'language':''})
    else:
        result['language_mode']='explicit';result['language']=normalize(result['language'])
    return result


def options():return [{'value':k,'label':v} for k,v in LANGUAGES.items()]


def assert_current(target,draft):
    actual=normalize(draft.get('language','en'))
    if actual!=resolve(target):raise HTTPException(409,'上架资料语言与当前店铺地区配置不一致，请重新生成资料')
    snapshot=draft.get('localization')
    if snapshot and (snapshot.get('target_id')!=target.get('id') or any(snapshot.get(k)!=target.get(k) for k in ['account_id','marketplace_id','environment'])):raise HTTPException(409,'资料所属店铺地区与当前目标不一致')
    return actual
