from aiogram import Router

from bot.handlers import billing, vault


def setup_routers() -> Router:
    root = Router()
    root.include_router(billing.router)
    root.include_router(vault.router)
    return root
