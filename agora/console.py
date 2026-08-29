"""`agora console` -- one terminal where the whole council is visible.

Without this the human experience is polling `agora show` and guessing when
something happened. The console is the answer to "where do I actually watch
this": replies from every CLI stream into one place as they land, and the same
prompt you are reading from is the one you talk back through.

## How it works

Two threads and no new protocol. A poller thread tails the `events` table -- the
same monotonic cursor every seat uses -- and prints anything new. The main thread
blocks on `input()`. `/run` starts the supervisor on a third thread, so the debate
advances *while* you are reading it and you can cut in mid-argument rather than
waiting for the loop to stop.

Each thread opens its own `Store`. sqlite3 connections are not shareable across
threads, and WAL means the reader never blocks the writers.

## Why the input line is not a chat box

Typing plain text posts as you, and `@name ...` directs a question. But approving
a proposal is `/approve`, deliberately a different gesture from talking -- the one
action agents cannot take should not look like just another message.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path

from .store import Store, StoreError, connect

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
CYAN, YELLOW, GREEN, RED = "\033[36m", "\033[33m", "\033[32m", "\033[31m"

BANNER = f"""{BOLD}agora console{RESET}  --  everything the council says, in one place

  {CYAN}<text>{RESET}                 post to the topic as yourself
  {CYAN}@agent <question>{RESET}      ask one councillor directly (jumps the queue)
  {CYAN}/run{RESET}                   start the debate; agents reply live below
  {CYAN}/stop{RESET}                  stop driving (the board keeps everything)
  {CYAN}/nudge <agent>{RESET}         wake one seat by hand
  {CYAN}/approve <id> [why]{RESET}    rule on a proposal      {DIM}(only you can){RESET}
  {CYAN}/reject <id> [why]{RESET}
  {CYAN}/proposals{RESET}  {CYAN}/seats{RESET}  {CYAN}/topic <slug>{RESET}  {CYAN}/help{RESET}  {CYAN}/quit{RESET}
