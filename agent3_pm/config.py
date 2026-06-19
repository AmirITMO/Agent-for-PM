import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Database
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/agent3_pm"
    )
    DATABASE_URL_SYNC: str = os.getenv(
        "DATABASE_URL_SYNC", "postgresql://postgres:postgres@localhost:5432/agent3_pm"
    )

    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

    # Web
    WEB_HOST: str = os.getenv("WEB_HOST", "0.0.0.0")
    WEB_PORT: int = int(os.getenv("WEB_PORT", "8080"))
    WEB_BASE_URL: str = os.getenv("WEB_BASE_URL", "http://localhost:8080")
    SECRET_KEY: str = os.getenv("SECRET_KEY", "change-me-in-production")

    # Notifications
    MORNING_SUMMARY_HOUR: int = int(os.getenv("MORNING_SUMMARY_HOUR", "9"))
    MORNING_SUMMARY_MINUTE: int = int(os.getenv("MORNING_SUMMARY_MINUTE", "0"))
    DEADLINE_CHECK_INTERVAL_MINUTES: int = int(os.getenv("DEADLINE_CHECK_INTERVAL_MINUTES", "30"))
    DEADLINE_WARNING_HOURS: int = int(os.getenv("DEADLINE_WARNING_HOURS", "24"))

    # Admin telegram IDs (comma-separated)
    ADMIN_TELEGRAM_IDS: list[int] = field(default_factory=list)

    TIMEZONE: str = os.getenv("TIMEZONE", "Europe/Moscow")

    def __post_init__(self):
        raw = os.getenv("ADMIN_TELEGRAM_IDS", "")
        if raw and not self.ADMIN_TELEGRAM_IDS:
            self.ADMIN_TELEGRAM_IDS = [int(x.strip()) for x in raw.split(",") if x.strip()]


config = Config()
