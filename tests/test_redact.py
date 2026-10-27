import pytest

from wardn.redact import RedactionRules, Redactor


@pytest.fixture
def redactor():
    return Redactor()


@pytest.mark.parametrize(
    ("text", "hidden"),
    [
        ("login password=hunter2 failed", "hunter2"),
        ("login Password: hunter2", "hunter2"),
        ('{"api_key": "abc123def456"}', "abc123def456"),
        ("curl --password hunter2 host", "hunter2"),
        ("Authorization: Bearer abcdef1234567890", "abcdef1234567890"),
        ("token=ghp_" + "a1" * 20, "ghp_"),
        ("key AKIAIOSFODNN7EXAMPLE used", "AKIAIOSFODNN7EXAMPLE"),
        ("https://admin:s3cret@example.com/x", "s3cret"),
        ("jwt eyJhbGciOiJI.eyJzdWIiOiIx.SflKxwRJSMeKKF2", "eyJhbGciOiJI"),
        ("blob " + "a1b2" * 12, "a1b2a1b2"),
        (
            "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\nBBBB\n-----END OPENSSH PRIVATE KEY-----",
            "AAAA",
        ),
        ("-----BEGIN RSA PRIVATE KEY-----\ncut off here", "cut off"),
    ],
)
def test_secrets_are_hidden(redactor, text, hidden):
    result = redactor.redact(text)
    assert hidden not in result
    assert "[REDACTED:secret]" in result


def test_the_key_stays_so_the_log_still_makes_sense(redactor):
    assert (
        redactor.redact("login password=hunter2 failed")
        == "login password=[REDACTED:secret] failed"
    )


@pytest.mark.parametrize(
    "text",
    [
        "Failed password for root from <ip>",  # the word alone is not a secret
        "pam_unix(sshd:auth): authentication failure",
        "Sep 18 09:44:28 pve systemd[1]: Started",
        "/mnt/storage 47% used",
        "/mnt/home/data",
        "fix-storage-perms.service: failed",
        "/dev/mapper/pve-root",
        "0.0.0.0:32400->32400/tcp",
        "listening on 127.0.0.1:8006 and ::1",
        "version 1.2.3.4.5",
        "999.1.1.1",
    ],
)
def test_harmless_text_is_untouched(redactor, text):
    assert redactor.redact(text) == text
    assert not redactor.counts


def test_ips_become_stable_aliases(redactor):
    text = "from 203.0.113.7 port 22, then 198.51.100.23, then 203.0.113.7:2222"
    assert redactor.redact(text) == "from <ip-1> port 22, then <ip-2>, then <ip-1>"
    assert redactor.counts["ip"] == 3


def test_cidr_and_ipv6(redactor):
    result = redactor.redact(
        "net 192.168.1.0/24 and 2001:db8::1 and fe80::1ff:fe23:4567:890a"
    )
    assert "192.168" not in result
    assert "2001:db8" not in result
    assert "fe80" not in result
    assert result.count("<ip-") == 3


def test_mac_addresses(redactor):
    assert redactor.redact("link AA:BB:CC:DD:EE:FF up") == "link <mac-1> up"


@pytest.mark.parametrize(
    "path",
    [
        "/home/wardn/.bashrc",
        "/root/notes.txt",
        "/etc/pve/storage.cfg",
        "/var/lib/docker/volumes/x",
        "/usr/local/bin/fix-storage-perms.sh",
        "/mnt/data/.ssh/authorized_keys",
        "/srv/key/id_ed25519",
    ],
)
def test_sensitive_paths_are_hidden(redactor, path):
    result = redactor.redact(f"cannot read '{path}': denied")
    assert path not in result
    assert result == "cannot read '<path-1>': denied"


def test_data_mounts_stay_visible(redactor):
    text = "chown: cannot access '/mnt/storage/nextcloud': No such file or directory"
    assert redactor.redact(text) == text


def test_extra_paths_from_the_rules():
    redactor = Redactor(RedactionRules(paths=("/mnt/private",)))
    assert (
        redactor.redact("see /mnt/private/photos and /mnt/public")
        == "see <path-1> and /mnt/public"
    )


def test_hostnames_and_users_from_the_rules():
    redactor = Redactor(RedactionRules(hostnames=("pve", "Nas.lan"), users=("deploy",)))
    text = "Sep 18 pve sshd: Accepted for DEPLOY on nas.lan; pveproxy and pve-root stay"
    assert redactor.redact(text) == (
        "Sep 18 <host-1> sshd: Accepted for <user-1> on <host-2>; pveproxy and pve-root stay"
    )


def test_counts_are_drained_by_category(redactor):
    redactor.redact("a 203.0.113.7 b password=x c /etc/passwd")
    assert dict(redactor.drain()) == {"secret": 1, "path": 1, "ip": 1}
    assert not redactor.drain()


def test_reset_starts_aliases_over(redactor):
    redactor.redact("203.0.113.7")
    assert redactor.redact("198.51.100.23") == "<ip-2>"
    redactor.reset()
    assert redactor.redact("198.51.100.23") == "<ip-1>"


def test_a_real_journal_excerpt():
    redactor = Redactor(RedactionRules(hostnames=("pve",), users=("wardn",)))
    text = (
        "Oct 27 09:33:03 pve sshd[137]: Accepted publickey for wardn from 192.168.1.50 port 58342 ssh2\n"
        "Oct 27 09:33:04 pve sshd[137]: pam_env: Unable to open env file: /etc/default/locale\n"
    )
    assert redactor.redact(text) == (
        "Oct 27 09:33:03 <host-1> sshd[137]: Accepted publickey for <user-1> from <ip-1> port 58342 ssh2\n"
        "Oct 27 09:33:04 <host-1> sshd[137]: pam_env: Unable to open env file: <path-1>\n"
    )
