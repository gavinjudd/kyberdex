"""Explicit configuration with secrets supplied only through a protected file."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

PROFILE_KEYS = frozenset({
    "VAULT_DATABASE", "DISCORD_TOKEN_FILE", "VAULT_GUILD_ID", "VAULT_SPAWN_INTERVAL", "VAULT_DROP_DURATION",
    "VAULT_PUBLIC_URL", "VAULT_ENABLED_EVENTS", "VAULT_ENABLED_RELEASES", "VAULT_ACTIVITY_CHANNEL_IDS",
})


def profile_directory(root: Path) -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")).expanduser().absolute()
    identity = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:32]
    directory = base / "kyberdex" / "profiles" / identity
    if directory.is_relative_to(root.resolve()):
        raise ValueError("KyberDex configuration must be stored outside the checkout; change XDG_CONFIG_HOME.")
    return directory


def private_file(path: Path):
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Saved configuration and credentials cannot use symlinks.")
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)):
        raise ValueError("Saved configuration and credentials must be owned private regular files (mode 0600).")


def load_profile(root: Path) -> dict:
    path = profile_directory(root) / "settings.json"
    if not path.exists() and not path.is_symlink():
        return {}
    private_file(path)
    if path.stat().st_size > 32 * 1024:
        raise ValueError("Saved KyberDex configuration is oversized.")
    document = json.loads(path.read_text())
    if (not isinstance(document, dict) or document.get("version") != 1
            or document.get("root") != str(root.resolve()) or not isinstance(document.get("settings"), dict)
            or not set(document["settings"]).issubset(PROFILE_KEYS)
            or any(not isinstance(value, str) for value in document["settings"].values())):
        raise ValueError("Saved KyberDex configuration is invalid or belongs to a different checkout.")
    return document


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
        saved = load_profile(root).get("settings", {})
        values = {**saved, **{key: os.environ[key] for key in PROFILE_KEYS if key in os.environ}}
        token_path = values.get("DISCORD_TOKEN_FILE", "")
        interval = int(values.get("VAULT_SPAWN_INTERVAL", "900"))
        duration = int(values.get("VAULT_DROP_DURATION", "180"))
        if not 300 <= interval <= 86400:
            raise ValueError("VAULT_SPAWN_INTERVAL must be between 300 and 86400 seconds.")
        if not 60 <= duration <= 900:
            raise ValueError("VAULT_DROP_DURATION must be between 60 and 900 seconds.")
        if duration > interval:
            raise ValueError("VAULT_DROP_DURATION cannot exceed VAULT_SPAWN_INTERVAL.")
        guild_value = values.get("VAULT_GUILD_ID", "").strip()
        guild_id = int(guild_value) if guild_value else None
        if guild_id is not None and guild_id <= 0:
            raise ValueError("VAULT_GUILD_ID must be a positive Discord server ID.")
        channels = values.get("VAULT_ACTIVITY_CHANNEL_IDS")
        activity_channels = None if channels is None else tuple(dict.fromkeys(
            int(value.strip()) for value in channels.split(",") if value.strip()))
        if activity_channels is not None and any(value <= 0 for value in activity_channels):
            raise ValueError("VAULT_ACTIVITY_CHANNEL_IDS must contain positive Discord channel IDs.")
        return cls(root, Path(values.get("VAULT_DATABASE", root / "state/vault.sqlite3")),
                   root / "data/cards.json", Path(token_path) if token_path else None,
                   guild_id, interval, duration,
                   values.get("VAULT_PUBLIC_URL", "").rstrip("/"),
                   tuple(key.strip() for key in values.get("VAULT_ENABLED_EVENTS", "").split(",") if key.strip()),
                   tuple(key.strip() for key in values.get("VAULT_ENABLED_RELEASES", "").split(",") if key.strip()),
                   activity_channels)

    def read_token(self) -> str:
        if not self.token_file:
            raise ValueError("DISCORD_TOKEN_FILE must name the dedicated bot's protected token file.")
        private_file(self.token_file)
        token = self.token_file.read_text().strip()
        if len(token) < 30 or any(char.isspace() for char in token):
            raise ValueError("The bot token file does not contain a valid token value.")
        return token
