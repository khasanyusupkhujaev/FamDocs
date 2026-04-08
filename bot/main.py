import asyncio
import logging

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import MenuButtonWebApp, WebAppInfo

from bot.config import (
    BOT_TOKEN,
    STORAGE_BACKEND,
    STORAGE_DIR,
    WEBAPP_HOST,
    WEBAPP_MENU_BUTTON_TEXT,
    WEBAPP_PORT,
    WEBAPP_PUBLIC_URL,
)
from bot.branding import load_branding_assets
from bot.db import init_db
from bot.handlers import setup_routers
from bot.handlers.admin import resolve_admin_users
from bot.webapp_server import create_webapp_app


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger(__name__)
    log.info(
        "Blob storage backend: %s (set FAMDOC_STORAGE=local to force local files)",
        STORAGE_BACKEND,
    )
    if STORAGE_BACKEND == "local":
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    await init_db()
    await load_branding_assets()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await resolve_admin_users(bot)

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(setup_routers())

    if WEBAPP_PUBLIC_URL:
        mini_url = f"{WEBAPP_PUBLIC_URL.rstrip('/')}/"
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text=WEBAPP_MENU_BUTTON_TEXT[:64],
                web_app=WebAppInfo(url=mini_url),
            )
        )
        logging.getLogger(__name__).info(
            "Mini App menu URL: %s (WEBAPP_PORT=%s)", mini_url, WEBAPP_PORT
        )
    else:
        logging.getLogger(__name__).warning(
            "WEBAPP_PUBLIC_URL is not set — no Mini App menu or Web App keyboard. "
            "Set it to your HTTPS URL (e.g. ngrok) and restart."
        )

    web_app = create_webapp_app()
    web_cfg = uvicorn.Config(
        web_app,
        host=WEBAPP_HOST,
        port=WEBAPP_PORT,
        log_level="info",
    )
    server = uvicorn.Server(web_cfg)

    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.gather(server.serve(), dp.start_polling(bot))


if __name__ == "__main__":
    asyncio.run(main())
