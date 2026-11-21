"""Transactional change clock; reading a revision never scans business history."""

def install(c):
    c.execute("CREATE TABLE IF NOT EXISTS commerce_snapshot_clock (id INTEGER PRIMARY KEY CHECK(id=1), value INTEGER NOT NULL)")
    c.execute("INSERT OR IGNORE INTO commerce_snapshot_clock VALUES(1,1)")
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ('commerce_records','commerce_jobs','commerce_stock_events','platform_accounts'):
        if table not in tables:continue
        for action in ('INSERT','UPDATE','DELETE'):
            name='snapshot_clock_'+table+'_'+action.lower()
            condition=''
            if table=='commerce_jobs' and action=='UPDATE':
                fields=('status','attempts','model_role','role_attempts','next_at','error','result','payload','updated_at')
                condition=' WHEN '+' OR '.join(f'OLD.{key} IS NOT NEW.{key}' for key in fields)
            c.execute(f"CREATE TRIGGER IF NOT EXISTS {name} AFTER {action} ON {table}{condition} BEGIN UPDATE commerce_snapshot_clock SET value=value+1 WHERE id=1; END")

def read(c):
    row=c.execute('SELECT value FROM commerce_snapshot_clock WHERE id=1').fetchone()
    if row is None:raise RuntimeError('Snapshot change clock is not installed')
    return 'v2:'+str(row[0])
