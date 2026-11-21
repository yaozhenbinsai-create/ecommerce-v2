"""Design asset metadata; no automatic quality/identity approval engine."""
PROTOCOL = 'image-fidelity-v3'
IDENTITY_EVIDENCE_PROTOCOL = 'source-identity-evidence-v1'
INDEPENDENT_REVIEW_PROTOCOL = 'independent-visual-review-v1'
IDENTITY_ANCHORS = ('silhouette', 'surface_pattern', 'edge_structure', 'spatial_relations')
CHECKS = ('shape', 'color', 'material', 'parts', 'count', 'brand', 'copy',
          'scene_integration', 'layout', 'readability', 'purpose', 'prop_scope')
REFERENCE_BOARD_ROLE = 'product_reference_board'
LISTING_MAIN_ROLE = 'listing_main_candidate'


def is_reference_board(result):
    return isinstance(result,dict) and result.get('asset_role')==REFERENCE_BOARD_ROLE


GENERATED_PROTOCOL = 'generated-assets-v1'


def generated(result):
    """New generation provenance, with no visual approval claim."""
    if not isinstance(result, dict):return False
    pipeline=result.get('pipeline') or {}
    return (isinstance(pipeline,dict) and pipeline.get('generation_protocol')==GENERATED_PROTOCOL
            and pipeline.get('review_mode') in ('none', 'human'))


def source_hashes(result):
    return ((result.get('generation') or {}) if generated(result) else
            (result.get('verification') or {})).get('source_sha256')


def usable(result):
    """Generated files proceed directly; old review records retain their meaning."""
    return isinstance(result,dict) and isinstance(result.get('url'),str) and bool(result['url'])
