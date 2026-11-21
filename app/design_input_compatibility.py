"""Reuse completed outputs only while their actual product/design input matches."""
import json


def completed_input_matches(saved, current):
    from app.simple_design_flow import compatible_completed_input
    saved = json.loads(saved) if isinstance(saved, str) else dict(saved)
    if not saved.get('archive_digest') or saved['archive_digest'] != current.get('archive_digest'):
        return False
    if (saved.get('pipeline') or {}).get('workflow') != (current.get('pipeline') or {}).get('workflow'):
        return False
    # Code file hashes and frozen model configuration are not product facts.
    # Language, style, SKU selection, facts and source-image hashes still match.
    # Revision counters also advance for model-name/type declarations and other
    # nonvisual archive edits. The nonempty matching physical digest and every
    # remaining actual design input are still required above/below.
    ignored = {'pipeline', '_model_candidates', '_creative_context', 'product_fact_revision', 'sku_fact_revisions'}
    return compatible_completed_input({k: v for k, v in saved.items() if k not in ignored},
                                      {k: v for k, v in current.items() if k not in ignored})
