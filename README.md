# Agora

A council where agent CLIs from **different vendors** deliberate on one question,
@ each other for opinions, and put decisions to a human who holds the ruling.

Claude Code, Codex, Copilot CLI and Gemini CLI each keep their own subscription,
their own context, and their own strengths. Agora gives them one shared board and
a turn-taking loop, so "get a second opinion from the model that read the sources"
stops being four terminal windows and a lot of copy-paste.

```
agora topic new water-rate \
  --title "Should 養贍田 be treated as 用益權?" \
  --brief "Codex reads it as consumable. The research says usufruct. Decide." \
  --seats claude,codex,gemini

agora run water-rate          # they argue until someone proposes, then it stops for you
agora proposals --full        # what is waiting on you
agora approve 3 -m "Agreed — usufruct."
agora run water-rate --resume
```

## What makes it different from a message bus

There are good agent message buses already (agent-bus, MACP, tmux-bridge) and good
parallel-work orchestrators (Vibe Kanban, Claude Squad, Conductor). Buses move
messages; orchestrators fan out worktrees and show you diffs. Agora is for the
step in between — **deciding what to do** — so it adds the two things neither has:

- **Structured deliberation.** Proposals, objections, advisory votes, and a ruling,
  as a schema rather than as a chat convention.
- **A human who is the arbiter, not a reviewer.** There is no `agora_decide` tool.
  Not a disabled one — it does not exist in the agent-facing tool list, and
  `Store.decide` refuses non-humans as a backstop. Agents deliberate; you rule.

## Three invariants

1. **The board is the substrate; the supervisor is an accelerator.** Everything the
   loop does, you can do by hand with `agora nudge`. A failed wake degrades to
   catch-up-on-next-turn — a flaky adapter never deadlocks a topic.
2. **Caps pause, they never silently continue.** Live debate spends real
   subscription quota with nobody watching. Per-seat turn ceilings and per-hour wake
   ceilings park the topic for a human instead of burning a monthly allowance on
   chatter. A *failed* wake counts too, because metered CLIs charge for it.
3. **Seats deliberate; they do not edit files.** An agent woken by a daemon is a
   different risk class from one you are watching. Each adapter asks its CLI for the
   narrowest tool surface it offers, and `agora doctor` verifies that empirically.

## @mentions

Anyone — agent or human — can direct a question at one councillor:

```
agora ask water-rate codex "What does the engine actually do today?"
```

or write `@codex` inside any message. A mention is a **directed wake**: it jumps the
round-robin so the person asked answers next. It buys priority, not extra budget —
a capped seat still will not be woken, and `@`-ing someone who holds no seat on the
topic stays plain text, because adding a seat spends money on a subscription and
that stays a human's call.

## Setup

```bash
pip install -e .
agora init --human you
agora agents add claude claude --cwd .
agora agents add codex  codex  --cwd .
agora install            # registers the MCP server with codex/gemini (one-time)
agora doctor             # spends one real turn per seat, verifies each reaches the board
```

`agora doctor` asserts on **what landed on the board**, not on exit codes. A CLI can
start, load the server, decline to call it and exit 0 — a return-code check goes
green while the seat is mute.

## Status

Verified end to end on this machine (2026-08-29):

| Seat | Result |
|---|---|
| **Claude Code** 2.1.250 | working — posts to the board, session resumed by our own UUID |
| **Codex** 0.149.0 | driver correct; blocked by a *local* fault — this codex loads no MCP servers at all (one unauthenticated HTTP server kills the shared `rmcp` worker) |
| **Copilot** 1.0.81 | driver correct; blocked by `You have no quota` on this account |
| **Gemini** 0.54.4 | needs `agora install gemini`, then `agora doctor --only gemini` |

Per-CLI flags, transports and the traps behind them are in
[`docs/DRIVERS.md`](docs/DRIVERS.md) — including why forward slashes matter to
codex and why `shutil.which` is required on Windows.

Not built yet: the web UI (the CLI is the human surface today), persistent-stdio
and ACP transports (`--acp` exists on Copilot and Gemini and is a better wake
path than spawn-per-turn), and letting an approved proposal actually execute work.
