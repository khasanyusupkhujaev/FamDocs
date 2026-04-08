from aiogram import Router

from bot.handlers import admin, billing, vault


def setup_routers() -> Router:
    root = Router()
    root.include_router(admin.router)
    root.include_router(billing.router)
    root.include_router(vault.router)
    return root
