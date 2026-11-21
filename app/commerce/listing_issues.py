"""Read Amazon's factual improvement requests without changing offer state."""
from __future__ import annotations


def _attribute_names(issue):
    names = issue.get('attributeNames')
    if names is None:
        names = [issue['attributeName']] if issue.get('attributeName') is not None else []
    if not isinstance(names, list) or any(not isinstance(name, str) or not name for name in names):
        return []
    return sorted(set(names))


def preflight_missing(listing):
    """Read only traceable, current, fact-only missing-attribute responses."""
    from . import archive_fields, listing_release
    draft = listing.get('draft') or {}
    proof = listing.get('publication_snapshot') or {}
    if (proof.get('listing_id') != listing.get('id') or proof.get('version') != listing.get('draft_version')
            or proof.get('publication_cycle_id') != listing_release.cycle(listing)
            or proof.get('listing_draft_sha256') != listing_release.fingerprint(draft)):
        return {}
    if proof.get('job_id') is not None and listing.get('preflight_job_id') != proof['job_id']:
        return {}
    expected = {row['seller_sku'] for row in draft.get('skus', [])}
    if draft.get('variation_theme'):
        expected.add(draft['parent_sku'])
    result = {}
    responses = listing.get('preflight_results') or {}
    if not isinstance(responses, dict) or set(responses) - expected:
        return {}
    for sku, response in responses.items():
        if (not isinstance(response, dict) or not response.get('request_id')
                or response.get('http_status') not in {200, 202}):
            return {}
        body = response.get('body') or {}
        if not isinstance(body, dict):
            return {}
        if body.get('status') not in {'VALID', 'INVALID'}:
            return {}
        issues = body.get('issues') or []
        if not isinstance(issues, list):
            return {}
        for issue in issues:
            if not isinstance(issue, dict):
                return {}
            if str(issue.get('severity') or '').upper() != 'ERROR':
                continue
            categories = issue.get('categories') or []
            missing = isinstance(categories, list) and ('MISSING_ATTRIBUTE' in categories or
                      not categories and str(issue.get('code')) == '90220')
            if not missing:
                return {}  # Price, identity, access and other errors are not factual defaults.
            names = _attribute_names(issue)
            if not names or any(not archive_fields.is_fact(name) for name in names):
                return {}
            result.setdefault(sku, set()).update(names)
    return {sku: sorted(names) for sku, names in result.items()}


def observed(listing):
    result = []
    seen = set()
    for sku, observation in (listing.get('platform_observed') or {}).items():
        issues = observation.get('response', {}).get('body', {}).get('issues', [])
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            names = _attribute_names(issue)
            categories = [value for value in issue.get('categories', []) if isinstance(value, str)]
            row = {'sku': sku, 'code': str(issue.get('code') or ''),
                   'message': str(issue.get('message') or ''),
                   'severity': str(issue.get('severity') or 'INFO').upper(),
                   'categories': categories, 'attribute_names': names}
            key = (sku, row['code'], row['message'], row['severity'], tuple(names))
            if key not in seen:
                result.append(row)
                seen.add(key)
    return result


def missing_fields(listing, sku):
    """Only structured, SKU-specific missing facts qualify for completion."""
    return sorted({name for issue in observed(listing)
                   if issue['sku'] == sku and 'MISSING_ATTRIBUTE' in issue['categories']
                   for name in issue['attribute_names']} | set(preflight_missing(listing).get(sku, [])))


def completion_scope(listing):
    scope = set(listing.get('platform_observed') or {}) | set(preflight_missing(listing))
    return {sku: names for sku in sorted(scope)
            if (names := missing_fields(listing, sku))}
