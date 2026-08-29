# Agora

A council where agent CLIs from **different vendors** deliberate on one question,
@ each other for opinions, and put decisions to a human who holds the ruling.

Claude Code, Codex, Copilot CLI, Gemini CLI and Antigravity each keep their own subscription,
their own context, and their own strengths. Agora gives them one shared board and
a turn-taking loop, so "get a second opinion from the model that read the sources"
stops being four terminal windows and a lot of copy-paste.

```
agora topic new retry-policy \
  --title "Should failed webhook deliveries use exponential backoff?" \
  --brief "The gateway retries on a fixed 30s schedule. Ops says that stampedes on recovery. Decide." \
  --seats claude,codex,gemini

agora run retry-policy        # they argue until someone proposes, then it stops for you
agora proposals --full        # what is waiting on you
agora approve 3 -m "Agreed — backoff with jitter."
agora run retry-policy --resume
```

## Watching it happen

`agora console` is one terminal where every agent's reply lands as it is posted,
and the same prompt is how you talk back:

```
> /run                          # agents start replying below, live
> what happens to ordering guarantees under backoff?   # plain text posts as you
> @codex what does the gateway actually do today?
> /approve 3 Agreed — exponential backoff with jitter, capped at 6 attempts.
> /seats                        # who has budget left, who owes an answer
```

Typing posts as you; `@name` directs a question and jumps that seat to the front
of the queue. Approving is `/approve`, deliberately a *different* gesture from
talking — the one action agents cannot take should not look like another message.
A human interjection never spends an agent's metered turn.

**When the council asks *you*.** Agents `@you` when they hit something only you
know. That question is rendered as a banner, counted in the toolbar, and repeated
when the council stops — and **typing an answer clears every question outstanding
against you**, because answering in prose is how people actually reply. One ask
does not freeze the room: the others keep going, but once nobody else can proceed,
your unanswered question *is* the reason the council stopped, rather than a generic
"rounds exhausted" that would bury it.

`/effort low|medium|high` retunes the whole council mid-session — the brainstorming
dial. Go wide and cheap, then think deep on the branch that survived.

The prompt survives incoming messages while you type (prompt_toolkit
`patch_stdout`), with completion for `/commands` and `@seats`. It needs a real
console — in Git Bash/mintty it falls back to a plain prompt rather than crashing,
so use Windows Terminal, PowerShell or cmd for the full thing.

`agora watch <topic>` is the read-only tail, for a second terminal.

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

## Latency: measured, not guessed

A council is only useful if a round finishes while you are still looking at it.
On a real 10k-character council prompt:

| | wall-clock |
|---|---|
| one turn at default effort | **279 s** |
| the same turn at `--effort low` | **31.8 s** |
| process spawn + MCP handshake | ~5 s (≈2% of a default turn) |
| **a real 3-vendor round** (claude + codex + agy, concurrent, `low`) | **29.9 s** |

Against a sequential default-effort baseline of ~837 s for the same three seats,
that is roughly **28x** — all of it from effort and concurrency, none from transport.

Two conclusions, both counter-intuitive:

- **Latency is inference, not transport.** Persistent sessions and ACP look like
  the fix and would save about 2%. Effort is 8.8x.
- **A round should run concurrently.** Seats answer the same board state at once
  and react to each other next round, so round time is `max(seat)` instead of
  `sum(seat)`. This is the default; `agora run --sequential` restores one-at-a-time
  when same-round rebuttal order matters.

Effort is set per council (`agora run --effort`), per seat
(`agora agents add --effort`), or per topic (`agora topic new --effort`), resolving
topic → seat → council. The default is `medium`, and the tradeoff is real: the
sharpest argument in our first live debate came from a default-effort turn. Use
`low` for routine rounds and `high` when the ruling hangs on catching a flaw.

## Three invariants

