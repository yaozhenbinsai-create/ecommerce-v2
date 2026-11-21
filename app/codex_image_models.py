"""Image model choices discovered from the signed-in official client."""
import re


IMAGE_CHANNEL_NOTICE = '这里只展示当前账号返回的图片模型。也可以在连接表单填写自己确认可用的模型标识。'


def catalog(models):
    result = []
    for model in models:
        identifier = str(model.get('model') or model.get('id') or '').strip()
        if not identifier:
            continue
        modalities = model.get('outputModalities') or []
        if 'image' not in modalities and not re.search(r'(?:^|[-_/])(?:image|dall-e|imagen)(?:[-_/\d]|$)', identifier, re.I):
            continue
        result.append({'id': identifier, 'display_name': model.get('displayName') or identifier,
                       'description': model.get('description') or IMAGE_CHANNEL_NOTICE,
                       'is_default': False, 'selectable': True})
    return result
