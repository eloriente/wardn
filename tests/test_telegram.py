import json
import stat

import httpx
import pytest

from wardn.audit import MemoryAuditLog
from wardn.telegram import (
    HELP,
    Bot,
    TelegramClient,
    TelegramError,
    Update,
    load_token,
    split_message,
)

TOKEN = "123456:ABCdefGhIJKlmNoPQRsTUVwxyz0123456789"


def message(update_id, text, user=7, chat=7, kind="private"):
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": user},
            "chat": {"id": chat, "type": kind},
            "text": text,
        },
    }


class FakeTelegram:
    """Plays the Telegram API: hands out queued updates, records what is sent."""

    def __init__(self, *updates):
        self.updates = list(updates)
        self.sent = []
        self.polled_with = []

    def __call__(self, request):
        body = json.loads(request.content)
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getUpdates":
            self.polled_with.append(body)
            offset = body.get("offset", 0)
            fresh = [u for u in self.updates if u["update_id"] >= offset]
            return httpx.Response(200, json={"ok": True, "result": fresh})
        self.sent.append((body["chat_id"], body["text"]))
        return httpx.Response(200, json={"ok": True, "result": {}})


def make_client(handler):
    http = httpx.Client(
        base_url=f"https://api.telegram.org/bot{TOKEN}",
        transport=httpx.MockTransport(handler),
    )
    return TelegramClient(TOKEN, http=http)


def make_bot(api, commands=None, allowed=(7,)):
    audit = MemoryAuditLog()
    return Bot(make_client(api), allowed, commands or {}, audit), audit


def events(audit, name):
    return [e for e in audit.events if e["event"] == name]


def test_updates_parse_messages_and_ask_only_for_messages():
    api = FakeTelegram(message(10, "/status", user=7, chat=7))
    (update,) = make_client(api).updates()

    assert update == Update(10, 7, 7, True, "/status")
    assert api.polled_with[0]["allowed_updates"] == ["message"]


def test_updates_without_a_text_message_still_have_an_id():
    api = FakeTelegram({"update_id": 11, "edited_message": {"text": "x"}})
    (update,) = make_client(api).updates()
    assert (update.update_id, update.user_id, update.text) == (11, 0, "")


def test_send_splits_long_text_into_several_messages():
    api = FakeTelegram()
    make_client(api).send(7, "a" * 5000)
    assert [len(text) for _, text in api.sent] == [4000, 1000]


def test_split_message_cuts_at_line_breaks():
    pieces = list(split_message("one\ntwo\nthree\n", limit=8))
    assert pieces == ["one\ntwo\n", "three\n"]


def test_an_allowed_user_runs_a_command_and_gets_the_reply():
    api = FakeTelegram(message(1, "/ask how are the disks?"))
    seen = []
    bot, audit = make_bot(
        api, {"/ask": lambda args, reply: (seen.append(args), reply("fine"))}
    )

    bot.poll()

    assert seen == ["how are the disks?"]
    assert api.sent == [(7, "fine")]
    assert events(audit, "telegram_message")[0]["command"] == "/ask"
    assert "disks" not in str(audit.events)  # the question itself is not logged


def test_a_stranger_gets_no_reply_and_is_noted_in_the_audit_log():
    api = FakeTelegram(message(1, "/status", user=999, chat=999))
    ran = []
    bot, audit = make_bot(api, {"/status": lambda a, r: ran.append(1)})

    bot.poll()

    assert ran == []
    assert api.sent == []
    assert events(audit, "telegram_denied") == [
        {**events(audit, "telegram_denied")[0], "user": 999}
    ]


def test_group_chats_are_ignored_even_for_an_allowed_user():
    api = FakeTelegram(message(1, "/status", user=7, chat=-100, kind="supergroup"))
    ran = []
    bot, _ = make_bot(api, {"/status": lambda a, r: ran.append(1)})
    bot.poll()
    assert ran == [] and api.sent == []


