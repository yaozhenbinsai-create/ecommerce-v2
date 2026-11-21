"""Encrypted individual accounts for opt-in secure deployments.

No passwords, MFA seeds, recovery codes or request payloads are sent to logs.
The process serving HTTP must use the same vault identity as the setup CLI.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import struct
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from app import security_secrets

ROLES = {"admin", "operator", "production", "shareholder"}
ITERATIONS = 600_000
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_AGE = 365 * 86400
PASSWORD_MIN_AGE = 86400
IDLE_TIMEOUT = 7 * 86400
SESSION_MAX_AGE = 30 * 86400
LOCK_SECONDS = 30 * 60
FAIL_LIMIT = 10
_LOCK = threading.RLock()


class AccountError(ValueError):
    """Safe, user-facing account configuration error."""


def enabled():
    return os.environ.get("SHOP_SECURITY_MODE") == "1" or local_required()


def store_path():
    default = getattr(sys.modules.get("app.main"), "DATA_ROOT", Path(__file__).resolve().parents[2] / "data")
    root = Path(os.environ.get("ECOMMERCE2_DATA_DIR", str(default)))
    # Keep the final directory component unresolved so symlink checks remain useful.
    return root.absolute() / "security_users.enc"


def _local_config():
    """Read the explicit local-login switch; malformed state never means disabled."""
    path = store_path().parent / "local_auth.json"
    try:
        try:
            info = path.lstat()
        except FileNotFoundError:
            account_path = store_path()
            if account_path.exists() or account_path.is_symlink():
                return {"required": True, "enrolled": True}
            return {"required": True, "enrolled": False}
        if not stat.S_ISREG(info.st_mode):
            raise ValueError()
        if os.name != "nt" and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022):
            raise ValueError()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError()
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError()
        data = json.loads(raw.decode("utf-8-sig"))
        if (not isinstance(data, dict) or set(data) - {"required", "enrolled", "auth_mode"}
                or type(data.get("required")) is not bool
                or ("enrolled" in data and type(data["enrolled"]) is not bool)
                or (data.get("enrolled") is True and data["required"] is not True)
                or data.get("auth_mode", "mfa") not in {"mfa", "password"}):
            raise ValueError()
        return data
    except (OSError, ValueError, UnicodeError):
        raise AccountError("本机登录配置无效，已拒绝免登录访问；请修复 local_auth.json") from None


def local_required():
    return _local_config().get("required") is True


def password_only():
    # Legacy account format; deployment can require MFA during enrollment migration.
    return _local_config().get('auth_mode') == 'password'


def mfa_required():
    from app.security_runtime import load_config
    return not password_only() or load_config().get('mfa_required') is True


def shareholder_password_login_enabled():
    from app.security_runtime import load_config
    return password_only() and load_config().get('shareholder_password_login_enabled') is True


def _local_bootstrap_configured():
    config = _local_config()
    return (config.get("required") is True and config.get("enrolled") is not True and not password_only()
            and os.environ.get("SHOP_SECURITY_MODE") != "1" and os.environ.get("SHOP_PUBLIC_MODE") != "1")


def bootstrap_allowed():
    """Existing, empty, disabled or unreadable account stores never reopen setup."""
    try:
        if not _local_bootstrap_configured():
            return False
        with locked_store() as data:
            return not store_path().exists() and not data["users"] and _local_bootstrap_configured()
    except (AccountError, security_secrets.SecretStorageError):
        return False


def _mark_local_enrolled():
    path = store_path().parent / "local_auth.json"
    temporary = path.with_name(".local_auth." + secrets.token_hex(12) + ".tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(temporary, flags, 0o600), "wb") as stream:
            stream.write(json.dumps({**_local_config(), 'required': True, 'enrolled': True}).encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise AccountError("管理员已创建，但初始化标记保存失败。请使用刚创建的账号登录，并请管理员检查本机登录配置") from None
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _valid_store(data):
    if not isinstance(data, dict):
        return False
    if not data:
        return True
    if data.get("version") != 1 or not isinstance(data.get("users"), dict) or not isinstance(data.get("failures", {}), dict):
        return False
    for name, account in data["users"].items():
        if not isinstance(name, str) or not isinstance(account, dict):
            return False
        if account.get("role") not in ROLES or not isinstance(account.get("password_hash"), str):
            return False
        if any(key in account and type(account[key]) is not bool for key in ('pending_activation','must_change_password','disabled')):
            return False
        if (not isinstance(account.get("totp_secret"), str)
                or (not re.fullmatch(r"[A-Z2-7]{32}", account["totp_secret"])
                    and not (account.get('auth_method') == 'password' and account['totp_secret'] == ''))):
            return False
        if not isinstance(account.get("password_changed_at"), (int, float)):
            return False
        if not isinstance(account.get("last_totp_counter", -1), int) or not isinstance(account.get("revision", ""), str):
            return False
        if not isinstance(account.get("password_history", []), list) or not all(isinstance(x, str) for x in account.get("password_history", [])):
            return False
        if not isinstance(account.get("recovery_hashes", []), list) or not all(isinstance(x, str) for x in account.get("recovery_hashes", [])):
            return False
    for key, failure in data.get("failures", {}).items():
        if not isinstance(key, str) or not isinstance(failure, dict) or not all(isinstance(failure.get(field, 0), (int, float)) for field in ("last", "until", "count")):
            return False
    return True


@contextmanager
def locked_store():
    """Serialize CLI and web authentication updates, including TOTP counters."""
    with _LOCK:
        path = store_path()
        fd = _open_lock(path)
        with os.fdopen(fd, "r+b") as handle:
            if os.name == "nt":
                import msvcrt
                if handle.seek(0, 2) == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                data = security_secrets.load_json(path, validator=_valid_store)
                if not data:
                    data = {"version": 1, "users": {}, "failures": {}}
                yield data
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _open_lock(path):
    """Validate before opening; nonblocking/no-follow prevents FIFO/symlink races."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent = path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or getattr(path.parent, "is_junction", lambda: False)():
            raise security_secrets.SecretStorageError("安全账号目录不能是符号链接或重解析目录")
        if os.name != "nt" and (parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077):
            raise security_secrets.SecretStorageError("安全账号目录须归服务账号所有且权限为700")
        lock_path = path.with_suffix(".lock")
        try:
            info = lock_path.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and not stat.S_ISREG(info.st_mode):
            raise security_secrets.SecretStorageError("安全账号锁路径必须是普通文件")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(str(lock_path), flags, 0o600)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise security_secrets.SecretStorageError("安全账号锁路径必须是普通文件")
            if os.name != "nt" and (opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) & 0o077):
                raise security_secrets.SecretStorageError("安全账号锁文件须归服务账号所有且权限为600")
            return fd
        except BaseException:
            os.close(fd)
            raise
    except OSError:
        raise security_secrets.SecretStorageError("无法安全访问账号锁文件，请检查路径与权限") from None


