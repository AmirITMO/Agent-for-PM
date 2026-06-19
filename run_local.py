"""
Local test launcher.
1. Starts PostgreSQL via docker
2. Seeds sample data
3. Starts web interface on http://localhost:8080
4. Starts Telegram bot (if token provided)

Usage:
    python run_local.py                    # web only (no bot)
    python run_local.py --bot-token=XXX    # web + bot
"""
import asyncio
import argparse
import logging
import os
import sys
import threading
import subprocess
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("run_local")


def wait_for_postgres(max_wait=30):
    """Wait until PostgreSQL is accepting connections."""
    import psycopg2
    dsn = os.environ.get("DATABASE_URL_SYNC", "postgresql://postgres:postgres@localhost:5432/agent3_pm")
    for i in range(max_wait):
        try:
            conn = psycopg2.connect(dsn)
            conn.close()
            return True
        except Exception:
            time.sleep(1)
    return False


def ensure_postgres():
    """Start postgres via docker if not already running."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=agent3-postgres", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=5,
        )
        if "agent3-postgres" in result.stdout:
            logger.info("PostgreSQL container already running")
            return True
    except Exception:
        pass

    logger.info("Starting PostgreSQL via docker...")
    subprocess.run([
        "docker", "run", "-d",
        "--name", "agent3-postgres",
        "-e", "POSTGRES_DB=agent3_pm",
        "-e", "POSTGRES_USER=postgres",
        "-e", "POSTGRES_PASSWORD=postgres",
        "-p", "5432:5432",
        "postgres:16-alpine",
    ], check=True)

    logger.info("Waiting for PostgreSQL to be ready...")
    if not wait_for_postgres():
        logger.error("PostgreSQL did not start in time")
        sys.exit(1)
    logger.info("PostgreSQL is ready!")
    return True


async def run_seed():
    from agent3_pm.database import init_db, AsyncSessionLocal
    from agent3_pm import repository as repo
    from sqlalchemy import text

    await init_db()

    async with AsyncSessionLocal() as session:
        result = await session.execute(text("SELECT COUNT(*) FROM users"))
        count = result.scalar()
        if count > 0:
            logger.info(f"Database already has {count} users, skipping seed")
            return

    logger.info("Seeding sample data...")
    from agent3_pm.seed import seed
    await seed()


async def run_web():
    import uvicorn
    from agent3_pm.web import app
    config = uvicorn.Config(app, host="0.0.0.0", port=8080, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def run_bot(token: str):
    from agent3_pm.bot import create_bot_application
    os.environ["TELEGRAM_BOT_TOKEN"] = token

    from agent3_pm.config import Config
    import agent3_pm.config as cfg
    cfg.config = Config()

    app = create_bot_application()
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info("Telegram bot started!")
    await asyncio.Event().wait()


async def main(bot_token: str | None = None):
    ensure_postgres()
    await run_seed()

    logger.info("=" * 50)
    logger.info("Web tracker: http://localhost:8080")
    logger.info("Admin board: http://localhost:8080/admin")
    logger.info("=" * 50)

    if bot_token:
        await asyncio.gather(run_web(), run_bot(bot_token))
    else:
        logger.info("No bot token provided — running web only")
        logger.info("To add bot: python run_local.py --bot-token=YOUR_TOKEN")
        await run_web()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot-token", default=None, help="Telegram bot token")
    args = parser.parse_args()
    asyncio.run(main(args.bot_token))
