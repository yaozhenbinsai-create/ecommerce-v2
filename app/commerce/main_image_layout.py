"""Main-image layout: preserve visible source content, remove only safe outer margins."""
from __future__ import annotations

import math
import re

from PIL import Image, ImageChops, ImageOps
from . import main_image_policy

VERSION = 'source-photo-frame-v4'
MIN_FRAME_FILL = .85
MAX_UPSCALE = 1.25


def _margin_plan(photo, policy, source_passed):
    """Bound every detectable component; never erase interior pixels or infer a mask.

    Near-white products do not provide enough contrast to locate their outer
    edges safely. Such photographs need an explicit transparent source or review.
    The content bound includes shadows, disconnected kit parts and stray pixels.
    """
    full = [0, 0, photo.width, photo.height]
    base = {'applied': False, 'source_crop_box': full, 'content_bounds': None,
            'protection_px': 0, 'status': 'not_allowed', 'method': 'whole_photo'}
    if not source_passed or not policy['white_background_required']:
        return base
    alpha = photo.getchannel('A')
    transparent = alpha.getextrema()[0] < 255
    if transparent:
        mask = alpha.point(lambda v: 255 if v > 0 else 0)
        solid = alpha.point(lambda v: 255 if v >= 128 else 0)
        method = 'existing_alpha_all_visible_pixels'
    else:
        if re.search(r'\b(?:white|ivory|cream)\b|白|米色|象牙', str(policy.get('source_color') or ''), re.I):
            return base | {'status': 'uncertain', 'reason': '白色或近白色商品的边界不能仅凭白底像素确定，请提供透明原图或人工核验'}
        channels = [photo.getchannel(channel) for channel in ('R', 'G', 'B')]
        low = ImageChops.darker(ImageChops.darker(channels[0], channels[1]), channels[2])
        high = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])
        neutral = ImageChops.subtract(high, low).point(lambda v: 255 if v <= 6 else 0)
        bright = low.point(lambda v: 255 if v >= 250 else 0)
        mask = ImageChops.invert(ImageChops.multiply(neutral, bright))
        solid = low.point(lambda v: 255 if v < 210 else 0)
        method = 'near_white_outer_margin_only'
    bounds = mask.getbbox()
    if not bounds:
        return base | {'status': 'uncertain', 'method': method, 'reason': '未能确定可见商品内容，不能猜测边界'}
    solid_count = sum(solid.histogram()[128:])
    bound_area = (bounds[2]-bounds[0]) * (bounds[3]-bounds[1])
    if (solid_count / (photo.width*photo.height) < (.002 if transparent else .025) or
            (not transparent and solid_count / bound_area < .18)):
        return base | {'status': 'uncertain', 'method': method, 'content_bounds': list(bounds),
                       'reason': '白上白或弱对比边界不可靠，保留原照并等待透明原图或人工核验'}
    margin = max(2, math.ceil(max(bounds[2]-bounds[0], bounds[3]-bounds[1]) * .03))
    crop = [max(0, bounds[0]-margin), max(0, bounds[1]-margin),
            min(photo.width, bounds[2]+margin), min(photo.height, bounds[3]+margin)]
    return base | {'applied': crop != full, 'source_crop_box': crop, 'content_bounds': list(bounds),
                   'protection_px': margin, 'status': 'applied' if crop != full else 'not_needed',
                   'method': method, 'background_rgb_min': None if transparent else 250,
                   'background_max_channel_difference': None if transparent else 6,
                   'all_detectable_content_preserved': True,
                   'reason': '仅去外侧透明或近白空边；保留所有可检测内容及保护边，不判定商品语义边界'}