def _save(data):
    security_secrets.save_json(store_path(), data, validator=_valid_store)


def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password, encoded):
    if len(password) > 1024:
        return False
    try:
        algorithm, rounds, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or not ITERATIONS <= int(rounds) <= 2_000_000:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(digest, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def validate_password(username, password, account=None, *, now=None, incident_reset=False):
    minimum = PASSWORD_MIN_LENGTH
    if not minimum <= len(password) <= 1024:
        raise AccountError(f"密码须为 {minimum} 至 1024 个字符")
    if not (any(c.isupper() for c in password) and any(c.islower() for c in password)
            and any(c.isdigit() for c in password) and any(not c.isalnum() and not c.isspace() for c in password)):
        raise AccountError("密码须同时包含大写、小写字母、数字和特殊字符")
    if username.casefold() in password.casefold():
        raise AccountError("密码不能包含用户名")
    if account:
        now = time.time() if now is None else now
        if not incident_reset and now - account["password_changed_at"] < PASSWORD_MIN_AGE:
            raise AccountError("常规密码更改至少间隔 24 小时；安全事件恢复须由管理员使用离线恢复流程")
        for previous in [account["password_hash"], *account.get("password_history", [])][:10]:
            if verify_password(password, previous):
                raise AccountError("不能重复使用最近 10 个密码")


def new_totp_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def totp_code(secret, counter):
    key = base64.b32decode(secret.upper(), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff
    return f"{number % 1_000_000:06d}"


def matching_counter(secret, code, *, now=None, last_counter=-1):
    if not re.fullmatch(r"[0-9]{6}", code):
        return None
    step = int((time.time() if now is None else now) // 30)
    for counter in (step, step - 1, step + 1):
        if counter > last_counter and counter >= 0 and hmac.compare_digest(totp_code(secret, counter), code):
            return counter
    return None


def new_recovery_codes():
    return [secrets.token_hex(10) for _ in range(8)]


def _recovery_hash(code):
    return hashlib.sha256(code.strip().lower().encode("ascii", errors="replace")).hexdigest()


def _new_account(username, password, role, totp_secret, enrollment_code, recovery_codes):
    if not username or len(username) > 100 or any(c.isspace() or ord(c) < 32 for c in username):
        raise AccountError("用户名须为 1 至 100 个非空白字符")
    if role not in ROLES:
        raise AccountError("账号角色无效")
    validate_password(username, password)
    counter = matching_counter(totp_secret, enrollment_code)
    if counter is None:
        raise AccountError("认证器验证码不正确，请检查设备时间")
    return {
        "password_hash": password_hash(password), "password_history": [],
        "password_changed_at": time.time(), "role": role, "disabled": False,
        "totp_secret": totp_secret, "last_totp_counter": counter,
        "recovery_hashes": [_recovery_hash(code) for code in (recovery_codes or [])],
        "revision": secrets.token_hex(16),
    }


def add_user(username, password, role, totp_secret, enrollment_code, *, recovery_codes=None):
    account = _new_account(username, password, role, totp_secret, enrollment_code, recovery_codes)
    with locked_store() as data:
        if not data["users"] and (username, role) != ("ADMIN", "admin"):
            raise AccountError("首个管理员账号须为 ADMIN")
        if username in data["users"]:
            raise AccountError("用户名已存在")
        data["users"][username] = account
        _save(data)


def first_admin(username, password, secret, otp, recovery_codes):
    """Create exactly one first administrator; access.py must verify a local peer.

    The login switch is already required before this operation. If marking
    enrollment fails after saving, the existing account file still blocks every
    later bootstrap attempt and the administrator can log in normally.
    """
    if username != "ADMIN":
        raise AccountError("首个管理员账号须为 ADMIN")
    account = _new_account(username, password, "admin", secret, otp, recovery_codes)
    with locked_store() as data:
        if not _local_bootstrap_configured() or store_path().exists() or data["users"]:
            raise AccountError("首次管理员设置已关闭；已有账号或初始化记录时请使用登录或管理员恢复流程")
        data["users"][username] = account
        _save(data)
        _mark_local_enrolled()


def _failure_keys(username, source):
    return ["user:" + hashlib.sha256(username.encode("utf-8")).hexdigest(),
            "source:" + hashlib.sha256(source.encode("utf-8")).hexdigest()]


def _prune_failures(data, now):
    data["failures"] = {key: value for key, value in data.get("failures", {}).items()
                        if value.get("until", 0) > now or value.get("last", 0) > now - LOCK_SECONDS}


def _record_failure(data, keys, now):
    for key in keys:
        record = data["failures"].get(key, {"count": 0, "last": now, "until": 0})
        if record.get("last", 0) <= now - LOCK_SECONDS:
            record = {"count": 0, "last": now, "until": 0}
        record["count"] += 1
        record["last"] = now
        if record["count"] >= FAIL_LIMIT:
            record["until"] = now + LOCK_SECONDS
        data["failures"][key] = record
    # Bound unauthenticated state growth; active lockouts are kept first.
    if len(data["failures"]) > 2000:
        entries = sorted(data["failures"].items(), key=lambda kv: (kv[1].get("until", 0), kv[1].get("last", 0)), reverse=True)
        data["failures"] = dict(entries[:2000])


def account_revision(account):
    fields = {key: account.get(key) for key in ("revision", "role", "disabled", "password_hash", "password_changed_at", "totp_secret", "pending_activation", "must_change_password")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode("utf-8")).hexdigest()


def authenticate(username, password, otp, source, *, now=None, new_password=None, shareholder_login=False):
    """Return (account snapshot | None, reason); updates are saved before success."""
    now = time.time() if now is None else now
    if shareholder_login and not shareholder_password_login_enabled():
        return None, 'invalid'
    username = username[:100]
    with locked_store() as data:
        _prune_failures(data, now)
        keys = _failure_keys(username, source)
        if any(data["failures"].get(key, {}).get("until", 0) > now for key in keys):
            return None, "locked"
        account = data["users"].get(username)
        shareholder_session = bool(shareholder_login and account and account.get('role') == 'shareholder')
        require_mfa = mfa_required() and not shareholder_session
        enrollment = bool(require_mfa and password_only() and account and not account.get('totp_secret'))
        # Unknown names still pay the same PBKDF cost as existing accounts.
        encoded = account["password_hash"] if account else f"pbkdf2_sha256${ITERATIONS}$" + "00" * 16 + "$" + "00" * 32
        password_ok = verify_password(password, encoded)
        counter = None
        recovery = None
        if require_mfa and password_ok and account and not account.get("disabled") and account.get('totp_secret'):
            counter = matching_counter(account["totp_secret"], otp, now=now, last_counter=account.get("last_totp_counter", -1))
            if counter is None and len(otp) == 20:
                digest = _recovery_hash(otp)
                recovery = next((item for item in account.get("recovery_hashes", []) if hmac.compare_digest(item, digest)), None)
        if not password_ok or not account or account.get("disabled") or account.get('pending_activation') or (shareholder_login and not shareholder_session) or (require_mfa and not enrollment and counter is None and recovery is None):
            _record_failure(data, keys, now)
            _save(data)
            return None, "invalid"
        if counter is not None:
            account["last_totp_counter"] = counter
        elif recovery is not None:
            account["recovery_hashes"].remove(recovery)
        for key in keys:
            data["failures"].pop(key, None)
        if new_password is not None:
            try:
                validate_password(username, new_password, account, now=now,
                                  incident_reset=bool(account.get('must_change_password')) or (require_mfa and len(password) < PASSWORD_MIN_LENGTH))
                _replace_password(account, new_password, now)
                account['must_change_password'] = False
            except AccountError:
                _save(data)  # A successfully presented MFA code is single-use even if the new password fails policy.
                raise
        _save(data)
        if account.get('must_change_password') or (require_mfa and len(new_password if new_password is not None else password) < PASSWORD_MIN_LENGTH):
            return None, 'change_required'
        if now - account["password_changed_at"] >= PASSWORD_MAX_AGE:
            return None, "expired"
        return {"username": username, "role": account["role"], "revision": account_revision(account),
                "mfa_verified": bool(require_mfa and not enrollment), "mfa_enrollment_required": enrollment,
                "shareholder_password_session": shareholder_session,
                "recovery_used": recovery is not None}, "ok"


def _replace_password(account, new_password, now):
    account["password_history"] = [account["password_hash"], *account.get("password_history", [])][:9]
    account["password_hash"] = password_hash(new_password)
    account["password_changed_at"] = now
    account["revision"] = secrets.token_hex(16)


def update_user(username, *, password=None, role=None, disabled=None, incident_reset=False,
                totp_secret=None, enrollment_code=None, recovery_codes=None):
    """Offline administrator recovery; OS/vault access is required."""
    with locked_store() as data:
        account = data["users"].get(username)
        if not account:
            raise AccountError("账号不存在")
        if role is not None and role not in ROLES:
            raise AccountError("账号角色无效")
        if account["role"] == "admin" and not account.get("disabled") and (disabled or (role is not None and role != "admin")):
            if not any(name != username and value["role"] == "admin" and not value.get("disabled") for name, value in data["users"].items()):
                raise AccountError("不能停用或降级最后一个管理员")
        if password is not None:
            validate_password(username, password, account, incident_reset=incident_reset)
            _replace_password(account, password, time.time())
        if role is not None:
            account["role"] = role
        if disabled is not None:
            account["disabled"] = bool(disabled)
        if totp_secret is not None:
            counter = matching_counter(totp_secret, enrollment_code or "")
            if counter is None:
                raise AccountError("认证器验证码不正确")
            account.update(totp_secret=totp_secret, last_totp_counter=counter,
                           recovery_hashes=[_recovery_hash(code) for code in (recovery_codes or [])])
        account["revision"] = secrets.token_hex(16)
        if incident_reset:
            data["failures"].pop(_failure_keys(username, "")[0], None)
        _save(data)


def session_account(entry, *, now=None):
    now = time.time() if now is None else now
    if entry.get("expiry", 0) <= now or now - entry.get("last_seen", 0) >= IDLE_TIMEOUT:
        return None
    with locked_store() as data:
        account = data["users"].get(entry.get("username"))
        if not account or account.get("disabled") or account.get('pending_activation') or account.get('must_change_password') or now - account["password_changed_at"] >= PASSWORD_MAX_AGE:
            return None
        if not hmac.compare_digest(entry.get("revision", ""), account_revision(account)):
            return None
        shareholder_session = bool(entry.get('shareholder_password_session') and account['role'] == 'shareholder'
                                   and shareholder_password_login_enabled())
        if entry.get('shareholder_password_session') and not shareholder_session:
            return None
        enrollment = bool(mfa_required() and not shareholder_session and not account.get('totp_secret'))
        if mfa_required() and not shareholder_session and not enrollment and not entry.get('mfa_verified'):
            return None
        return {"username": entry["username"], "role": account["role"], "mfa_enrollment_required": enrollment}


def enroll_mfa(username, revision, secret, otp, codes):
    """Only a fresh password-authenticated enrollment session can bind once."""
    with locked_store() as data:
        now = time.time()
        keys = _failure_keys(username, 'mfa-enrollment:' + username)
        _prune_failures(data, now)
        if any(data['failures'].get(key, {}).get('until', 0) > now for key in keys):
            raise AccountError('验证尝试过多，请30分钟后重试')
        account = data['users'].get(username)
        if (not account or account.get('disabled') or account.get('totp_secret')
                or account.get('must_change_password') or account.get('pending_activation')
                or not hmac.compare_digest(account_revision(account), revision)):
            raise AccountError('账号已变化或已绑定，请重新登录')
        counter = matching_counter(secret, otp)
        if counter is None:
            _record_failure(data, keys, now)
            _save(data)
            raise AccountError('验证码不正确，请检查验证器时间')
        account.update(totp_secret=secret, last_totp_counter=counter,
                       recovery_hashes=[_recovery_hash(code) for code in codes], revision=secrets.token_hex(16))
        _save(data)


def status():
    path = store_path()
    if not path.exists():
        return {"configured": False, "user_count": 0, "mfa_user_count": 0,
                "store_exists": False, "total_user_count": 0}
    with locked_store() as data:
        active = [account for account in data["users"].values() if not account.get("disabled")]
        return {"configured": any(account["role"] == "admin" and not account.get('pending_activation') for account in active),
                "mfa_required": mfa_required(),
                "store_exists": True, "total_user_count": len(data["users"]),
                "user_count": len(active), "mfa_user_count": sum(bool(account.get("totp_secret")) for account in active),
                "password_rotation_pending_count": sum(bool(account.get("must_change_password")) for account in active),
                "password_min_length": PASSWORD_MIN_LENGTH,
                "idle_timeout_seconds": IDLE_TIMEOUT, "lockout_seconds": LOCK_SECONDS,
                "password_max_age_days": 365, "password_history_count": 10}


def require_password_rotation():
    """One-time policy migration: revoke old sessions and require compliant passwords."""
    with locked_store() as data:
        changed = 0
        for account in data["users"].values():
            if account.get("pending_activation") or account.get("must_change_password"):
                continue
            account["must_change_password"] = True
            changed += 1
        if changed:
            _save(data)
        return changed


def validate_configuration():
    if os.environ.get("SHOP_PUBLIC_MODE") == "1" and os.environ.get("SHOP_SECURITY_MODE") != "1":
        raise AccountError("公网模式必须启用 SHOP_SECURITY_MODE=1 和HTTPS；本机登录开关不能替代公网安全部署")
    if not enabled():
        return
    if password_only():
        with locked_store() as data:
            owners = [u for u in data['users'].values() if u['role'] == 'admin']
            if len(owners) != 1 or owners[0].get('disabled'):
                raise AccountError('传统登录模式必须预置唯一且有效的超级管理员')
            if owners[0].get('pending_activation') and (os.environ.get('SHOP_PUBLIC_MODE') == '1' or os.environ.get('SHOP_SECURITY_MODE') == '1'):
                raise AccountError('请先在本机完成超级管理员初始密码设置，再部署公网服务')
        return
    if not status()["configured"]:
        if bootstrap_allowed():
            return  # Only access.py's verified loopback bootstrap/health routes may pass.
        raise AccountError("安全模式尚未配置管理员，请先在服务器终端运行 tools/security_accounts.py add-user")


def _password_account(username, password, role, *, pending=False):
    if not isinstance(username, str) or not re.fullmatch(r'[\w.@-]{1,80}', username) or username in {'.','..'}:
        raise AccountError('账号限1至80位中文、字母、数字、下划线、点、@或短横线')
    validate_password(username, password)
    return {'password_hash': password_hash(password), 'password_history': [], 'password_changed_at': time.time(),
            'role': role, 'disabled': False, 'totp_secret': '', 'last_totp_counter': -1, 'recovery_hashes': [],
            'auth_method': 'password', 'pending_activation': pending, 'must_change_password': role != 'admin',
            'created_at': time.time(), 'revision': secrets.token_hex(16)}


def provision_owner(username='admin'):
    """OS-authorized provisioning only. The random placeholder can never log in."""
    if not password_only():
        raise AccountError('尚未启用传统登录模式')
    with locked_store() as data:
        if data['users']:
            raise AccountError('已有账号，不能覆盖或重复创建超级管理员')
        placeholder = secrets.token_urlsafe(48) + 'Aa1!'
        data['users'][username] = _password_account(username, placeholder, 'admin', pending=True)
        _save(data)
        _mark_local_enrolled()


def pending_owner():
    if not password_only():
        return None
    with locked_store() as data:
        owners = [(name, u) for name, u in data['users'].items() if u['role'] == 'admin']
        if len(owners) == 1 and len(data['users']) == 1 and owners[0][1].get('pending_activation') and not owners[0][1].get('disabled'):
            return owners[0][0]
    return None


def activate_owner(password):
    if not password_only():
        raise AccountError('超级管理员初始化不可用')
    with locked_store() as data:
        owners = [(name, u) for name, u in data['users'].items() if u['role'] == 'admin']
        if len(owners) != 1 or len(data['users']) != 1 or not owners[0][1].get('pending_activation') or owners[0][1].get('disabled'):
            raise AccountError('初始密码已经设置，不能重复初始化')
        name, account = owners[0]
        validate_password(name, password)
        _replace_password(account, password, time.time())
        account.update(pending_activation=False, must_change_password=False)
        _save(data)


def _managed_owner(data, actor):
    if not password_only():
        raise AccountError('用户管理仅在传统登录模式启用')
    account = data['users'].get(actor, {})
    owners = [u for u in data['users'].values() if u['role'] == 'admin']
    if len(owners) != 1 or account.get('role') != 'admin' or account.get('disabled') or account.get('pending_activation'):
        raise AccountError('只有超级管理员可以管理用户')


def managed_users(actor):
    with locked_store() as data:
        _managed_owner(data, actor)
        return [{'username': name, 'role': u['role'], 'disabled': bool(u.get('disabled')),
                 'mfa_enrolled': bool(u.get('totp_secret')),
                 'must_change_password': bool(u.get('must_change_password')), 'created_at': u.get('created_at')}
                for name, u in data['users'].items()]


def manage_user(actor, username, *, create=False, password=None, role=None, disabled=None):
    with locked_store() as data:
        _managed_owner(data, actor)
        if create:
            if role not in {'operator', 'production', 'shareholder'}:
                raise AccountError('不能创建其他超级管理员，只能创建股东、运营或生产用户')
            if not isinstance(username, str) or any(name.casefold() == username.casefold() for name in data['users']):
                raise AccountError('账号已存在或无效')
            if not isinstance(password, str):
                raise AccountError('请设置初始密码')
            data['users'][username] = _password_account(username, password, role)
        else:
            account = data['users'].get(username)
            if not account:
                raise AccountError('账号不存在')
            if account['role'] == 'admin':
                raise AccountError('超级管理员不可停用、降级或在此重置；本人请使用更改密码')
            if role is not None:
                if role not in {'operator', 'production', 'shareholder'}:
                    raise AccountError('账号角色无效')
                account['role'] = role
            if disabled is not None:
                if type(disabled) is not bool:
                    raise AccountError('账号状态无效')
                account['disabled'] = disabled
            if password is not None:
                validate_password(username, password, account, incident_reset=True)
                _replace_password(account, password, time.time())
                account['must_change_password'] = True
                data['failures'].pop(_failure_keys(username, '')[0], None)
            account['revision'] = secrets.token_hex(16)
        _save(data)
