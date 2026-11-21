import os
import sys
sys.dont_write_bytecode = True

import uvicorn
from app.security_runtime import server_options
from app import error_log

if __name__ == "__main__":
    # Stable single-process mode is the default. Development reload must be
    # explicitly enabled because the reloader starts a second process and can
    # hide differences in permissions/network access between launches.
    error_log.configure()
    try:
        uvicorn.run("app.main:app", **server_options(), log_level='error', log_config=None)
    except Exception as exc:
        error_log.record('server.start_failed', exc)
        raise
