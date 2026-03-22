"""Centralized configuration for CarefulClaudeClaw.

Reads all settings from environment variables (or a .env file in the project root).
"""

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Telegram ---
    telegram_bot_token: str = ""
    telegram_chat_id: int = 0

    # --- Slack ---
    slack_bot_token: str = ""
    slack_app_token: str = ""
    # Optional: restrict to messages from this Slack user ID only (recommended)
    slack_allowed_user_id: str = ""

    # --- Paths ---
    mcp_telegram_cli: Path = Path("tg")

    @field_validator("telegram_chat_id", mode="before")
    @classmethod
    def parse_chat_id(cls, v: object) -> object:
        if v == "" or v is None:
            return 0
        return v

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def slack_configured(self) -> bool:
        return bool(self.slack_bot_token and self.slack_app_token)


settings = Settings()