def test_the_bot_username_suffix_and_case_are_accepted():
    api = FakeTelegram(message(1, "/Status@wardn_bot"))
    ran = []
    bot, _ = make_bot(api, {"/status": lambda a, r: ran.append(a)})
    bot.poll()
    assert ran == [""]


@pytest.mark.parametrize("text", ["/help", "/start", "/nope", "hello"])
def test_anything_else_gets_the_help(text):
    api = FakeTelegram(message(1, text))
    bot, _ = make_bot(api, {"/status": lambda a, r: None})
    bot.poll()
    assert api.sent == [(7, HELP)]


def test_a_message_is_never_handled_twice():
    api = FakeTelegram(message(1, "/status"))
    ran = []
    bot, _ = make_bot(api, {"/status": lambda a, r: ran.append(1)})

    bot.poll()
    bot.poll()

    assert ran == [1]
    assert api.polled_with[1]["offset"] == 2


def test_a_failing_command_is_reported_without_the_exception_text():
    api = FakeTelegram(message(1, "/status"))

    def broken(args, reply):
        raise RuntimeError("cannot open /home/secret/file on 192.168.1.9")

    bot, audit = make_bot(api, {"/status": broken})
    bot.poll()

    assert len(api.sent) == 1
    assert "192.168" not in api.sent[0][1]
    assert "192.168" not in str(audit.events)
    assert events(audit, "telegram_error")[0]["error"] == "RuntimeError"


def test_run_backs_off_while_telegram_is_down_and_recovers():
    calls = []

    def api(request):
        calls.append(1)
        if len(calls) <= 3:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"ok": True, "result": []})

    bot, audit = make_bot(api)
    delays = []
    bot.run(stop=lambda: len(calls) >= 5, sleep=delays.append)

    assert delays == [2.0, 4.0, 8.0]  # and it reset after the first good poll
    assert len(events(audit, "telegram_error")) == 3


def test_a_network_error_never_carries_the_token():
    def api(request):
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    with pytest.raises(TelegramError) as caught:
        make_client(api).updates()

    assert "ConnectError" in str(caught.value)
    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_a_refused_token_has_a_clear_message():
    api = lambda request: httpx.Response(401, json={"ok": False})
    with pytest.raises(TelegramError, match="refused the token"):
        make_client(api).updates()


def test_an_error_from_telegram_is_reported():
    api = lambda request: httpx.Response(
        409, json={"ok": False, "description": "Conflict: another getUpdates"}
    )
    with pytest.raises(TelegramError, match="409: Conflict"):
        make_client(api).updates()


def test_an_unusable_answer_is_reported():
    api = lambda request: httpx.Response(200, text="not json")
    with pytest.raises(TelegramError, match="answered 200"):
        make_client(api).updates()


def test_load_token_reads_a_private_file(tmp_path):
    path = tmp_path / "token"
    path.write_text(TOKEN + "\n")
    path.chmod(0o600)
    assert load_token(path) == TOKEN


def test_load_token_refuses_a_file_others_can_read(tmp_path):
    path = tmp_path / "token"
    path.write_text(TOKEN)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    with pytest.raises(TelegramError, match="chmod 600"):
        load_token(path)


def test_load_token_refuses_something_that_is_not_a_token(tmp_path):
    path = tmp_path / "token"
    path.write_text("hello")
    path.chmod(0o600)
    with pytest.raises(TelegramError, match="@BotFather"):
        load_token(path)


def test_load_token_reports_a_missing_file(tmp_path):
    with pytest.raises(TelegramError, match="cannot read the token file"):
        load_token(tmp_path / "missing")


def test_whoami_returns_the_bot_username():
    api = lambda request: httpx.Response(
        200, json={"ok": True, "result": {"id": 1, "username": "wardn_bot"}}
    )
    assert make_client(api).whoami() == "@wardn_bot"


def test_whoami_fails_clearly_with_a_bad_token():
    api = lambda request: httpx.Response(401, json={"ok": False})
    with pytest.raises(TelegramError, match="refused the token"):
        make_client(api).whoami()
