"""Environment configuration; persisted schedule takes precedence after initialization."""

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


def _ids(name: str) -> frozenset[int]:
    try:
        return frozenset(int(x.strip()) for x in os.getenv(name, "").split(",") if x.strip())
    except ValueError:
        raise ValueError(f"{name} must be comma-separated integer IDs") from None


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = field(default="", repr=False)
    telegram_chat_id: int = 0
    allowed_user_ids: frozenset[int] = frozenset()
    allowed_chat_ids: frozenset[int] = frozenset()
    telegram_profile_user_ids: frozenset[int] = frozenset()
    timezone: str = "America/Toronto"
    database_path: str = "data/teleminder.sqlite"
    mcp_token: str = field(default="", repr=False)
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8765
    digest_start_hour: int = 9
    digest_end_hour: int = 21
    digest_interval_minutes: int = 60
    quiet_start_hour: int = 22
    quiet_end_hour: int = 9
    send_empty_digest: bool = False
    mcp_allowed_hosts: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "Settings":
        empty = os.getenv("SEND_EMPTY_DIGEST", "false").lower()
        if empty not in ("true", "false"):
            raise ValueError("SEND_EMPTY_DIGEST must be true or false")
        owner_raw = os.getenv("TELEGRAM_USER_ID", "").strip()
        owner = None
        if owner_raw:
            try:
                owner = int(owner_raw)
            except ValueError:
                raise ValueError("TELEGRAM_USER_ID must be a positive integer") from None
            if owner == 0:
                owner = None
            if owner is not None and not 0 < owner <= 2**63 - 1:
                raise ValueError("TELEGRAM_USER_ID must be a positive integer")
        settings = cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=owner
            if owner is not None
            else int(os.getenv("TELEGRAM_CHAT_ID", "0")),
            allowed_user_ids=frozenset({owner}) if owner is not None else _ids("ALLOWED_USER_IDS"),
            allowed_chat_ids=frozenset({owner}) if owner is not None else _ids("ALLOWED_CHAT_IDS"),
            telegram_profile_user_ids=_ids("TELEGRAM_PROFILE_USER_IDS"),
            timezone=os.getenv("TIMEZONE", "America/Toronto"),
            database_path=os.getenv("DATABASE_PATH", "data/teleminder.sqlite"),
            mcp_token=os.getenv("MCP_TOKEN", ""),
            mcp_host=os.getenv("MCP_HOST", "127.0.0.1"),
            mcp_port=int(os.getenv("MCP_PORT", "8765")),
            digest_start_hour=int(os.getenv("DIGEST_START_HOUR", "9")),
            digest_end_hour=int(os.getenv("DIGEST_END_HOUR", "21")),
            digest_interval_minutes=int(os.getenv("DIGEST_INTERVAL_MINUTES", "60")),
            quiet_start_hour=int(os.getenv("QUIET_START_HOUR", "22")),
            quiet_end_hour=int(os.getenv("QUIET_END_HOUR", "9")),
            send_empty_digest=(empty == "true"),
            mcp_allowed_hosts=tuple(
                x.strip() for x in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if x.strip()
            ),
        )
        ZoneInfo(settings.timezone)
        if not 1 <= settings.mcp_port <= 65535:
            raise ValueError("MCP_PORT out of range")
        return settings

    def validate_worker(self) -> None:
        if (
            not self.telegram_bot_token.strip()
            or self.telegram_bot_token.strip() != self.telegram_bot_token
            or not self.allowed_user_ids
            or not self.allowed_chat_ids
        ):
            raise ValueError("Worker requires Telegram token and nonempty user AND chat allowlists")
        if 0 in self.allowed_chat_ids or self.telegram_chat_id not in self.allowed_chat_ids:
            raise ValueError("TELEGRAM_CHAT_ID must be in ALLOWED_CHAT_IDS")
        if any(x <= 0 for x in self.allowed_user_ids):
            raise ValueError("Allowed user IDs must be positive")
        if self.telegram_profile_user_ids:
            if any(
                type(user_id) is not int or not 0 < user_id <= 2**63 - 1
                for user_id in self.telegram_profile_user_ids
            ):
                raise ValueError(
                    "Telegram profile user IDs must be positive signed 64-bit integers"
                )
            owner = self.telegram_chat_id
            if (
                owner <= 0
                or self.allowed_user_ids != frozenset({owner})
                or self.allowed_chat_ids != frozenset({owner})
                or owner in self.telegram_profile_user_ids
            ):
                raise ValueError(
                    "Telegram profiles require a distinct owner with private chat access"
                )

    def validate_remote(self) -> None:
        if len(self.mcp_token) < 32 or self.mcp_token.strip() != self.mcp_token:
            raise ValueError(
                "MCP_TOKEN must contain at least 32 characters, without surrounding whitespace"
            )
