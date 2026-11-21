"""Initialize an external POSIX vault key; never display or overwrite a key."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.security_secrets import SecretStorageError, _private_path


def create_key(path: Path, data_dir: Path) -> None:
    if os.name == "nt":
        raise SecretStorageError("Windows 使用服务账户的 DPAPI，不需要创建 Linux 主密钥")
    if not path.is_absolute() or not data_dir.is_absolute():
        raise SecretStorageError("密钥和数据目录必须使用绝对路径")
    key_dir, data_dir = path.parent.resolve(), data_dir.resolve()
    if key_dir == data_dir or key_dir.is_relative_to(data_dir) or data_dir.is_relative_to(key_dir):
        raise SecretStorageError("主密钥目录与数据目录必须独立且互不包含")
    if path.exists() or path.is_symlink():
        raise SecretStorageError("目标已经存在，拒绝覆盖或轮换现有主密钥")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private_path(path.parent, directory=True)
    if data_dir.exists():
        _private_path(data_dir, directory=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        with os.fdopen(os.open(path, flags, 0o600), "wb") as stream:
            created = True
            stream.write(os.urandom(32))
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        if created:
            path.unlink(missing_ok=True)
        raise SecretStorageError("主密钥创建失败，请核对目录与服务账户权限") from None


def main() -> int:
    parser = argparse.ArgumentParser(description="交互式创建独立的 Linux 凭据主密钥，不显示密钥内容")
    parser.add_argument("--path", type=Path, required=True, help="独立目录中的新主密钥绝对路径")
    parser.add_argument("--data-dir", type=Path, required=True, help="应用 ECOMMERCE2_DATA_DIR 的绝对路径")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        print("请由服务账户在交互式终端中执行；禁止无人值守初始化。", file=sys.stderr)
        return 2
    print("请以运行应用的同一服务账户执行。主密钥丢失会导致已保存的凭据无法恢复。")
    if input("输入 CREATE 创建全新主密钥（不会覆盖已有文件）：").strip() != "CREATE":
        print("已取消。")
        return 1
    try:
        create_key(args.path, args.data_dir)
    except SecretStorageError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print("主密钥已安全创建，未显示其内容。请将 SHOP_SECRET_KEY_FILE 设置为此文件的绝对路径。")
    print("请通过受控的加密备份保管主密钥，与数据备份分开；勿上传 Git 或覆盖此文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