"""


def _fmt_event(store: Store, ev, me: str) -> str | None:
    """One board event as a line the human wants to read. None = not worth showing."""
    if ev.kind == "message":
        mid = ev.payload.get("message_id")
        row = store.q1("SELECT * FROM messages WHERE id = ?", (mid,))
        if row is None:
            return None
        author, kind, body = row["author"], row["kind"], row["body"].strip()
        if author == me and kind != "ruling":
            return None                      # you just typed it; do not echo it back
        colour = {"system": DIM, "ruling": GREEN, "object": YELLOW}.get(kind, CYAN)
        tag = "" if kind == "say" else f" {DIM}[{kind}]{RESET}"
        mentions = ev.payload.get("mentions") or []
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


class Console:
    def __init__(self, db: Path | str | None, topic_ref: str, me: str):
        self.db = db
        self.store = connect(db)
        self.topic = self.store.topic(int(topic_ref) if str(topic_ref).isdigit() else topic_ref)
        self.topic_id = int(self.topic["id"])
        self.me = me
        self.stop = threading.Event()
        self.driving = threading.Event()
        self.out: queue.Queue[str] = queue.Queue()

    # ------------------------------------------------------------------ threads

    def _poll(self) -> None:
        """Tail the board. Own Store: sqlite3 connections are per-thread."""
        store = connect(self.db)
        cursor = store.head()
        # Show the tail of what already happened, so joining mid-debate has context.
        for m in store.transcript(self.topic_id)[-6:]:
            print(f"\n{DIM}{m['author']}{RESET}  {m['body'].strip()[:400]}")
        while not self.stop.is_set():
            try:
                for ev in store.events_since(cursor, self.topic_id):
                    cursor = ev.id
                    line = _fmt_event(store, ev, self.me)
                    if line:
                        print(line)
                        print(f"{BOLD}>{RESET} ", end="", flush=True)
            except Exception as exc:                    # never kill the console
                print(f"{RED}poller: {exc}{RESET}")
            time.sleep(1.0)
        store.close()

    def _drive(self) -> None:
        """Run the supervisor in the background so the debate advances while you read."""
        import asyncio

        from .drivers.registry import build_drivers
        from .supervisor import Supervisor

        store = connect(self.db)
        try:
            if store.topic(self.topic_id)["status"] == "paused":
                store.set_topic_status(self.topic_id, "open", self.me, "resumed from console")
            sup = Supervisor(store, build_drivers(store))
            reason = asyncio.run(sup.run_topic(self.topic_id))
            print(f"\n{DIM}— supervisor stopped: {reason}{RESET}")
        except Exception as exc:
            print(f"\n{RED}— supervisor failed: {exc}{RESET}")
        finally:
            store.close()
            self.driving.clear()
            print(f"{BOLD}>{RESET} ", end="", flush=True)

    # ------------------------------------------------------------------ commands

    def handle(self, line: str) -> bool:
        """Returns False to quit."""
        line = line.strip()
        if not line:
            return True

        if not line.startswith("/"):
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
            # A human interjection never spends an agent's metered turn.
            self.store.post(self.topic_id, self.me, line, count_turn=False)
            return True

        cmd, _, rest = line[1:].partition(" ")
        rest = rest.strip()

        if cmd in {"quit", "q", "exit"}:
            return False
        if cmd == "help":
            print(BANNER)
        elif cmd == "run":
            if self.driving.is_set():
                print(f"{DIM}already driving{RESET}")
            else:
                self.driving.set()
                threading.Thread(target=self._drive, daemon=True).start()
                print(f"{DIM}driving — agents will reply below; keep typing to interject{RESET}")
        elif cmd == "stop":
            # The supervisor checks topic status between turns; pausing is how you
            # stop it without killing a turn already in flight.
            self.store.set_topic_status(self.topic_id, "paused", self.me, "stopped from console")
            print(f"{DIM}paused after the current turn{RESET}")
        elif cmd == "nudge":
            self._nudge(rest)
        elif cmd in {"approve", "reject"}:
            self._decide(cmd, rest)
        elif cmd == "proposals":
            for p in self.store.proposals(self.topic_id):
                votes = ", ".join(f"{v['agent']}:{v['stance']}" for v in self.store.votes(p["id"]))
                print(f"  #{p['id']} [{p['status']}] {p['title']}  {DIM}{votes}{RESET}")
        elif cmd == "seats":
            for s in self.store.seats(self.topic_id):
                asks = len(self.store.open_mentions(self.topic_id, s["agent"]))
                flag = f"  {YELLOW}{asks} open ask(s){RESET}" if asks else ""
                print(f"  {s['agent']:<12} {s['kind']:<9} {s['state']:<8} "
                      f"{s['turns_used']}/{s['max_turns']} turns{flag}")
        elif cmd == "topic":
            try:
                self.topic = self.store.topic(rest)
                self.topic_id = int(self.topic["id"])
                print(f"{DIM}now on `{self.topic['slug']}`{RESET}")
            except StoreError as exc:
                print(f"{RED}{exc}{RESET}")
        else:
            print(f"{RED}unknown command /{cmd} — /help{RESET}")
        return True

    def _nudge(self, agent: str) -> None:
        import asyncio

        from .drivers.registry import build_drivers
        from .supervisor import Supervisor

        def work() -> None:
            store = connect(self.db)
            try:
                sup = Supervisor(store, build_drivers(store, [agent]))
                r = asyncio.run(sup.wake_seat(self.topic_id, agent))
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

    # --------------------------------------------------------------------- loop

    def run(self) -> int:
        print(BANNER)
        print(f"{BOLD}{self.topic['title']}{RESET}  {DIM}(`{self.topic['slug']}`, "
              f"{self.topic['status']}){RESET}")
        print(f"{DIM}you are {self.me}. /run to start the debate, /help for commands.{RESET}")

        threading.Thread(target=self._poll, daemon=True).start()
        try:
            while True:
                try:
                    line = input(f"{BOLD}>{RESET} ")
                except EOFError:
                    break
                if not self.handle(line):
                    break
        except KeyboardInterrupt:
            print()
        finally:
            self.stop.set()
            self.store.close()
        print(f"{DIM}left the console. The board keeps everything: agora show "
              f"{self.topic['slug']}{RESET}")
        return 0


def run_console(db: Path | str | None, topic: str, me: str) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    # Legacy conhost renders ANSI as literal `<-[1m` garbage until virtual terminal
    # processing is switched on; this no-op call is what enables it. Windows
    # Terminal and mintty are fine, but the console must not assume which one is
    # attached.
    if sys.platform == "win32":
        import os
        os.system("")
    return Console(db, topic, me).run()
