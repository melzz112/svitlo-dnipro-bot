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
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_my_commands([BotCommand(command="menu", description="Головне меню")])
    asyncio.create_task(notifier.loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