def frame_fill(subject, canvas_size, minimum=MIN_FRAME_FILL):
    """Measure an existing alpha channel only; opaque photos require vision."""
    if minimum is None:
        return {'method':'scene_composition_requires_visual','status':'requires_visual',
                'minimum':None,'ratio':None}
    alpha = subject.getchannel('A')
    if alpha.getextrema()[0] == 255:
        return {'method': 'opaque_photo_requires_visual', 'status': 'requires_visual',
                'minimum': minimum, 'ratio': None}
    bounds = alpha.point(lambda value: 255 if value >= 128 else 0).getbbox()
    ratio = (max(bounds[2]-bounds[0], bounds[3]-bounds[1])/max(canvas_size) if bounds else 0)
    return {'method': 'existing_source_alpha_ge_128', 'alpha_threshold': 128,
            'bounds_within_photo': list(bounds) if bounds else None,
            'denominator': 'longest_canvas_edge',
            'ratio': ratio, 'minimum': minimum,
            'status': 'eligible' if ratio >= minimum else 'reject'}


def inspect_source(source, policy=None):
    """Reject obvious source backgrounds before any white border is added.

    White/transparent edges are only a local precondition. They cannot establish
    an empty background, a true subject mask, or a complete sales unit.
    """
    policy = policy or main_image_policy.resolve({'product_type': 'GENERAL'})
    with Image.open(source) as raw:
        photo = ImageOps.exif_transpose(raw).convert('RGBA')
    if not policy['white_background_required']:
        return {'can_prepare': True, 'status': 'requires_visual',
                'method': policy['profile']+'_requires_semantic_check', 'source_size': list(photo.size)}
    sample = photo.copy(); sample.thumbnail((320, 320))
    w, h = sample.size; pixels = sample.load()
    def background(x, y):
        r, g, b, a = pixels[x, y]
        return a < 16 or (min(r, g, b) >= 245 and max(r, g, b)-min(r, g, b) <= 9)
    border = [background(x, 0) for x in range(w)] + [background(x, h-1) for x in range(w)]
    border += [background(0, y) for y in range(h)] + [background(w-1, y) for y in range(h)]
    span = max(1, min(w, h)//12)
    corners = [background(x, y) for y in list(range(span))+list(range(h-span, h))
               for x in list(range(span))+list(range(w-span, w))]
    border_ratio = sum(border)/len(border); corner_ratio = sum(corners)/len(corners)
    passed = border_ratio >= .88 and corner_ratio >= .90
    margin_plan = _margin_plan(photo, policy, passed)
    uncertain = margin_plan['status'] == 'uncertain'
    return {'can_prepare': passed and not uncertain,
            'status': 'requires_visual' if passed and not uncertain else 'requires_source_cleanup',
            'method': 'original_photo_edges_before_padding', 'source_size': list(photo.size),
            'white_or_transparent_border_ratio': round(border_ratio, 4),
            'white_or_transparent_corner_ratio': round(corner_ratio, 4),
            'margin_crop': margin_plan,
            'reason': (margin_plan['reason'] if uncertain else
                       '原照片边缘初筛通过，内部场景、杂物和完整套装仍须逐项视觉检查' if passed else
                       '原照片含非白底背景或场景；加白边不能成为合格主图，请补充本款白底/透明原图或人工处理背景')}




def preservation_valid(proof):
    """Require a complete current crop declaration rather than claiming no crop."""
    scale = proof.get('source_scale')
    if (proof.get('main_layout_version') != VERSION or proof.get('protocol') != 'source-layout-v2' or
            proof.get('subject_repainted') is not False or type(scale) not in (int,float) or
            not 0 < scale <= MAX_UPSCALE or proof.get('source_upscaled') is not (scale > 1) or
            proof.get('interpolated') is not (scale != 1)):
        return False
    if proof.get('source_photo_cropped') is False:
        return proof.get('white_margin_crop') is False
    crop = proof.get('margin_crop') or {}
    bounds, box = crop.get('content_bounds'), crop.get('source_crop_box')
    return (proof.get('source_photo_cropped') is True and proof.get('white_margin_crop') is True and
            crop.get('applied') is True and crop.get('all_detectable_content_preserved') is True and
            crop.get('method') in {'existing_alpha_all_visible_pixels','near_white_outer_margin_only'} and
            isinstance(bounds,list) and isinstance(box,list) and len(bounds)==len(box)==4 and
            proof.get('source_crop_box')==box and proof.get('source_content_bounds')==bounds and
            box[0]<=bounds[0]<bounds[2]<=box[2] and box[1]<=bounds[1]<bounds[3]<=box[3])
