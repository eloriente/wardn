"""SSHTarget: runs allowlisted commands on a remote host through the ssh CLI."""

import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path

from wardn.audit import AuditLog
from wardn.target import Allowlist, CommandResult, CommandTimeout, Target


class SSHTarget(Target):
    def __init__(
        self,
        allowlist: Allowlist,
        audit: AuditLog,
        host: str,
        user: str,
        key_path: Path,
        port: int = 22,
        timeout: float = 10.0,
    ):
        super().__init__(allowlist, audit)
        self._host = host
        self._user = user
        self._key_path = key_path
        self._port = port
        self._timeout = timeout

    def _execute(self, argv: Sequence[str]) -> CommandResult:
        # ssh sends the command to the remote login shell as one string, so
        # every argument is quoted. No local shell is involved (no shell=True).
        command = [
            "ssh",
            "-i",
            str(self._key_path),
            "-p",
            str(self._port),
            "-o",
            "BatchMode=yes",  # never prompt for passwords or host keys
            "-o",
            "IdentitiesOnly=yes",  # use only the key given above
            "-o",
            f"ConnectTimeout={int(self._timeout)}",
            f"{self._user}@{self._host}",
            "--",
            shlex.join(argv),
        ]
        try:
            proc = subprocess.run(
                command,
                check=False,  # a non-zero exit is a result, not an exception
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommandTimeout(
                f"timed out after {self._timeout}s: {list(argv)!r}"
            ) from exc
        return CommandResult(
            stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode
        )
