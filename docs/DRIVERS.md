# Agent CLI driver matrix — verified, not from docs

Every row below was read from `--help` on **this machine**, 2026-08-29. Versions matter;
re-run `python -m agora.doctor` after any CLI upgrade rather than trusting this file.

| CLI | version | headless | resume | caller-set session id | per-run MCP injection |
|---|---|---|---|---|---|
| Claude Code | 2.1.250 | `-p/--print` | `--resume <id>` / `-c` / `--fork-session` | **`--session-id <uuid>`** | `--mcp-config <files\|json>` |
| Codex CLI | 0.149.0 | `codex exec [PROMPT]` | `codex exec resume <id\|--last> [PROMPT]` | no (server assigns) | `-c mcp_servers.<n>...` |
| Copilot CLI | 1.0.81 | `-p/--prompt` **+ `--allow-all-tools`** | `--continue` / `--connect[=id]` | `-n/--name` (name, not id) | `--additional-mcp-config <json\|@file>` |
| Gemini CLI | 0.54.4 | `-p/--prompt` | `-r/--resume <latest\|N>` | **`--session-id <uuid>`** | `gemini mcp add` / settings.json |

## The finding that shapes the architecture

**No single transport drives all four.** Anyone claiming otherwise has not run the binaries.
Three distinct styles, so: one `Driver` ABC, four adapters, and the supervisor never
learns which style it is talking to.

| Style | Who | Mechanism | Latency per turn |
|---|---|---|---|
| **Persistent stdio (stream-json)** | Claude | `--print --input-format stream-json --output-format stream-json --session-id <uuid>` — long-lived process, write a turn to stdin, read events off stdout. `--replay-user-messages` and `--include-partial-messages` available. | lowest |
| **ACP** (Agent Client Protocol) | Copilot, Gemini | `--acp` — JSON-RPC session where the *supervisor is the client*. Permission requests and agent questions route **back to us**, which is exactly the human-decision hook. | low |
| **Spawn-per-turn** | Codex | `codex exec resume <session_id> "<msg>"`. Process dies each turn; state lives in the session. Also has `app-server` (experimental JSON-RPC) and `codex queue` — evaluate later, do not depend on experimental in v0. | highest (cold start) |

Codex has **no `--acp`**. Do not design around ACP as if it were universal.

## Consequences for the supervisor

1. **`Driver` interface is the abstraction**: `start(session) -> handle`, `send(handle, text) -> AsyncIterator[Event]`, `close(handle)`. Spawn-per-turn adapters fake persistence by storing the session id and re-spawning; the supervisor cannot tell.
2. **Session ids are ours where possible.** Claude and Gemini accept a caller-supplied UUID — generate it, store it in `sessions`, and resume is deterministic. Codex and Copilot hand back their own identifier, so capture it from first-run output and persist it. Never use `--last` / `--continue` in the supervisor: it races when two topics drive the same CLI.
3. **Non-interactive requires permission pre-grants**, and they differ per CLI:
   `claude --permission-mode`, `copilot --allow-all-tools` (mandatory for `-p`), `gemini --yolo` / `--approval-mode`, `codex` sandbox config. Each is a blast-radius decision — keep them in one config block, never scattered through the adapters.
4. **MCP injection is per-run for Claude and Copilot** (`--mcp-config`, `--additional-mcp-config`), which means an agent can be handed the Agora tools *without* mutating that CLI's global config. Prefer this. Gemini needs settings.json, so it is the one that requires a persistent install step.

## Unverified — measure before relying on

- **Tool-call timeout per CLI.** Decides whether a blocking `wait_for_event` long-poll is
  usable, or whether every wake must go through the supervisor. Measure with a deliberately
  slow tool; do not guess.
- **Codex `app-server`** as a persistent driver. Marked experimental.
- **Antigravity** (`~/.antigravity` and `%APPDATA%/Antigravity` both exist here) is an IDE,
  not a CLI. It can consume MCP servers, so it can *participate* in a council as a
  human-driven seat — but it cannot be *woken* by the supervisor. Treat it as a
  poll-on-turn participant, never as a driver.
