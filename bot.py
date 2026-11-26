import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand

import db
import notifier
from config import BOT_TOKEN
from handlers import router


async def main():
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise SystemExit("Вкажи BOT_TOKEN у .env")
    await db.init()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML", link_preview_is_disabled=True))
    dp = Dispatcher()

    @dp.update.outer_middleware()
    async def track_activity(handler, event, data):
        """Кожне оновлення від людини — відмітка «був активний» (для /stats)."""
        if user := data.get("event_from_user"):
            if not user.is_bot:
                try:
                    await db.touch_user(user.id)
                except Exception:
                    logging.getLogger(__name__).exception("touch_user failed")
        return await handler(event, data)

    dp.include_router(router)
    await bot.set_my_commands([BotCommand(command="menu", description="Головне меню")])
    asyncio.create_task(notifier.loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
