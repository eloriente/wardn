"""Configuration: which servers Wardn can inspect, and which model it uses.

One file holds everything, secrets included: a token can be written in it
(`token = "..."`), or kept in a file of its own (`token_file = "..."`). A config
with a token in it is refused unless only its owner can read it. SSH keys stay
files, and the config holds their path.
"""

import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wardn.proxmox import TOKEN as PROXMOX_TOKEN
from wardn.proxmox import load_token as load_proxmox_token
from wardn.redact import RedactionRules
from wardn.telegram import TOKEN as TELEGRAM_TOKEN
from wardn.telegram import load_token as load_telegram_token
from wardn.tools import ALL_TOOLS

DEFAULT_CONFIG_PATH = Path("~/.config/wardn/config.toml")
DEFAULT_MODEL = "qwen3:4b-instruct"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_AUDIT_LOG = Path("~/.local/state/wardn/audit.jsonl")
GUEST_STATUS = "guest_status"  # built at run time from the proxmox section


class ConfigError(Exception):
    """The configuration file is missing or invalid."""


class ConfigNotFound(ConfigError):
    """There is no configuration file at the expected path."""


@dataclass(frozen=True)
class ProxmoxConfig:
    url: str
    node: str
    # The API token, written here or in a file of its own. Never in the repr: a
    # traceback or a log line would show it.
    token: str | None = field(default=None, repr=False)
    token_file: Path | None = None
    ca_file: Path | None = None  # CA that signed the Proxmox certificate

    def read_token(self) -> str:
        if self.token is not None:
            return self.token
        assert self.token_file is not None  # the parser requires one of the two
        return load_proxmox_token(self.token_file)


@dataclass(frozen=True)
class TelegramConfig:
    allowed_users: tuple[int, ...]  # Telegram user ids; nobody else gets an answer
    token: str | None = field(default=None, repr=False)  # the bot token
    token_file: Path | None = None

    def read_token(self) -> str:
        if self.token is not None:
            return self.token
        assert self.token_file is not None  # the parser requires one of the two
        return load_telegram_token(self.token_file)


@dataclass(frozen=True)
class TargetConfig:
    name: str
    host: str
    user: str
    key: Path
    port: int = 22
    tools: tuple[str, ...] | None = None  # None means all tools
    proxmox: ProxmoxConfig | None = None  # enables the guest_status tool
    summary: bool = True  # whether `wardn summary` includes this target


@dataclass(frozen=True)
class Config:
    model: str
    ollama_url: str
    audit_log: Path
    targets: dict[str, TargetConfig]
    default_target: str | None = None
    redact: RedactionRules = field(default_factory=RedactionRules)
    telegram: TelegramConfig | None = None

    def select_target(self, name: str | None) -> TargetConfig:
        name = name or self.default_target
        if name is None and len(self.targets) == 1:
            name = next(iter(self.targets))
        if name is None:
            raise ConfigError(
                f"several targets defined, choose one with --target: {sorted(self.targets)}"
            )
        if name not in self.targets:
            raise ConfigError(
                f"unknown target {name!r}. Available: {sorted(self.targets)}"
            )
        return self.targets[name]


def _get(
    table: dict[str, Any], key: str, kind: type, where: str, default: Any = None
) -> Any:
    if key not in table:
        if default is None:
            raise ConfigError(f"{where}: missing '{key}'")
        return default
    value = table[key]
    if not isinstance(value, kind) or isinstance(value, bool):
        raise ConfigError(f"{where}: '{key}' must be of type {kind.__name__}")
    return value


def _parse_token(
    table: dict[str, Any], where: str, pattern: re.Pattern[str], shape: str
) -> tuple[str | None, Path | None]:
    """A token written in the config, or the path of a file that holds it."""
    token = table.get("token")
    token_file = table.get("token_file")
    if (token is None) == (token_file is None):
        raise ConfigError(f"{where}: set 'token' or 'token_file', one of them")
    if token is not None:
        if not isinstance(token, str) or not pattern.fullmatch(token.strip()):
            raise ConfigError(f"{where}: 'token' must look like {shape}")
        return token.strip(), None
    if not isinstance(token_file, str):
        raise ConfigError(f"{where}: 'token_file' must be of type str")
    return None, Path(token_file).expanduser()


