"""Local reminders only; credentials are never sent to notification records."""
from datetime import datetime, timezone
from . import amazon, operations


def schedule(c):
    day = datetime.now(timezone.utc).date().isoformat()
    for account in c.execute("SELECT id,account_name FROM platform_accounts WHERE enabled=1 AND platform='亚马逊'").fetchall():
        status = amazon.credential_status(account['id'])
        if not any(status['fields'].values()):
            continue
        rotation = status['rotation']
        if rotation['status'] == 'scheduled':
            continue
        suffix = 'unknown' if rotation['status'] == 'unknown' else day
        operations.notify(c, f"lwa-rotation:{account['id']}:{suffix}",
                          account['account_name'] + '：' + rotation['message'])
