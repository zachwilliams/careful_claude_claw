"""Centralized configuration for CarefulClaudeClaw.

Reads all settings from environment variables (or a .env file in the project root).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Slack ---
    slack_bot_token: str = ""
    slack_app_token: str = ""
    # Optional: restrict to messages from this Slack user ID only (recommended)
    slack_allowed_user_id: str = ""

    @property
    def slack_configured(self) -> bool:
        return bool(self.slack_bot_token and self.slack_app_token)


settings = Settings()
