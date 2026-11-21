"""Bound routine snapshots without discarding open work or publication evidence."""
import json

from fastapi import HTTPException


def decode(row):
    job = dict(row)
    for field in ('payload', 'result'):
        job[field] = json.loads(job[field])
    return job


def current(c, listings, recent=200):
    rows = {r['id']: r for r in c.execute('SELECT * FROM commerce_jobs ORDER BY id DESC LIMIT ?', (recent,))}
    for row in c.execute("SELECT * FROM commerce_jobs WHERE status!='completed'"):
        rows[row['id']] = row
    # An old paused submit must not hide a newer success outside the recent page.
    # Current-version status therefore always includes its actual last write.
    for listing in listings:
        for preview in (0, 1):
            row = c.execute("""SELECT * FROM commerce_jobs WHERE kind='submit'
                AND json_extract(payload,'$.listing_id')=? AND json_extract(payload,'$.version')=?
                AND coalesce(json_extract(payload,'$.preview'),0)=? ORDER BY id DESC LIMIT 1""",
                (listing['id'], listing.get('draft_version') or 0, preview)).fetchone()
            if row is not None:
                rows[row['id']] = row
    return [decode(rows[key]) for key in sorted(rows, reverse=True)]


def history(c, *, before=None, limit=50):
    if not 1 <= limit <= 100 or (before is not None and before < 1):
        raise HTTPException(422, '历史任务页码或数量无效')
    total = c.execute('SELECT COUNT(*) FROM commerce_jobs').fetchone()[0]
    where, params = ('WHERE id<?', [before]) if before is not None else ('', [])
    rows = c.execute(f'SELECT * FROM commerce_jobs {where} ORDER BY id DESC LIMIT ?', (*params, limit + 1)).fetchall()
    selected = rows[:limit]
    from . import poll_retention
    changes=poll_retention.changes(c,limit=31,before=before)
    return {'jobs':[decode(row) for row in selected], 'total':total, 'retention':poll_retention.summary(c), 'changes':changes[:30],
            'changes_next_before':changes[29]['last_job_id'] if len(changes)>30 else None,
            'next_before':selected[-1]['id'] if len(rows) > limit else None}
