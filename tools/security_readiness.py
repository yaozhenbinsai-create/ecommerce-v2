"""Run locally on the deployment host. Prints status only, never secret values."""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    from app.security_readiness import inspect
    print(json.dumps(inspect(), ensure_ascii=False, indent=2))
