"""Wake a workflow when its durable inputs change, never for elapsed poll time."""
from hashlib import sha256
import time

from . import store as s

PENDING = {'queued', 'running', 'retry_wait'}


def _state(row):
    if row is None:
        return None
    value = dict(row)
    if value.get('status') in PENDING:
        value['status'] = 'pending'
        value.pop('attempts', None)
    return value


def signature(c, workflow):
    # A lease heartbeat, model retry, or individual image completion does not
    # make a waiting listing ready. The final dependency/run outcome does.
    dependencies = []
    for name, ident in sorted(workflow.get('jobs', {}).items()):
        row = c.execute('SELECT id,status,attempts FROM commerce_jobs WHERE id=?', (ident,)).fetchone()
        dependencies.append([name, ident, _state(row)])
    state = {'revision': workflow['revision'], 'stage': workflow.get('stage'),
             'dependencies': dependencies}
    if workflow.get('stage') in {'official_fields', 'preflight'}:
        listing = s.get(c, 'listing', workflow.get('listing_id'), required=False)
        proof = ((listing or {}).get('draft') or {}).get('source', {}).get('attribute_completion') or {}
        field_job = c.execute('SELECT id,status,attempts FROM commerce_jobs WHERE id=?',
                             (proof.get('job_id'),)).fetchone()
        state['official_fields'] = {
            'version': (listing or {}).get('draft_version'),
            'published': (listing or {}).get('published_version'),
            'status': proof.get('status'), 'signature': proof.get('signature'),
            'job': _state(field_job)}
    if workflow.get('stage') in {'readback','detail'}:
        listing=s.get(c,'listing',workflow.get('listing_id'),required=False)
        from . import listing_issues
        state['readback']={
            'listing':[listing.get('draft_version'),listing.get('published_version'),bool(listing.get('paused'))] if listing else None,
            'expected':sorted(sku['seller_sku'] for sku in (listing or {}).get('draft',{}).get('skus',[])),
            'observed':sorted([sku,row.get('status'),row.get('identity_status')]
                              for sku,row in ((listing or {}).get('platform_observed') or {}).items()),
            'errors':[row for row in listing_issues.observed(listing or {}) if row['severity']=='ERROR']}
        if workflow.get('stage')=='readback':
            # A single deadline transition, never a job for each clock tick.
            deadline=workflow.get('readback_deadline') or 0
            state['readback']['expired']=bool(deadline and time.time()>=deadline)
    if workflow.get('stage')=='detail':
        pub=s.get(c,'detail_publication',listing.get('detail_publication_id'),required=False) if listing and listing.get('detail_publication_id') else None
        watched=[pub] if pub else []
        if pub:
            watched.extend(s.get(c,'detail_publication',ident) for ident in pub.get('children',[]))
        # Status transitions matter; checking timestamps and retry counters do not.
        state['detail']={'listing':[listing.get('draft_version'),listing.get('published_version')] if listing else None,
                         'publications':[[row['id'],row.get('status')] for row in watched]}
    if workflow.get('stage') == 'design':
        project = c.execute('SELECT simple_current_run,updated_at FROM design_projects WHERE id=?',
                            (workflow['project_id'],)).fetchone()
        latest = c.execute("""SELECT id,status,input_hash FROM design_simple_runs
            WHERE project_id=? AND mode NOT IN ('detail','detail_redesign','detail_single')
            ORDER BY id DESC LIMIT 1""", (workflow['project_id'],)).fetchone()
        watched = c.execute('SELECT id,status,input_hash FROM design_simple_runs WHERE id=?',
                             (workflow.get('design_run_id'),)).fetchone()
        product = c.execute("SELECT id,revision FROM commerce_records WHERE kind='product' AND natural_key=?",
                            (f'design:{workflow["project_id"]}',)).fetchone()
        skus = c.execute("""SELECT id,revision FROM commerce_records WHERE kind='sku'
            AND json_extract(data,'$.product_id')=? ORDER BY id""", (product['id'],)).fetchall() if product else []
        state.update(project=dict(project) if project else None,
                     latest_design=_state(latest), watched_design=_state(watched),
                     product=dict(product) if product else None, skus=[dict(row) for row in skus])
    return sha256(s.dump(state).encode()).hexdigest()[:32]


def schedule(c):
    from . import execution_controls
    workflows = c.execute("""SELECT * FROM commerce_records WHERE kind='listing_workflow'
        AND json_extract(data,'$.status') IN ('running','waiting') ORDER BY id""").fetchall()
    for row in workflows:
        workflow = s.decode(row)
        if execution_controls.workflow_paused(c, workflow):
            continue
        pending = c.execute("""SELECT 1 FROM commerce_jobs WHERE kind='listing_workflow'
            AND json_extract(payload,'$.workflow_id')=?
            AND status IN ('queued','running','retry_wait') LIMIT 1""", (workflow['id'],)).fetchone()
        if pending:
            continue
        key = f'listing-workflow:{workflow["id"]}:state:{signature(c, workflow)}'
        # Avoid INSERT OR IGNORE and decoding a stored result on every quiet
        # scheduler tick. The unique key also protects concurrent schedulers.
        if c.execute('SELECT 1 FROM commerce_jobs WHERE dedupe=?', (key,)).fetchone():
            continue
        s.enqueue(c, 'listing_workflow', key,
                  {'workflow_id': workflow['id'], 'target_id': workflow['target_id']})
