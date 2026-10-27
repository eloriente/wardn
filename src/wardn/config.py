"""Configuration: which servers Wardn can inspect, and which model it uses.

The file holds no secrets, only the path to each SSH key.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wardn.tools import ALL_TOOLS

DEFAULT_CONFIG_PATH = Path("~/.config/wardn/config.toml")
DEFAULT_MODEL = "qwen3:4b-instruct"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_AUDIT_LOG = Path("~/.local/state/wardn/audit.jsonl")


class ConfigError(Exception):
    """The configuration file is missing or invalid."""


@dataclass(frozen=True)
class TargetConfig:
    name: str
    host: str
    user: str
    key: Path
    port: int = 22
    tools: tuple[str, ...] | None = None  # None means all tools


@dataclass(frozen=True)
class Config:
    model: str
    ollama_url: str
    audit_log: Path
    targets: dict[str, TargetConfig]
    default_target: str | None = None

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


def _parse_target(name: str, table: Any) -> TargetConfig:
    where = f"[targets.{name}]"
    if not isinstance(table, dict):
        raise ConfigError(f"{where}: must be a table")

    tools = None
    if "tools" in table:
        names = _get(table, "tools", list, where)
        known = {tool.name for tool in ALL_TOOLS}
        unknown = [n for n in names if n not in known]
        if unknown or not all(isinstance(n, str) for n in names):
            raise ConfigError(
                f"{where}: unknown tools {unknown}. Available: {sorted(known)}"
            )
        tools = tuple(names)

    return TargetConfig(
        name=name,
        host=_get(table, "host", str, where),
        user=_get(table, "user", str, where),
        key=Path(_get(table, "key", str, where)).expanduser(),
        port=_get(table, "port", int, where, default=22),
        tools=tools,
    )


def load_config(path: Path | None = None) -> Config:
    path = (path or DEFAULT_CONFIG_PATH).expanduser()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(
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

    return Config(
        model=_get(data, "model", str, str(path), default=DEFAULT_MODEL),
        ollama_url=_get(data, "ollama_url", str, str(path), default=DEFAULT_OLLAMA_URL),
        audit_log=Path(
            _get(data, "audit_log", str, str(path), default=str(DEFAULT_AUDIT_LOG))
        ).expanduser(),
        targets=targets,
        default_target=default_target,
    )