def _parse_proxmox(target: str, table: Any) -> ProxmoxConfig:
    where = f"[targets.{target}.proxmox]"
    if not isinstance(table, dict):
        raise ConfigError(f"{where}: must be a table")
    url = _get(table, "url", str, where)
    if not url.startswith("https://"):
        raise ConfigError(f"{where}: 'url' must start with https://")
    ca_file = _get(table, "ca_file", str, where, default="")
    token, token_file = _parse_token(
        table, where, PROXMOX_TOKEN, "user@realm!tokenid=secret"
    )
    return ProxmoxConfig(
        url=url,
        node=_get(table, "node", str, where),
        token=token,
        token_file=token_file,
        ca_file=Path(ca_file).expanduser() if ca_file else None,
    )


def _parse_target(name: str, table: Any) -> TargetConfig:
    where = f"[targets.{name}]"
    if not isinstance(table, dict):
        raise ConfigError(f"{where}: must be a table")

    proxmox = _parse_proxmox(name, table["proxmox"]) if "proxmox" in table else None

    tools = None
    if "tools" in table:
        names = _get(table, "tools", list, where)
        known = {tool.name for tool in ALL_TOOLS} | (
            {GUEST_STATUS} if proxmox else set()
        )
        unknown = [n for n in names if n not in known]
        if unknown or not all(isinstance(n, str) for n in names):
            hint = (
                ""
                if proxmox
                else f" ({GUEST_STATUS} needs a [targets.{name}.proxmox] section)"
            )
            raise ConfigError(
                f"{where}: unknown tools {unknown}. Available: {sorted(known)}{hint}"
            )
        tools = tuple(names)

    summary = table.get("summary", True)
    if not isinstance(summary, bool):
        raise ConfigError(f"{where}: 'summary' must be true or false")

    return TargetConfig(
        name=name,
        host=_get(table, "host", str, where),
        user=_get(table, "user", str, where),
        key=Path(_get(table, "key", str, where)).expanduser(),
        port=_get(table, "port", int, where, default=22),
        tools=tools,
        proxmox=proxmox,
        summary=summary,
    )


def _parse_redact(table: Any) -> RedactionRules:
    where = "[redact]"
    if not isinstance(table, dict):
        raise ConfigError(f"{where}: must be a table")
    lists = {}
    for key in ("hostnames", "users", "paths"):
        values = table.get(key, [])
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ConfigError(f"{where}: '{key}' must be a list of strings")
        lists[key] = tuple(values)
    return RedactionRules(**lists)


def _parse_telegram(table: Any) -> TelegramConfig:
    where = "[telegram]"
    if not isinstance(table, dict):
        raise ConfigError(f"{where}: must be a table")
    users = _get(table, "allowed_users", list, where)
    if not users or not all(
        isinstance(u, int) and not isinstance(u, bool) and u > 0 for u in users
    ):
        raise ConfigError(
            f"{where}: 'allowed_users' must list your numeric Telegram user id"
        )
    token, token_file = _parse_token(
        table, where, TELEGRAM_TOKEN, "the token @BotFather gives you"
    )
    return TelegramConfig(
        allowed_users=tuple(users), token=token, token_file=token_file
    )


def load_config(path: Path | None = None) -> Config:
    path = (path or DEFAULT_CONFIG_PATH).expanduser()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigNotFound(
            f"config not found at {path}. Copy config.example.toml there and edit it."
        ) from None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc

    raw_targets = data.get("targets")
    if not isinstance(raw_targets, dict) or not raw_targets:
        raise ConfigError(f"{path}: define at least one [targets.<name>]")
    targets = {name: _parse_target(name, table) for name, table in raw_targets.items()}

    default_target = data.get("default_target")
    if default_target is not None and default_target not in targets:
        raise ConfigError(
            f"{path}: default_target {default_target!r} is not a defined target"
        )

    config = Config(
        model=_get(data, "model", str, str(path), default=DEFAULT_MODEL),
        ollama_url=_get(data, "ollama_url", str, str(path), default=DEFAULT_OLLAMA_URL),
        audit_log=Path(
            _get(data, "audit_log", str, str(path), default=str(DEFAULT_AUDIT_LOG))
        ).expanduser(),
        targets=targets,
        default_target=default_target,
        redact=_parse_redact(data.get("redact", {})),
        telegram=_parse_telegram(data["telegram"]) if "telegram" in data else None,
    )
    if _holds_secrets(config):
        _require_private(path)
    return config


def _holds_secrets(config: Config) -> bool:
    proxmox = [t.proxmox for t in config.targets.values() if t.proxmox]
    tokens = [p.token for p in proxmox] + [config.telegram and config.telegram.token]
    return any(tokens)


def _require_private(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ConfigError(
            f"{path} holds a token and can be read by other users. Run: chmod 600 {path}"
        )
