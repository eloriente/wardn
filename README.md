# Wardn

> A local-first AI agent that watches over your homelab.

Wardn is an SRE agent for a homelab, powered by a local LLM. It is also a learning project: the goal is to understand the AI-driven programming paradigm by building the pieces from scratch, without frameworks: agent loop, tool calling, context management, permissions and evals.

## What it does

Ask about your servers in plain language, from a terminal or from your phone, and get an answer based on what the machines report right now:

- **`wardn ask` / `wardn chat`**: "How full is /mnt/storage?", "Is Plex running?", "Is anything wrong with my containers?"
- **`wardn summary`**: checks every machine, lists what needs attention (full disks, failed services, containers restarting, unreachable machines) and adds a short summary written by the model.
- **`wardn bot`**: the same, over Telegram: `/status` and `/ask <question>`. Built for the case of being away from home, with the computer off, and wanting to know how the server is doing.
- **`wardn eval`**: measures how reliably the model gets it right.

Everything runs on your own hardware: the model is served by [Ollama](https://ollama.com), and nothing is sent to a cloud LLM.

## How it is built

```
 Telegram / terminal
         │
        CLI ──► Agent loop ──► Ollama (local model)
                   │  ▲
         tool call │  │ short, truncated result
                   ▼  │
               Tools (read-only) ──► Target ──► allowlist ──► SSH ──► your machines
                   │                                  └─► Proxmox API (read-only)
                   └─► Redaction ──► audit log (private, JSONL)
```

The agent loop, the tool calling, the permission layer and the evals are written from scratch: the only runtime dependency is `httpx`.

Design decisions worth knowing:

- **Tools do the work, the model reads the result.** Each tool runs one fixed, read-only command and returns a short, pre-computed text ("`/mnt/storage`: 47% used, WARNING: almost full"). The model never does arithmetic or reads raw `df` output. What steers the model to the right tool is the tool *description*: changing the system prompt did not help, rewording the descriptions did.
- **What needs attention is decided by rules, not by the model.** In `wardn summary`, plain rules build the list of problems; the model only writes a summary on top of it. If it invents or omits something, the list is still right.
- **Evals instead of hope.** A model that works once is luck. `wardn eval` runs scripted scenarios (real command output as fixtures) several times and counts. Each check has a negative control proving it can fail.

## Security model

An agent that can reach your servers must be hard to misuse, so Wardn assumes the model can be wrong or manipulated:

- **Read-only by construction.** There is no generic "run a command" tool. Each tool maps to an entry of an allowlist of exact argument lists (never a shell string, never `shell=True`). `systemctl status` accepts only a validated `*.service` name.
- **Least privilege on the servers.** A dedicated `wardn` user with key-only SSH, no password and no `sudo` group. Docker is read through one exact `sudoers` rule (`docker ps -a --format ...`), never through the `docker` group. Proxmox is read with an API token with the `PVEAuditor` role and its certificate verified.
- **Sensitive data is hidden from the model and from your screen.** Secrets, IPs, MAC addresses, sensitive paths (`/home`, `/etc`, ...), and the users and hostnames you connect with are replaced by stable aliases (`<ip-1>`, `<host-1>`). The real values appear only in a native session on the server, or in the audit log. This is best effort (patterns cannot catch everything), so the read-only limits above remain the stronger control.
- **Audit log.** Every attempt to run a command, allowed or not, is written to a JSONL file only its owner can read. Redaction is recorded as counts, never as values.
- **Telegram.** Wardn polls Telegram and opens no port. Only the Telegram user ids you list get an answer; anyone else is ignored and noted in the audit log, and group chats are ignored. The bot token never appears in an error. Messages travel through Telegram's servers, which is why everything is redacted first.
- **Secrets.** Tokens can live in `config.toml`, but Wardn refuses to start if the file is readable by other users. SSH keys stay in files.

## Quick start

You need Python 3.12+, [uv](https://docs.astral.sh/uv/), [Ollama](https://ollama.com) and a machine you can reach by SSH.

```bash
uv sync
ollama pull qwen3:4b-instruct
mkdir -p ~/.config/wardn
cp config.example.toml ~/.config/wardn/config.toml
chmod 600 ~/.config/wardn/config.toml
```

Edit `~/.config/wardn/config.toml`: one `[targets.<name>]` per machine (host, user, path to the SSH key). `config.toml` is ignored by git and holds everything, tokens included; `config.example.toml` explains every option. Then:

```bash
uv run wardn ask "How full are my disks?"
uv run wardn summary
```

### Try it without a server: the sandbox

A Docker container that behaves like a small server (systemd and sshd), with a fake `docker` command, so you can run Wardn against something safe:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/wardn_sandbox
WARDN_SSH_PUBKEY="$(cat ~/.ssh/wardn_sandbox.pub)" docker compose -f compose.dev.yaml up -d --build
uv run wardn --target sandbox ask "Is anything wrong with my containers?"
```

The sandbox target in `config.example.toml` points to it (`127.0.0.1:2222`). It needs `privileged: true` because systemd must run inside the container: for local development only.

### Preparing a real machine

- **A server or VM**: create a `wardn` user, install the public key, disable password login.
- **Proxmox LXC guests**: `scripts/provision-guest.sh <CTID> <public-key-file> [docker]`, run as root on the Proxmox host, creates the user, installs the key, and with `docker` adds the single `sudoers` rule. Read it before running it.
- **Proxmox itself**: a user and API token with the `PVEAuditor` role, configured in `[targets.<name>.proxmox]` to enable the `guest_status` tool.

Accept each host key once by hand (`ssh -i <key> wardn@<host> true`): Wardn connects with `BatchMode=yes` and never asks questions.

## Commands

| Command | What it does |
| --- | --- |
| `wardn ask <question>` | Answer one question and exit |
| `wardn chat` | Ask several questions, each one a fresh conversation |
| `wardn summary [--no-model]` | What needs attention across all targets, plus the model's summary |
| `wardn bot` | Answer `/status` and `/ask` in Telegram, to the allowed users only |
| `wardn eval [--runs N] [scenario ...]` | Run the eval scenarios and report the pass rate |

Global options: `--config`, `--target`, `--model`, and `-v` to also show the tools the agent used and what they returned. Set `language = "Spanish"` (or any language) in the config to have the model answer in it; with Spanish, the bot's own messages ("Pensando...") are translated too.

## Tools

| Tool | Reads |
| --- | --- |
| `disk_usage` | Filesystem usage, with a warning when a disk is almost full |
| `memory_usage` | RAM and swap |
| `service_status` | The state and recent log lines of one systemd service |
| `failed_services` | The services that failed |
| `running_services` | The running services, optionally filtered by name |
| `container_status` | Docker containers and their health, flagging restart loops |
| `guest_status` | LXC containers and VMs of a Proxmox node, through its read-only API |

Each target can limit which tools it offers (`tools = [...]` in the config).

## Running it 24/7: the Telegram bot on the server

For status checks while away, run Wardn on the server, not on a laptop. The setup used here: a small unprivileged LXC with Wardn and Ollama inside (6 cores, 4 GB of RAM; the model runs on the CPU), its own SSH key authorized on each target, one `config.toml` with mode `0600`, and the bot as a systemd service:

```bash
install -m 644 deploy/wardn-bot.service /etc/systemd/system/wardn-bot.service
systemctl daemon-reload && systemctl enable --now wardn-bot
journalctl -u wardn-bot -f
```

See [deploy/wardn-bot.service](deploy/wardn-bot.service): it runs as the `wardn` user, restarts on failure and has a read-only file system except for the audit log. Only one process may poll a bot token at a time: a second one makes Telegram answer `409 Conflict`.

## Evals

```bash
uv run wardn eval                                   # every scenario, 5 runs each
uv run wardn eval --runs 10 summary_names_every_problem
uv run wardn -v eval disk_ok                        # also print the answers that failed
```

Each scenario gives the agent a scripted server built from real command output, asks a question and checks the tools it called and the words in the answer (and that it does *not* leak IPs, users or secrets). There are scenarios for tool choice, partial service names, missing units, stopped Proxmox guests, crashing containers, redaction, the summary (every problem named, "all is well", an unreachable machine) and answers in Spanish. The reference model is `qwen3:4b-instruct`; the last full run recorded while building it was 80/80 over 16 scenarios with 5 runs each. Evals need Ollama but no target or config.

## Development

```bash
uv run pytest        # unit tests, no network and no model
uv run ruff check . && uv run ruff format --check .
```

The SSH integration tests run against the sandbox and are skipped unless `WARDN_SSH_KEY` points to its key:

```bash
WARDN_SSH_KEY=~/.ssh/wardn_sandbox uv run pytest tests/test_ssh_integration.py -v
```

Layout of `src/wardn`:

| Module | Role |
| --- | --- |
| `agent.py` | The agent loop: ask the model, run the requested tools, repeat |
| `ollama.py` | Minimal client of Ollama's `/api/chat` with tool calling |
| `tools.py` | The read-only tools and their descriptions |
| `target.py`, `ssh_target.py` | Where commands run, and the allowlist that limits them |
| `proxmox.py` | Read-only client of the Proxmox API |
| `redact.py` | Hides sensitive data from the model and the screen |
| `audit.py` | The private audit log |
| `summary.py` | The attention rules and the model's summary |
| `telegram.py` | The Telegram client and the bot |
| `evals.py`, `scenarios.py` | The eval harness and its scenarios |
| `config.py`, `cli.py` | Configuration and the `wardn` command |

## Limitations

- It only reads. It cannot restart a service or change anything, by design.
- Redaction is best effort: treat it as a layer, not a guarantee.
- A 4B model on a CPU is slow (seconds to a minute per answer) and can still pick the wrong tool; that is what the evals measure.
- `/ask` talks to a single target, and the lists of problems ("Needs attention:") are always in English.

## License

Distributed under the MIT License. See [LICENSE.md](LICENSE.md) for details.
