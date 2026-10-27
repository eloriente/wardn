"""A Telegram bot that answers one person, and nobody else.

Wardn connects out to Telegram (long polling) and opens no port. Only the user
ids in the config get an answer; everyone else is ignored without a reply, and
noted in the audit log. Messages travel through Telegram's servers, so what the
bot sends has already been through the redaction layer.

The client can do exactly two things, like the Proxmox one: read the incoming
messages and send text. The token is part of every URL, so an error here never
carries the URL: it would put the token in a log or in a chat.
"""

import re
import stat
import time
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from wardn.audit import AuditLog, timestamp

API_URL = "https://api.telegram.org"
TOKEN = re.compile(r"\d+:[A-Za-z0-9_-]{20,}")  # 123456:ABC-def...
MAX_MESSAGE_CHARS = 4000  # Telegram refuses more than 4096
MAX_BACKOFF_SECONDS = 60

HELP = (
    "Wardn commands:\n"
    "/status - what needs attention in the homelab, and a short summary\n"
    "/ask <question> - ask the agent about the server\n"
    "/help - this message"
)


class TelegramError(Exception):
    """Telegram is unreachable, refused the token, or answered something unusable."""


@dataclass(frozen=True)
class Update:
    update_id: int
    user_id: int  # 0 when the update is not a message from a person
    chat_id: int
    private: bool  # a direct chat with the bot, not a group
    text: str


def load_token(path: Path) -> str:
    """Read the bot token, refusing a file other users can read."""
    path = path.expanduser()
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise TelegramError(f"cannot read the token file {path}: {exc}") from exc
    if mode & 0o077:
        raise TelegramError(f"{path} can be read by other users. Run: chmod 600 {path}")
    if not TOKEN.fullmatch(text):
        raise TelegramError(f"{path} must hold one line: the token from @BotFather")
    return text


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> Iterator[str]:
    """Pieces that fit in one Telegram message, cut at line breaks when possible."""
    piece = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # a single line longer than a message
            if piece:
                yield piece
                piece = ""
            yield line[:limit]
            line = line[limit:]
        if len(piece) + len(line) > limit:
            yield piece
            piece = ""
        piece += line
    if piece.strip():
        yield piece


class TelegramClient:
    def __init__(
        self,
        token: str,
        poll_seconds: int = 30,
        http: httpx.Client | None = None,
    ):
        self._poll_seconds = poll_seconds
        self._http = http or httpx.Client(
            base_url=f"{API_URL}/bot{token}", timeout=poll_seconds + 10
        )

    def whoami(self) -> str:
        """The bot's username: proves the token works, and says which bot it is."""
        try:
            return "@" + str(self._call("getMe", {})["username"])
        except (KeyError, TypeError) as exc:
            raise TelegramError(f"unexpected answer from Telegram: {exc!r}") from exc

    def updates(self, offset: int | None = None) -> list[Update]:
        """New messages. Waits up to poll_seconds when there are none."""
        payload: dict[str, Any] = {
            "timeout": self._poll_seconds,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        try:
            return [_parse_update(item) for item in self._call("getUpdates", payload)]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise TelegramError(f"unexpected answer from Telegram: {exc!r}") from exc

    def send(self, chat_id: int, text: str) -> None:
        for piece in split_message(text):
            self._call("sendMessage", {"chat_id": chat_id, "text": piece})

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        try:
            response = self._http.post(f"/{method}", json=payload)
        except httpx.HTTPError as exc:
            # Not `from exc`, and not str(exc): both carry the URL, so the token.
            raise TelegramError(
                f"cannot reach Telegram ({type(exc).__name__})"
            ) from None
        if response.status_code in (401, 404):
            raise TelegramError("Telegram refused the token: check it with @BotFather")
        try:
            body = response.json()
            if body["ok"]:
                return body["result"]
            description = str(body.get("description", "no description"))
        except (ValueError, KeyError, TypeError):
            raise TelegramError(f"Telegram answered {response.status_code}") from None
        raise TelegramError(f"Telegram answered {response.status_code}: {description}")


def _parse_update(item: dict[str, Any]) -> Update:
    message = item.get("message") or {}
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    return Update(
        update_id=int(item["update_id"]),
        user_id=int(sender.get("id", 0)),
        chat_id=int(chat.get("id", 0)),
        private=chat.get("type") == "private",
        text=str(message.get("text") or ""),
    )


Reply = Callable[[str], None]
Handler = Callable[[str, Reply], None]  # (what follows the command, how to answer)


class Bot:
    def __init__(
        self,
        client: TelegramClient,
        allowed_users: Collection[int],
        commands: Mapping[str, Handler],
        audit: AuditLog,
    ):
        self._client = client
        self._allowed = frozenset(allowed_users)
        self._commands = dict(commands)
        self._audit = audit
        self._offset: int | None = None

    def poll(self) -> None:
        """Fetch what arrived and handle it, in order."""
        for update in self._client.updates(self._offset):
            self._offset = update.update_id + 1  # never handle a message twice
            self.handle(update)

    def run(
        self,
        stop: Callable[[], bool] = lambda: False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Answer messages until `stop`. A Telegram outage only slows it down."""
        delay = 0.0
        while not stop():
            try:
                self.poll()
                delay = 0.0
            except TelegramError as exc:
                delay = min(max(delay * 2, 2.0), MAX_BACKOFF_SECONDS)
                self._record("telegram_error", error=str(exc), retry_in=delay)
                sleep(delay)

    def handle(self, update: Update) -> None:
        if not update.text or not update.user_id:
            return  # not a text message from a person
        if update.user_id not in self._allowed or not update.private:
            self._record("telegram_denied", user=update.user_id)
            return

        word, _, rest = update.text.strip().partition(" ")
        command = word.split("@")[0].lower()  # /status@MyBot, as sent in groups
        self._record("telegram_message", command=command)

        def reply(text: str) -> None:
            self._client.send(update.chat_id, text)

        handler = self._commands.get(command)
        if handler is None:
            reply(HELP)
            return
        try:
            handler(rest.strip(), reply)
        except TelegramError:
            raise
        except Exception as exc:  # noqa: BLE001  # one bad command must not stop the bot
            # Only the type: the message could hold paths or addresses.
            self._record("telegram_error", command=command, error=type(exc).__name__)
            reply("Something went wrong. The details are in the audit log.")

    def _record(self, event: str, **fields: Any) -> None:
        self._audit.record({"ts": timestamp(), "event": event, **fields})
