"""Explicit configuration with secrets supplied only through a protected file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    root: Path
    database: Path
    catalog: Path
    token_file: Path | None
    guild_id: int | None
    spawn_interval: int = 900
    drop_duration: int = 180
    vault_url: str = ""
    enabled_events: tuple[str, ...] = ()
    enabled_releases: tuple[str, ...] = ()
    activity_channels: tuple[int, ...] | None = None

    @property
    def activity_channel_ids(self):
        return self.activity_channels or ()

    def require_discord_configuration(self):
        if type(self.guild_id) is not int or self.guild_id <= 0:
            raise ValueError("Set VAULT_GUILD_ID to your Discord server ID before starting the bot.")

    @classmethod
    def from_env(cls) -> Settings:
        root = Path(os.environ.get("VAULT_ROOT", Path(__file__).resolve().parents[2])).resolve()
        token_path = os.environ.get("DISCORD_TOKEN_FILE", "")
        interval = int(os.environ.get("VAULT_SPAWN_INTERVAL", "900"))
        duration = int(os.environ.get("VAULT_DROP_DURATION", "180"))
        if not 300 <= interval <= 86400:
            raise ValueError("VAULT_SPAWN_INTERVAL must be between 300 and 86400 seconds.")
        if not 60 <= duration <= 900:
            raise ValueError("VAULT_DROP_DURATION must be between 60 and 900 seconds.")
        if duration > interval:
            raise ValueError("VAULT_DROP_DURATION cannot exceed VAULT_SPAWN_INTERVAL.")
        guild_value = os.environ.get("VAULT_GUILD_ID", "").strip()
        guild_id = int(guild_value) if guild_value else None
        if guild_id is not None and guild_id <= 0:
            raise ValueError("VAULT_GUILD_ID must be a positive Discord server ID.")
        channels = os.environ.get("VAULT_ACTIVITY_CHANNEL_IDS")
        activity_channels = None if channels is None else tuple(dict.fromkeys(
            int(value.strip()) for value in channels.split(",") if value.strip()))
        if activity_channels is not None and any(value <= 0 for value in activity_channels):
            raise ValueError("VAULT_ACTIVITY_CHANNEL_IDS must contain positive Discord channel IDs.")
        return cls(root, Path(os.environ.get("VAULT_DATABASE", root / "state/vault.sqlite3")),
                   root / "data/cards.json", Path(token_path) if token_path else None,
                   guild_id, interval, duration,
                   os.environ.get("VAULT_PUBLIC_URL", "").rstrip("/"),
                   tuple(key.strip() for key in os.environ.get("VAULT_ENABLED_EVENTS", "").split(",") if key.strip()),
                   tuple(key.strip() for key in os.environ.get("VAULT_ENABLED_RELEASES", "").split(",") if key.strip()),
                   activity_channels)

    def read_token(self) -> str:
        if not self.token_file:
            raise ValueError("DISCORD_TOKEN_FILE must name the dedicated bot's protected token file.")
        info = self.token_file.stat()
        if info.st_mode & 0o077:
            raise ValueError("Bot token file must not be readable by other users (use mode 0600).")
        token = self.token_file.read_text().strip()
        if len(token) < 30 or any(char.isspace() for char in token):
            raise ValueError("The bot token file does not contain a valid token value.")
        return token
