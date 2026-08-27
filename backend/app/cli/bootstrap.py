from __future__ import annotations

import argparse
import asyncio
import getpass

from app.container import build_container
from app.core.config import get_settings
from app.database import Database
from app.schemas.auth import SetupRequest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="交互式创建首个本机系统管理员（密码不会出现在命令行中）"
    )
    parser.add_argument("--username", default="admin")
    parser.add_argument("--display-name", default="Platform Admin")
    return parser.parse_args()


async def run() -> None:
    args = parse_args()
    password = getpass.getpass("请输入本机管理员密码（12-128 字符）: ")
    confirmation = getpass.getpass("请再次输入密码: ")
    if password != confirmation:
        raise SystemExit("两次输入的密码不一致")

    settings = get_settings()
    settings.validate_local_safety()
    if not settings.local_only:
        raise SystemExit("bootstrap 只允许 LOCAL_ONLY=true")
    database = Database(
        settings.database_url,
        runtime_mode=settings.database_runtime_mode,
        app_env=settings.app_env,
    )
    container = build_container(database, settings)
    try:
        await container.initialize()
        issued = await container.identity.setup(
            SetupRequest(
                username=args.username,
                display_name=args.display_name,
                password=password,
            )
        )
        print(f"已创建本机管理员: {issued.principal.username}")
        print("密码和 Session Token 均未输出；请通过 /auth/login 登录。")
    finally:
        await container.shutdown()


if __name__ == "__main__":
    asyncio.run(run())
