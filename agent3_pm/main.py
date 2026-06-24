import asyncio
import logging
import uvicorn
from telegram import Bot

from agent3_pm.config import config
from agent3_pm.database import init_db
from agent3_pm.bot import create_bot_application, set_scheduler
from agent3_pm.scheduler import create_scheduler
from agent3_pm.web import app as fastapi_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    logger.info("Initializing database...")
    await init_db()

    logger.info("Starting Telegram bot...")
    bot_app = create_bot_application()
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)

    logger.info("Starting scheduler...")
    scheduler = create_scheduler(bot)
    set_scheduler(scheduler)
    scheduler.start()

    logger.info("All systems ready. Running bot polling...")
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()

    logger.info("Starting web server on %s:%s", config.WEB_HOST, config.WEB_PORT)
    uvi_config = uvicorn.Config(
        fastapi_app, host=config.WEB_HOST, port=config.WEB_PORT, log_level="info",
    )
    server = uvicorn.Server(uvi_config)

    try:
        await server.serve()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        logger.info("Shutting down...")
        scheduler.shutdown(wait=False)
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
