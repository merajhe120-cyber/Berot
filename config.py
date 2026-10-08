import os
from dataclasses import dataclass


def csv_ints(value: str) -> set[int]:
    out = set()
    for item in (value or '').split(','):
        item = item.strip()
        if item:
            out.add(int(item))
    return out


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_url: str
    admin_ids: set[int]
    webhook_url: str
    webhook_secret: str
    openai_api_key: str | None
    openai_model: str
    bot_name: str


def load_settings() -> Settings:
    token = os.getenv('BOT_TOKEN', '').strip()
    database_url = os.getenv('DATABASE_URL', '').strip()
    if not token:
        raise RuntimeError('BOT_TOKEN is required')
    if not database_url:
        raise RuntimeError('DATABASE_URL is required')
    return Settings(
        bot_token=token,
        database_url=database_url,
        admin_ids=csv_ints(os.getenv('ADMIN_IDS', '')),
        webhook_url=os.getenv('WEBHOOK_URL', '').rstrip('/'),
        webhook_secret=os.getenv('WEBHOOK_SECRET', 'change-me'),
        openai_api_key=os.getenv('OPENAI_API_KEY') or None,
        openai_model=os.getenv('OPENAI_MODEL', 'gpt-5.6-luna'),
        bot_name=os.getenv('BOT_NAME', 'صادق Sadek bot'),
    )
