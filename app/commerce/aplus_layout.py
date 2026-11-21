"""Deterministic publication formatting of existing whole detail artwork.

The source is one completed image. Its full, ordered pixel range is encoded as
standard A+ modules; this does not create design tasks or new product content.
"""
import copy
import hashlib
import io
import math

from PIL import Image, ImageOps,ImageChops
from . import amazon

PROTOCOL='standard-strips-v2'
WIDTH=970
MAX_MODULES=5  # Basic A+; no Premium entitlement is assumed.


def prepare(path,force_header=False):
    with Image.open(path) as source:
        if source.width*source.height>40_000_000:raise amazon.PlatformError('详情原图过大，无法准备发布格式',422)
        image=ImageOps.exif_transpose(source).convert('RGB')
        scale=min(WIDTH/image.width,MAX_MODULES*600/image.height)
        size=(max(1,round(image.width*scale)),max(1,round(image.height*scale)))
        full=image.resize(size,Image.Resampling.LANCZOS)
    assets=[];top=0
    quantum=600 if force_header else 300
    slack=math.ceil(full.height/quantum)*quantum-full.height
    gray=ImageOps.grayscale(full)
    while top<full.height:
        remaining=full.height-top
        height=600 if force_header or remaining>300 else 300
        bottom=min(top+height,full.height)
        # Prefer a nearby quiet row to avoid cutting a line of baked-in text.
        # Only use spare module space; every source row is still encoded once.
        if bottom<full.height and slack:
            ideal=bottom
            def boundary_cost(row):
                band=gray.crop((0,max(0,row-1),full.width,min(full.height,row+2)))
                horizontal=ImageChops.difference(band.crop((1,0,band.width,band.height)),band.crop((0,0,band.width-1,band.height))) if band.width>1 else band
                vertical=ImageChops.difference(gray.crop((0,row-1,full.width,row)),gray.crop((0,row,full.width,row+1)))
                return sum(horizontal.histogram()[30:])+2*sum(vertical.histogram()[30:])+(ideal-row)*0.25
            bottom=min(range(max(top+1,ideal-min(24,slack)),ideal+1),key=boundary_cost)
            slack-=ideal-bottom
        frame=Image.new('RGB',(WIDTH,height),'white')
        left=(WIDTH-full.width)//2
        frame.paste(full.crop((0,top,full.width,bottom)),(left,0))
        output=io.BytesIO();frame.save(output,'JPEG',quality=95)
        raw=output.getvalue()
        assets.append({'raw':raw,'width':WIDTH,'height':height,'source_box':[0,top,full.width,bottom],
            'scaled_size':list(full.size),'paste_left':left,'sha256':hashlib.sha256(raw).hexdigest(),
            'module_type':'STANDARD_HEADER_IMAGE_TEXT' if height==600 else 'STANDARD_IMAGE_TEXT_OVERLAY'})
        top=bottom
    return assets


def text_present(module):
    header=module.get('standardHeaderImageText') or {};block=header.get('block') or {}
    return any(header.get(key) for key in ('headline',)) or any(block.get(key) for key in ('headline','body'))


def document(name,locale,uploads,assets,title,template=None):
    if len(uploads)!=len(assets) or not 1<=len(assets)<=MAX_MODULES:
        raise amazon.PlatformError('详情发布图片和模块未完整对应，不能截断或混排',409)
    source_modules=(template or {}).get('contentModuleList') or []
    result={'name':name,'contentType':'EBC','locale':locale,'contentModuleList':[]}
    for index,(upload,asset) in enumerate(zip(uploads,assets)):
        section=asset.get('source_section',0)
        original=source_modules[section] if section<len(source_modules) else {}
        header=original.get('standardHeaderImageText') or {};block=header.get('block') or {}
        image={'uploadDestinationId':upload,'altText':(block.get('image',{}).get('altText') or title or 'Product')[:100],
            'imageCropSpecification':{'size':{'width':{'value':asset['width'],'units':'pixels'},
                'height':{'value':asset['height'],'units':'pixels'}}}}
        own_block={'image':image}
        first=index==0 or assets[index-1].get('source_section',0)!=section
        last=index+1==len(assets) or assets[index+1].get('source_section',0)!=section
        if asset['module_type']=='STANDARD_HEADER_IMAGE_TEXT':
            own_header={'block':own_block}
            if first and header.get('headline'):own_header['headline']=copy.deepcopy(header['headline'])
            if last:
                for key in ('headline','body'):
                    if block.get(key):own_block[key]=copy.deepcopy(block[key])
            module={'contentModuleType':asset['module_type'],'standardHeaderImageText':own_header}
        else:
            if text_present(original):raise amazon.PlatformError('人工文字需要原有文字模块，不能覆盖在产品图上',409)
            module={'contentModuleType':asset['module_type'],'standardImageTextOverlay':{
                'overlayColorType':'LIGHT','block':own_block}}
        result['contentModuleList'].append(module)
    return result