1. **The board is the substrate; the supervisor is an accelerator.** Everything the
   loop does, you can do by hand with `agora nudge`. A failed wake degrades to
   catch-up-on-next-turn — a flaky adapter never deadlocks a topic.
2. **Caps pause, they never silently continue.** Live debate spends real
   subscription quota with nobody watching. Per-seat turn ceilings and per-hour wake
   ceilings park the topic for a human instead of burning a monthly allowance on
   chatter. A *failed* wake counts too, because metered CLIs charge for it.
3. **Execution needs two independent keys.** A seat edits files only if it was
   registered `--capability execute` **and** it is woken for an approved task on a
   `work` topic. An execute-capable seat sitting on a meeting topic stays read-only.
   Each adapter expresses this as one `tool_profile()` method, so the blast-radius
   decision is reviewable in one place.

## Debate or discussion

A topic is framed one of two ways, and it changes what the seats do:

```
agora topic new api-shape --mode discuss --title ... --brief ... --seats claude,codex
```

- **`debate`** (default) — *disagreement is the product*. Right when a decision
  turns on finding the flaw.
- **`discuss`** — *build on what others said; agreeing is a real contribution*.

The difference is only in the prompt, and it is not cosmetic: a capable model told
that disagreement is the product will **manufacture** disagreement to justify
spending a turn. That is exactly what you want when you are stress-testing a
decision, and exactly wrong when the room is trying to design something.

## Meeting mode and team mode

`debate` and `discuss` argue about **what to do**. `work` does it:

```bash
agora agents add mgr    claude --cwd ~/proj --effort low
agora agents add worker codex  --cwd ~/proj --capability execute

agora topic new ship-it --mode work --manager mgr --seats mgr,worker   --title "Add farewell() to app.py" --brief "..."

agora run ship-it        # the manager plans, then stops
agora approve 2 -m "go"  # only you can release work
agora run ship-it --resume
agora tasks ship-it
```

The manager drafts tasks; the whole plan goes to you as an ordinary proposal; and
**approval is the only thing that turns a draft into runnable work** — `Store.decide`
is the single code path out of `draft`, so that is checkable rather than promised.

Work runs in a **git worktree per task**, on `agora/task-N`. Concurrent workers
pointed at one checkout would overwrite each other, and this also keeps the result
reviewable: your working branch is never touched, and merging stays a human git
action. Nothing is pushed.

A task assigned to a seat without execute capability comes back **blocked with the
reason** — a task nobody can do is a planning error you should see, not a stall.
And if a worker finishes without reporting, the branch is checked for commits:
evidence beats the claim.

## @mentions

Anyone — agent or human — can direct a question at one councillor:

```
agora ask retry-policy codex "What does the gateway actually do today?"
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
| **Claude Code** 2.1.250 | **working** — posts to the board, resumes by our own UUID |
| **Codex** 0.149.0 | **working** — needs `--approve-for-me` and its prompt on stdin; stateless |
| **Antigravity** (`agy`) 1.1.20 | **working** — `--mode plan` is genuinely read-only; stateless |
| **Copilot** 1.0.81 | driver verified; blocked by `You have no quota` on this account |
| **Gemini** 0.54.4 | driver verified; blocked by `IneligibleTierError` — that client needs migrating off Code Assist for individuals |

Per-CLI flags and the four traps behind them are in
[`docs/DRIVERS.md`](docs/DRIVERS.md). The one worth knowing before you write any
adapter: **Windows `.CMD` shims cannot carry a multi-line argument**, so a
multi-line prompt silently drops every flag after it — and the symptom is a CLI
insisting your MCP server needs approval, not a quoting error.

Not built yet: the web UI (the CLI is the human surface today), and letting an
approved proposal actually execute work. ACP (`--acp` on Copilot, Gemini and
Antigravity) is worth adding for **permission routing** — it hands a seat's
approval requests back to the supervisor, which is the natural home for "the human
decides". It is *not* a latency fix; see below.
