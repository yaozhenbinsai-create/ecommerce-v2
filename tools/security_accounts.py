"""Interactive account enrollment and offline recovery. Never pass secrets as flags."""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.commerce import secure_auth
from app.security_audit import emit


def _password(username):
    password = getpass.getpass(f"新密码（至少{secure_auth.PASSWORD_MIN_LENGTH}位，大小写/数字/特殊字符，不含用户名）: ")
    if password != getpass.getpass("再次输入密码: "):
        raise secure_auth.AccountError("两次密码不一致，未保存")
    secure_auth.validate_password(username, password)
    return password


def _enroll(username):
    secret = secure_auth.new_totp_secret()
    codes = secure_auth.new_recovery_codes()
    print("\n请在认证器中手工添加账户，类型选择基于时间（TOTP）。")
    print("以下密钥仅在本次交互终端展示。勿截图、录屏或粘贴到聊天；请关闭终端会话录制。")
    print("账户:", username)
    print("认证器设置密钥:", secret)
    print("算法 SHA1，6位，周期30秒。")
    print("如认证器支持 URI 导入:")
    print("otpauth://totp/" + quote("织造台:" + username, safe="") + "?secret=" + secret + "&issuer=" + quote("织造台", safe="") + "&algorithm=SHA1&digits=6&period=30")
    otp = getpass.getpass("请输入认证器当前6位验证码，确认绑定: ").strip()
    if secure_auth.matching_counter(secret, otp) is None:
        raise secure_auth.AccountError("验证码不正确，未保存。请确认认证器与服务器时间一致")
    print("\n一次性恢复码（每个仅能使用一次，请保存到公司批准的密码管理器或离线保管）:")
    for code in codes:
        print(code)
    if input("已安全保存恢复码？输入 SAVE 继续: ").strip() != "SAVE":
        raise secure_auth.AccountError("未确认保存，操作取消")
    # Ask again because manual recovery-code storage can take longer than a TOTP window.
    otp = getpass.getpass("请输入认证器当前6位验证码完成保存: ").strip()
    return secret, otp, codes


def main(argv=None):
    parser = argparse.ArgumentParser(description="织造台安全账号管理（在目标服务器服务账号的交互终端运行）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="仅显示配置状态，不显示用户名或秘密")
    add = sub.add_parser("add-user", help="创建员工独立账号并绑定 MFA")
    add.add_argument("--username", required=True)
    add.add_argument("--role", choices=sorted(secure_auth.ROLES), default="operator")
    password = sub.add_parser("change-password", help="离线更改密码；正常用户也可使用网页更新")
    password.add_argument("--username", required=True)
    password.add_argument("--incident-reset", action="store_true", help="安全事件恢复，允许绕过24小时最短密码期限；会记录审计")
    role = sub.add_parser("set-role", help="调整角色并撤销现有会话")
    role.add_argument("--username", required=True)
    role.add_argument("--role", choices=sorted(secure_auth.ROLES), required=True)
    for command in ("disable", "enable", "reset-mfa"):
        item = sub.add_parser(command, help="需管理员拥有目标服务器与密钥访问权限")
        item.add_argument("--username", required=True)
    args = parser.parse_args(argv)
    if args.command == "status":
        try:
            print(json.dumps(secure_auth.status(), ensure_ascii=False, indent=2))
            return 0
        except secure_auth.security_secrets.SecretStorageError:
            print("安全存储不可用；请检查服务账号和密钥配置", file=sys.stderr)
            return 1
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("账号变更必须在交互终端执行，禁止管道输入或重定向录制秘密", file=sys.stderr)
        return 2
    mutation_done = False
    try:
        emit("auth.account_" + args.command.replace("-", "_") + "_requested", "requested", actor=args.username)
        if args.command == "add-user":
            password = _password(args.username)
            secret, otp, codes = _enroll(args.username)
            secure_auth.add_user(args.username, password, args.role, secret, otp, recovery_codes=codes)
        elif args.command == "change-password":
            if args.incident_reset and input("确认这是已授权的安全事件恢复？输入 RESET: ").strip() != "RESET":
                raise secure_auth.AccountError("已取消")
            secure_auth.update_user(args.username, password=_password(args.username), incident_reset=args.incident_reset)
        elif args.command == "set-role":
            secure_auth.update_user(args.username, role=args.role)
        elif args.command in {"disable", "enable"}:
            secure_auth.update_user(args.username, disabled=args.command == "disable")
        elif args.command == "reset-mfa":
            if input("已核实账号所有者身份并获准恢复 MFA？输入 RESET: ").strip() != "RESET":
                raise secure_auth.AccountError("已取消")
            secret, otp, codes = _enroll(args.username)
            secure_auth.update_user(args.username, totp_secret=secret, enrollment_code=otp, recovery_codes=codes, incident_reset=True)
        mutation_done = True
        emit("auth.account_" + args.command.replace("-", "_"), "incident_reset" if getattr(args, "incident_reset", False) else "success", actor=args.username)
        print("已保存。账号变更会使旧会话失效。首次绑定后请等待下一组验证码再登录。")
        if args.command == "add-user":
            print("本机访问已要求使用账号登录。" if secure_auth.local_required() else "本机登录被配置显式关闭；请先启用本机登录。")
            print("本操作未改变 HTTPS 或公网监听配置；公网部署仍需单独完成证书及安全模式配置。")
        return 0
    except (secure_auth.AccountError, secure_auth.security_secrets.SecretStorageError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("账号已变更，但完成审计写入失败；请管理员核对账号状态并修复审计存储" if mutation_done else "安全审计或账号存储不可用，操作已停止；请检查后重试", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\n已取消", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
