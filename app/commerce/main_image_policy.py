"""Small, conservative main-image baseline; never a platform approval record.

Category style guides override general guidance.  No client-supplied policy or
percentage can turn a candidate into an approved image.
"""
import re

VERSION = 'amazon-main-strategy-v2'
GENERAL_GUIDE = 'https://sellercentral.amazon.com/help/hub/reference/external/G1881'
STYLE_GUIDE = 'https://sellercentral.amazon.com/help/hub/reference/external/G200270100'
COVER_TYPES = {'BOOK', 'BOOKS', 'MUSIC', 'VIDEO', 'DVD', 'VIDEO_DVD'}
SPECIAL = re.compile(r'(?:APPAREL|CLOTHING|SHIRT|DRESS|PANTS|TROUSERS|JACKET|COAT|'
                     r'SHOE|BOOT|SANDAL|JEWEL|NECKLACE|EARRING|BRACELET|HANDBAG|LUGGAGE)', re.I)


def resolve(draft=None):
    from app import design_contract
    product_type = str((draft or {}).get('product_type') or '').strip().upper()
    cover = product_type in COVER_TYPES
    review = bool(SPECIAL.search(product_type)) or not product_type or product_type == 'PRODUCT'
    presentation = ((draft or {}).get('source') or {}).get('design_presentation') or {}
    if design_contract.active({'pipeline': presentation}):
        strategy = design_contract.first_image(presentation)
        profile = strategy['profile']
        return {'version': VERSION, 'profile':profile, 'product_type':product_type,
                'minimum_fill': None if profile=='lifestyle_scene' else 1.0 if profile=='front_cover' else .85,
                'white_background_required':profile=='product_photo',
                'strategy':strategy, 'category_status':strategy.get('category_rule_status','needs_category_review'),
                'category_review_blocks_automatic_selection':review,
                'guidance_urls':[GENERAL_GUIDE,STYLE_GUIDE],
                'limitation':'商家首图设计策略不是Amazon类目许可、平台接受或买家展示证明；以当前目的地类目要求和实际回读为准',
                'message':'实际首图采用已记录的设计策略；商品与环境道具、真实销售组成须清楚区分，类目检查和平台展示单独记录'}
    return {'version': VERSION, 'profile': 'front_cover' if cover else 'product_photo',
            'product_type': product_type, 'minimum_fill': 1.0 if cover else .85,
            'white_background_required': not cover,
            'category_status': 'needs_category_review',
            'category_review_blocks_automatic_selection': review,
            'guidance_urls': [GENERAL_GUIDE, STYLE_GUIDE],
            'limitation': ('封面类按完整正封面100%画幅检查；不自动验证目标站点当前类目规则' if cover else
                           '85%仅为通用照片候选基线；类目/站点规则优先，未经核实不宣称平台合规'),
            'message': ('该商品类型涉及模特、角度或取景等类目差异，请核对官方类目要求后处理主图；通用检查不授予批准'
                        if review else '通用候选检查可进行；目标站点类目差异仍需核对，检查通过不等于Amazon接受')}
