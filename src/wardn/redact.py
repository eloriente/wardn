"""Redaction: hide sensitive data from the model and from the user's screen.

Best effort. The patterns catch common formats, and cannot promise to catch
everything. The stronger controls are that the tools are read-only and
limited, and that command output is never stored.

Placeholders are stable within a conversation (the same IP is always <ip-1>),
so the model can still reason about "the same address" without knowing it.
"""

import ipaddress
import re
from collections import Counter
from dataclasses import dataclass

from wardn.audit import AuditLog, timestamp

SECRET = "[REDACTED:secret]"

# Always hidden, on top of the paths in the rules. Data disks (/mnt/...) stay
# visible: they are the content of disk_usage.
DEFAULT_SENSITIVE_PATHS = (
    "/home",
    "/root",
    "/etc",
    "/var/lib",
    "/opt",
    "/srv",
    "/usr/local",
)

_PATH_CHARS = r"[^\s'\"`<>|;,:()\[\]]"

PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
URL_CREDENTIALS = re.compile(r"([a-zA-Z][\w+.-]*://)[^/\s:@]+:[^/\s@]+@")
KEY_VALUE = re.compile(
    r"""(\b(?:pass(?:word|wd)?|secret|token|api[_-]?key|access[_-]?key|private[_-]?key
        |credentials?|auth(?:orization)?)\b["']?\s*[=:]\s*)
        (?:(?:bearer|basic)\s+)?("[^"]*"|'[^']*'|\S+)""",
    re.IGNORECASE | re.VERBOSE,
)
CLI_FLAG = re.compile(
    r"(--?(?:pass(?:word|wd)?|secret|token|api[_-]?key)[= ]+)(\S+)", re.IGNORECASE
)
BEARER = re.compile(r"\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
KNOWN_TOKENS = re.compile(
    r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"  # AWS access key
    r"|\bgh[pousr]_[A-Za-z0-9]{36,}\b"  # GitHub
    r"|\bxox[baprs]-[A-Za-z0-9-]{10,}"  # Slack
    r"|\bsk-[A-Za-z0-9]{20,}\b"  # API keys of the sk- kind
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"  # JWT
)
# Long strings that mix letters and digits look like keys, hashes or tokens.
LONG_RANDOM = re.compile(
    r"(?<![\w-])(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{40,}(?![\w-])"
)
SSH_PATH = re.compile(
    rf"{_PATH_CHARS}*\.ssh/{_PATH_CHARS}*|\bid_(?:rsa|ed25519|ecdsa)\w*"
)
IPV4 = re.compile(
    r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?!\w|\.\d)(?:/\d{1,2})?(?::\d{1,5})?"
)
IPV6_CANDIDATE = re.compile(r"(?<![\w:.])[0-9A-Fa-f:]{3,45}(?![\w:])")
MAC = re.compile(r"(?<![\w:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![\w:-])")


@dataclass(frozen=True)
class RedactionRules:
    hostnames: tuple[str, ...] = ()
    users: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()  # extra sensitive prefixes


def _names_pattern(names: tuple[str, ...]) -> re.Pattern[str] | None:
    names = tuple(sorted({n for n in names if n}, key=len, reverse=True))
    if not names:
        return None
    alternatives = "|".join(re.escape(n) for n in names)
    return re.compile(rf"(?<![\w.-])(?:{alternatives})(?![\w-])", re.IGNORECASE)


class Redactor:
    def __init__(self, rules: RedactionRules | None = None):
        rules = rules or RedactionRules()
        prefixes = sorted(
            {
                p.rstrip("/")
                for p in (*DEFAULT_SENSITIVE_PATHS, *rules.paths)
                if p.rstrip("/")
            },
            key=len,
            reverse=True,
        )
        alternatives = "|".join(re.escape(p) for p in prefixes)
        self._paths = re.compile(
            rf"(?<![\w/.-])(?:{alternatives})(?:/{_PATH_CHARS}*)?(?![\w-])"
        )
        self._hostnames = _names_pattern(rules.hostnames)
        self._users = _names_pattern(rules.users)
        self._aliases: dict[tuple[str, str], str] = {}
        self.counts: Counter[str] = Counter()

    def reset(self) -> None:
        """Forget the aliases. Call it when a new conversation starts."""
        self._aliases.clear()
        self.counts.clear()

    def drain(self) -> Counter[str]:
        """What was hidden since the last call, by category."""
        found, self.counts = self.counts, Counter()
        return found

    def _hide(self, kind: str, value: str) -> str:
        key = (kind, value.lower() if kind in ("host", "user") else value)
        if key not in self._aliases:
            number = sum(1 for k in self._aliases if k[0] == kind) + 1
            self._aliases[key] = f"<{kind}-{number}>"
        self.counts[kind] += 1
        return self._aliases[key]

    def _secret(self, _match: re.Match[str]) -> str:
        self.counts["secret"] += 1
        return SECRET

    def redact(self, text: str) -> str:
        count = self._secret

        text = PRIVATE_KEY.sub(count, text)
        text = URL_CREDENTIALS.sub(lambda m: m.group(1) + SECRET + "@", text)
        text = KEY_VALUE.sub(lambda m: self._keep(m.group(1)), text)
        text = CLI_FLAG.sub(lambda m: self._keep(m.group(1)), text)
        text = BEARER.sub(lambda m: self._keep(m.group(1) + " "), text)
        text = KNOWN_TOKENS.sub(count, text)

        text = self._paths.sub(lambda m: self._hide("path", m.group(0)), text)
        text = SSH_PATH.sub(lambda m: self._hide("path", m.group(0)), text)

        text = IPV4.sub(self._ipv4, text)
        text = IPV6_CANDIDATE.sub(self._ipv6, text)
        text = MAC.sub(lambda m: self._hide("mac", m.group(0).lower()), text)

        if self._hostnames:
            text = self._hostnames.sub(lambda m: self._hide("host", m.group(0)), text)
        if self._users:
            text = self._users.sub(lambda m: self._hide("user", m.group(0)), text)

        return LONG_RANDOM.sub(count, text)

    def _keep(self, prefix: str) -> str:
        """Keep the key (`password=`) and hide only the value."""
        self.counts["secret"] += 1
        return prefix + SECRET

    def _ipv4(self, match: re.Match[str]) -> str:
        try:
            address = ipaddress.IPv4Address(match.group(1))
        except ValueError:
            return match.group(0)
        if address.is_loopback or address.is_unspecified:
            return match.group(0)  # not sensitive, and common in ports and logs
        return self._hide("ip", str(address))

    def _ipv6(self, match: re.Match[str]) -> str:
        try:
            address = ipaddress.IPv6Address(match.group(0))
        except ValueError:
            return match.group(0)  # a time such as 09:44:28 is not an address
        if address.is_loopback or address.is_unspecified:
            return match.group(0)
        return self._hide("ip", str(address))


def hide(redactor: Redactor, audit: AuditLog, text: str, source: str) -> str:
    """Hide sensitive data, and note in the audit log how much was hidden."""
    text = redactor.redact(text)
    if counts := redactor.drain():
        audit.record(
            {
                "ts": timestamp(),
                "event": "redaction",
                "source": source,
                "counts": dict(counts),
            }
        )
    return text
