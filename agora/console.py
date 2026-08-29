"""`agora console` -- one terminal where the whole council is visible, and where
you answer when it asks you something.

## Why this is not just a log tail

A council that can @ you is useless if answering is awkward. Two things make it
work, and both are load-bearing rather than polish:

**Printing must not mangle what you are typing.** A background poller writing to
stdout while you compose an answer destroys the line you are on. `prompt_toolkit`'s
`patch_stdout` puts arriving messages *above* a stable prompt, so you can be
half-way through a sentence when three agents reply and lose nothing. Without it,
"reply interactively" is a lie. (There is a plain `input()` fallback if
prompt_toolkit is missing; it works, but it interleaves badly.)

**A question addressed to you must be impossible to miss.** Agents @ you when they
have hit something only you know -- where a file lives, which of two readings you
meant. That arrives in the same stream as everything else, so it is rendered
differently, counted in the toolbar, and repeated when the council stops.

## What answering does

Typing plain text posts as you and **discharges every question outstanding against
you** -- because answering in prose is how people actually reply, and requiring a
special "answer" gesture would leave stale asks forever. Your interjections never
spend an agent's metered turn.

## Two gestures that are deliberately not chat

`/approve` and `/reject` close a proposal. That is the one power agents do not
have, and it should not look like just another message. `/effort` retunes how hard
the whole council thinks -- the brainstorming dial: go wide and cheap, then think
deep on the branch worth it.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from .store import Store, StoreError, connect

try:  # optional, but the difference between usable and not
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.patch_stdout import patch_stdout
    HAVE_PTK = True
except ImportError:  # pragma: no cover - fallback path
    HAVE_PTK = False

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
CYAN, YELLOW, GREEN, RED, MAGENTA = "\033[36m", "\033[33m", "\033[32m", "\033[31m", "\033[35m"

COMMANDS = {
    "/run": "start the council; replies stream in below",
    "/stop": "stop driving after the current turn",
    "/effort": "low | medium | high -- how hard the whole council thinks",
    "/asks": "questions waiting on you",
    "/auto": "on | off -- whether posting wakes the council (default on)",
    "/nudge": "wake one seat by hand",
    "/approve": "<id> [why] -- rule on a proposal (only you can)",
    "/reject": "<id> [why]",
    "/proposals": "what is waiting on you",
    "/seats": "who has budget left, who owes an answer",
    "/topic": "<slug> -- switch topic",
    "/help": "this list",
    "/quit": "leave (the board keeps everything)",
}

BANNER = f"""{BOLD}agora console{RESET}  --  everything the council says, in one place

  {CYAN}<text>{RESET}              post as yourself, and answer anything asked of you
  {CYAN}@agent <question>{RESET}   ask one councillor directly (jumps the queue)
  {CYAN}/run{RESET}  {CYAN}/effort{RESET}  {CYAN}/asks{RESET}  {CYAN}/approve{RESET}  {CYAN}/seats{RESET}  {CYAN}/help{RESET}  {CYAN}/quit{RESET}
"""


def _ask_banner(asker: str, question: str) -> str:
    return (f"\n{MAGENTA}{BOLD}❓ {asker} is asking you{RESET}\n"
            f"{MAGENTA}{question.strip()}{RESET}\n"
            f"{DIM}   just type your answer — it posts as you and clears the question{RESET}\n")


def _fmt_event(store: Store, ev, me: str) -> str | None:
    """One board event as a line worth reading. None = not worth showing."""
    if ev.kind == "message":
        row = store.q1("SELECT * FROM messages WHERE id = ?", (ev.payload.get("message_id"),))
        if row is None:
            return None
        author, kind, body = row["author"], row["kind"], row["body"].strip()
        if author == me and kind != "ruling":
            return None                      # you just typed it; do not echo it back

        mentions = ev.payload.get("mentions") or []
        if me in mentions:
            # A question addressed to you is not just another message in the feed.
            return _ask_banner(author, body)

        colour = {"system": DIM, "ruling": GREEN, "object": YELLOW}.get(kind, CYAN)
        tag = "" if kind == "say" else f" {DIM}[{kind}]{RESET}"
        at = f"  {YELLOW}→ @{', @'.join(mentions)}{RESET}" if mentions else ""
        return f"\n{colour}{BOLD}{author}{RESET}{tag}{at}\n{body}\n"

    if ev.kind == "proposal" and ev.payload.get("action") == "opened":
        pid = ev.payload["proposal_id"]
        return (f"\n{YELLOW}{BOLD}◆ proposal #{pid}{RESET} {YELLOW}{ev.payload['title']}{RESET}"
                f"\n{DIM}  by {ev.actor} — waiting on you: /approve {pid}  |  /reject {pid}{RESET}\n")

    if ev.kind == "decision":
        return (f"\n{GREEN}✓ proposal #{ev.payload['proposal_id']} "
                f"{ev.payload['status']} by {ev.actor}{RESET}\n")

    if ev.kind == "topic" and ev.payload.get("action") == "paused":
        return f"\n{DIM}— paused: {ev.payload.get('note', '')}{RESET}\n"
    return None


class _ConsoleCompleter:
    """Completes `/commands` and `@seatnames`. Seats are re-read each time so a
    council that gains a member mid-session completes it."""

    def __init__(self, console: "Console") -> None:
        self.console = console

    def get_completions(self, document, complete_event):  # pragma: no cover - UI
        text = document.text_before_cursor
        word = text.split()[-1] if text.split() and not text.endswith(" ") else ""
        if text.startswith("/") and " " not in text.rstrip():
            for cmd, why in COMMANDS.items():
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text), display_meta=why)
        elif word.startswith("@"):
            for s in self.console.seat_names():
                if s.startswith(word[1:]):
                    yield Completion("@" + s, start_position=-len(word))


class Console:
    def __init__(self, db: Path | str | None, topic_ref: str, me: str):
        self.db = db
        self.store = connect(db)
        self.topic = self.store.topic(int(topic_ref) if str(topic_ref).isdigit() else topic_ref)
        self.topic_id = int(self.topic["id"])
        self.me = me
        self.stop = threading.Event()
        self.driving = threading.Event()
        #: Posting wakes the council without you typing /run. This is the whole
        #: difference between a meeting and a batch job: you say something, they
        #: pick it up, you see the replies, you answer again. /auto off restores
        #: the explicit-only behaviour.
        self.auto = True

    # ------------------------------------------------------------------- state

    def seat_names(self) -> list[str]:
        return [s["agent"] for s in self.store.seats(self.topic_id) if s["agent"] != self.me]

    def pending_asks(self):
        return self.store.open_mentions(self.topic_id, self.me)

    def effort(self) -> str:
        return self.store.topic(self.topic_id)["effort"] or "medium"

    def toolbar(self) -> str:  # pragma: no cover - UI
        asks = len(self.pending_asks())
        props = len(self.store.proposals(self.topic_id, status="open"))
        bits = [f" {self.topic['slug']}", f"effort {self.effort()}"]
        # Live, because the toolbar re-renders on a timer: you can watch a seat
        # think instead of wondering whether anything is happening.
        thinking = [f"{w['agent']} {w['secs']}s" for w in self.store.active_wakes(self.topic_id)]
        if thinking:
            bits.append("thinking: " + ", ".join(thinking))
        else:
            bits.append("driving" if self.driving.is_set() else "idle")
        if asks:
            bits.append(f"{asks} question(s) for you")
        if props:
            bits.append(f"{props} proposal(s) to rule on")
        return "  |  ".join(bits)

    # ----------------------------------------------------------------- threads

    def _poll(self) -> None:
        """Tail the board. Own Store: sqlite3 connections are per-thread."""
        store = connect(self.db)
        cursor = store.head()
        for m in store.transcript(self.topic_id)[-6:]:
            print(f"\n{DIM}{m['author']}{RESET}  {m['body'].strip()[:400]}")
        for a in store.open_mentions(self.topic_id, self.me):
            print(_ask_banner(a["asker"], a["question"]))
        while not self.stop.is_set():
            try:
                for ev in store.events_since(cursor, self.topic_id):
                    cursor = ev.id
                    line = _fmt_event(store, ev, self.me)
                    if line:
                        print(line)
            except Exception as exc:                    # never kill the console
                print(f"{RED}poller: {exc}{RESET}")
            time.sleep(1.0)
        store.close()

    def _drive(self) -> None:
        import asyncio

        from .drivers.registry import build_drivers
        from .supervisor import Caps, Supervisor

        store = connect(self.db)
        try:
            if store.topic(self.topic_id)["status"] == "paused":
                store.set_topic_status(self.topic_id, "open", self.me, "resumed from console")
            sup = Supervisor(store, build_drivers(store), Caps(effort=self.effort()))
            reason = asyncio.run(sup.run_topic(self.topic_id))
            print(f"\n{DIM}— council stopped: {reason}{RESET}")
            for a in store.open_mentions(self.topic_id, self.me):
                print(_ask_banner(a["asker"], a["question"]))
        except Exception as exc:
            print(f"\n{RED}— council failed: {exc}{RESET}")
        finally:
            store.close()
            self.driving.clear()

    # ---------------------------------------------------------------- commands

    def handle(self, line: str) -> bool:
        """Returns False to quit."""
        line = line.strip()
        if not line:
            return True

        if not line.startswith("/"):
            return self._speak(line)

        cmd, _, rest = line[1:].partition(" ")
        rest = rest.strip()
        fn = {
            "quit": lambda _: False, "q": lambda _: False, "exit": lambda _: False,
            "help": self._help, "run": self._run, "stop": self._stop,
            "effort": self._effort, "asks": self._asks, "nudge": self._nudge,
            "auto": self._auto,
            "proposals": self._proposals, "seats": self._seats, "topic": self._switch,
        }.get(cmd)
        if cmd in {"approve", "reject"}:
            self._decide(cmd, rest)
            return True
        if fn is None:
            print(f"{RED}unknown /{cmd}{RESET} — /help")
            return True
        return fn(rest) is not False

    def _speak(self, line: str) -> bool:
        if line.startswith("@"):
            target, _, question = line[1:].partition(" ")
            if not question.strip():
                print(f"{RED}usage: @agent your question{RESET}")
                return True
            try:
                self.store.ask(self.topic_id, self.me, target, question.strip())
                print(f"{DIM}asked {target} — they answer next{RESET}")
            except StoreError as exc:
                print(f"{RED}{exc}{RESET}")
            return True

        answered = self.pending_asks()
        # count_turn=False: a human joining never spends an agent's metered turn.
        self.store.post(self.topic_id, self.me, line, count_turn=False)
        if answered:
            who = ", ".join(sorted({a["asker"] for a in answered}))
            print(f"{DIM}answers {who}{RESET}")
        if self.auto and not self.driving.is_set():
            # What you just said is new board state, so every seat is behind again
            # and the council has something to react to. Making you type /run here
            # is what made this feel like a batch job rather than a conversation.
            self._run("")
        elif not self.driving.is_set():
            print(f"{DIM}council idle — /run when you want them to pick it up{RESET}")
        return True

    def _help(self, _: str) -> None:
        for cmd, why in COMMANDS.items():
            print(f"  {CYAN}{cmd:<11}{RESET} {why}")

    def _run(self, _: str) -> None:
        if self.driving.is_set():
            print(f"{DIM}already driving{RESET}")
            return
        self.driving.set()
        threading.Thread(target=self._drive, daemon=True).start()
        print(f"{DIM}· council thinking at effort {self.effort()} — keep typing{RESET}")

    def _auto(self, rest: str) -> None:
        if rest in {"on", "off"}:
            self.auto = rest == "on"
        print(f"{DIM}auto-wake {'on — posting resumes the council' if self.auto else 'off — /run to drive'}{RESET}")

    def _stop(self, _: str) -> None:
        self.store.set_topic_status(self.topic_id, "paused", self.me, "stopped from console")
        print(f"{DIM}pausing after the current turn{RESET}")

    def _effort(self, rest: str) -> None:
        """The brainstorming dial: cheap and wide, then deep on what survived."""
        if rest not in {"low", "medium", "high"}:
            print(f"  effort is {BOLD}{self.effort()}{RESET}   "
                  f"{DIM}/effort low|medium|high{RESET}")
            print(f"  {DIM}low ≈ 9x faster and thinner; high for a call that turns on "
                  f"catching a flaw{RESET}")
            return
        with self.store.tx() as c:
            c.execute("UPDATE topics SET effort = ? WHERE id = ?", (rest, self.topic_id))
        # Takes effect on the next wake even mid-run: the supervisor captures Caps
        # once, but wake_seat re-reads the topic row every time and topic effort
        # outranks the council default. A concurrent round's wakes all start
        # together, so the change lands on the round after the current one.
        when = " — from the next round" if self.driving.is_set() else ""
        print(f"{DIM}council effort → {rest}{when}{RESET}")

    def _asks(self, _: str) -> None:
        asks = self.pending_asks()
        if not asks:
            print(f"{DIM}nothing is waiting on you{RESET}")
            return
        for a in asks:
            print(_ask_banner(a["asker"], a["question"]))

    def _seats(self, _: str) -> None:
        for s in self.store.seats(self.topic_id):
            owed = len(self.store.open_mentions(self.topic_id, s["agent"]))
            flag = f"  {YELLOW}{owed} open ask(s){RESET}" if owed else ""
            print(f"  {s['agent']:<12} {s['kind']:<9} {s['state']:<8} "
                  f"{s['turns_used']}/{s['max_turns']} turns{flag}")

    def _proposals(self, _: str) -> None:
        for p in self.store.proposals(self.topic_id):
            votes = ", ".join(f"{v['agent']}:{v['stance']}" for v in self.store.votes(p["id"]))
            print(f"  #{p['id']} [{p['status']}] {p['title']}  {DIM}{votes}{RESET}")

    def _switch(self, rest: str) -> None:
        try:
            self.topic = self.store.topic(rest)
            self.topic_id = int(self.topic["id"])
            print(f"{DIM}now on `{self.topic['slug']}`{RESET}")
        except StoreError as exc:
            print(f"{RED}{exc}{RESET}")

    def _nudge(self, agent: str) -> None:
        import asyncio

        from .drivers.registry import build_drivers
        from .supervisor import Supervisor

        def work() -> None:
            store = connect(self.db)
            try:
                r = asyncio.run(Supervisor(store, build_drivers(store, [agent]))
                                .wake_seat(self.topic_id, agent))
                if not r.ok:
                    print(f"\n{RED}{agent}: {r.detail}{RESET}")
            finally:
                store.close()

        threading.Thread(target=work, daemon=True).start()
        print(f"{DIM}waking {agent}...{RESET}")

    def _decide(self, cmd: str, rest: str) -> None:
        pid_s, _, why = rest.partition(" ")
        if not pid_s.isdigit():
            print(f"{RED}usage: /{cmd} <proposal id> [reason]{RESET}")
            return
        try:
            self.store.decide(int(pid_s), self.me, cmd == "approve", why.strip())
        except StoreError as exc:
            print(f"{RED}{exc}{RESET}")

    # -------------------------------------------------------------------- loop

    def run(self) -> int:
        print(BANNER)
        print(f"{BOLD}{self.topic['title']}{RESET}  {DIM}(`{self.topic['slug']}`, "
              f"{self.topic['status']}, effort {self.effort()}){RESET}")
        print(f"{DIM}you are {self.me}. /run to start, /help for commands.{RESET}")

        threading.Thread(target=self._poll, daemon=True).start()
        try:
            self._input_loop()
        except KeyboardInterrupt:
            print()
        finally:
            self.stop.set()
            self.store.close()
        print(f"{DIM}left the console. The board keeps everything: "
              f"agora show {self.topic['slug']}{RESET}")
        return 0

    def _input_loop(self) -> None:
        """Rich prompt where the terminal supports one, plain input everywhere else.

        prompt_toolkit needs a real console. Piped stdin raises EOF, and on Windows
        an MSYS/Cygwin terminal -- Git Bash, mintty -- raises
        NoConsoleScreenBufferError, which is a very plausible way to launch this.
        Crashing there would be absurd when `input()` works fine, so any failure
        setting up the rich prompt degrades instead of ending the session.
        """
        if not HAVE_PTK or not sys.stdin.isatty():
            if HAVE_PTK is False:
                print(f"{DIM}(pip install prompt_toolkit for a prompt that survives "
                      f"incoming messages){RESET}")
            self._plain_loop()
            return
        try:
            self._ptk_loop()
        except Exception as exc:
            print(f"{DIM}(plain prompt: {type(exc).__name__} — for the full console "
                  f"use Windows Terminal, PowerShell or cmd rather than mintty){RESET}")
            self._plain_loop()

    def _ptk_loop(self) -> None:  # pragma: no cover - interactive
        completer = _ConsoleCompleter(self)
        # refresh_interval keeps the toolbar live, so "claude thinking 14s" ticks
        # up while you wait rather than freezing until your next keystroke.
        session = PromptSession(completer=completer, complete_while_typing=False,
                                refresh_interval=1.0)
        # patch_stdout is the whole point: the poller thread's prints land *above*
        # the prompt instead of through the middle of what you are typing.
        with patch_stdout(raw=True):
            while True:
                try:
                    line = session.prompt(ANSI(f"{BOLD}>{RESET} "),
                                          bottom_toolbar=lambda: self.toolbar())
                except EOFError:
                    break
                except KeyboardInterrupt:
                    continue          # Ctrl-C clears the line; Ctrl-D leaves
                if not self.handle(line):
                    break

    def _plain_loop(self) -> None:
        while True:
            try:
                line = input(f"{BOLD}>{RESET} ")
            except EOFError:
                break
            if not self.handle(line):
                break


def run_console(db: Path | str | None, topic: str, me: str) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.platform == "win32":
        import os
        os.system("")   # enable ANSI on legacy conhost
    return Console(db, topic, me).run()
