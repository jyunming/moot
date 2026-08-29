"""The debate loop.

Reads the board, decides who speaks next, wakes them, repeats. Everything it
knows how to do, a human could do by hand with `agora nudge` -- that is
deliberate. The supervisor is an accelerator over a board that works without it.

Three rules it will not break:

1. **A human closes every proposal.** The loop stops and waits. It cannot approve,
   and it cannot route around a pending decision by starting a new sub-debate.
2. **Caps pause, they do not silently continue.** Hitting a turn or wake ceiling
   parks the topic in `paused` with a reason on the board, where a human sees it.
3. **A failed wake is not a lost message.** The seat's cursor is untouched, so the
   agent catches up next time anything wakes it. Adapters flake; topics don't die.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Sequence

from .drivers.base import Driver, Seat, WakeResult
from .store import Store

log = logging.getLogger("agora.supervisor")


@dataclass(frozen=True)
class Caps:
    """Cost governor.

    Live debate spends real subscription quota without a human in the loop, and
    the point of routing work across four vendors is to *save* that quota. An
    unbounded loop would burn Copilot's monthly requests on chatter, so every
    ceiling here is a hard stop that pauses for a human rather than a soft hint.
    """
    max_rounds: int = 3
    max_turns_per_seat: int = 6
    max_wakes_per_agent_per_hour: int = 30
    #: Consecutive silent turns across all seats that mean the debate is spent.
    quiet_rounds_to_settle: int = 1


class Supervisor:
    def __init__(
        self,
        store: Store,
        drivers: dict[str, Driver],
        caps: Caps | None = None,
    ) -> None:
        #: keyed by agent *kind* (claude/codex/...), or by agent name for overrides
        self.drivers = drivers
        self.store = store
        self.caps = caps or Caps()

    # ------------------------------------------------------------------ driving

    def driver_for(self, agent: str, kind: str) -> Driver | None:
        return self.drivers.get(agent) or self.drivers.get(kind)

    async def run_topic(self, topic_id: int) -> str:
        """Drive one topic until it needs a human, settles, or hits a cap.

        Returns the terminal reason, which is also written to the board so the
        state is legible without reading logs.
        """
        while True:
            reason = self._blocking_reason(topic_id)
            if reason:
                self._park(topic_id, reason)
                return reason

            speaker = self._next_speaker(topic_id)
            if speaker is None:
                # Everyone in this round has spoken. Advance, or settle.
                topic = self.store.topic(topic_id)
                if topic["round"] + 1 >= topic["max_rounds"]:
                    self._park(topic_id, "rounds exhausted -- needs a human to extend or rule")
                    return "rounds_exhausted"
                with self.store.tx() as c:
                    c.execute("UPDATE topics SET round = round + 1 WHERE id = ?", (topic_id,))
                self.store.post(
                    topic_id, "agora",
                    f"--- round {topic['round'] + 2} of {topic['max_rounds']} ---",
                    kind="system", count_turn=False,
                )
                continue

            await self.wake_seat(topic_id, speaker)

    async def wake_seat(self, topic_id: int, agent: str) -> WakeResult:
        row = self.store.seat(topic_id, agent)
        if row is None:
            raise ValueError(f"{agent} holds no seat on topic {topic_id}")
        meta = self.store.agent(agent)
        topic = self.store.topic(topic_id)

        driver = self.driver_for(agent, meta["kind"])
        if driver is None:
            # Human and external seats are never woken; they read the board.
            return WakeResult.failure(f"no driver for {agent} (kind={meta['kind']})")

        seat = Seat(
            topic_id=topic_id,
            topic_slug=topic["slug"],
            agent=agent,
            kind=meta["kind"],
            cli_session=row["cli_session"],
            cfg=json.loads(meta["driver_cfg"]),
        )
        prompt, cursor = self.build_prompt(topic_id, agent)

        self.store.set_seat_state(topic_id, agent, "waking")
        wake_id = self.store.record_wake(topic_id, agent)
        try:
            result = await asyncio.wait_for(driver.wake(seat, prompt), timeout=driver.timeout_s)
        except asyncio.TimeoutError:
            result = WakeResult.failure(f"timed out after {driver.timeout_s}s")
        except Exception as exc:  # an adapter bug must not take the topic down
            log.exception("driver %s raised for %s", driver.kind, agent)
            result = WakeResult.failure(f"{type(exc).__name__}: {exc}")

        self.store.finish_wake(wake_id, "ok" if result.ok else "error", result.detail)

        if result.ok:
            if result.cli_session and result.cli_session != row["cli_session"]:
                self.store.set_cli_session(topic_id, agent, result.cli_session)
            # Only advance the cursor on success. A failed wake leaves it alone so
            # the agent still sees everything it missed whenever it next speaks.
            self.store.advance_cursor(topic_id, agent, cursor)
            self.store.set_seat_state(topic_id, agent, "idle")
        else:
            self.store.set_seat_state(topic_id, agent, "failed")
            self.store.post(
                topic_id, "agora",
                f"wake failed for {agent}: {result.detail}. "
                f"Its cursor is unchanged -- it will catch up when next woken.",
                kind="system", count_turn=False,
            )
        return result

    # ------------------------------------------------------------- turn-taking

    def _blocking_reason(self, topic_id: int) -> str | None:
        topic = self.store.topic(topic_id)
        if topic["status"] != "open":
            return f"topic is {topic['status']}"

        pending = self.store.proposals(topic_id, status="open")
        if pending:
            # A proposal blocks once every seat has had its *chance* to respond --
            # not once every seat has cast a formal vote. Waiting for votes lets a
            # council that argues without calling agora_vote debate forever with a
            # decision outstanding, which is exactly the routing-around this rule
            # exists to prevent. Seeing a proposal and saying nothing is abstention.
            cursors = {s["agent"]: s["last_seen"] for s in self.store.seats(topic_id)
                       if s["kind"] not in {"human", "external"}}
            for p in pending:
                opened_at = self.store.proposal_event_id(p["id"])
                voted = {v["agent"] for v in self.store.votes(p["id"])}
                outstanding = [
                    a for a, seen in cursors.items()
                    if a != p["author"] and a not in voted and seen < opened_at
                ]
                if not outstanding:
                    return f"proposal #{p['id']} ({p['title']!r}) awaits a human decision"
        return None

    def _next_speaker(self, topic_id: int) -> str | None:
        """Round-robin among seats that still have something to say and budget to say it.

        A seat speaks this round if it has unread events. That is what makes the
        loop terminate naturally: once nobody has anything new to react to, the
        round yields no speakers and the topic settles.
        """
        head = self.store.head()
        seats = {s["agent"]: s for s in self.store.seats(topic_id)}

        # Directed asks jump the queue. Someone said "@codex, what about X?" and
        # waiting for codex's turn in the rotation is not what either of them
        # meant. Caps still apply -- a mention buys priority, not extra budget.
        for m in self.store.open_mentions(topic_id):
            s = seats.get(m["target"])
            if s is None or s["kind"] in {"human", "external"} or not s["enabled"]:
                continue                       # humans answer in their own time
            if s["turns_used"] >= min(s["max_turns"], self.caps.max_turns_per_seat):
                continue
            if self.store.wakes_in_last_hour(s["agent"]) >= self.caps.max_wakes_per_agent_per_hour:
                continue
            return s["agent"]

        for s in seats.values():
            if s["kind"] in {"human", "external"} or not s["enabled"]:
                continue
            if s["last_seen"] >= head:
                continue                                  # nothing new for them
            if s["turns_used"] >= min(s["max_turns"], self.caps.max_turns_per_seat):
                self.store.set_seat_state(topic_id, s["agent"], "capped")
                continue
            if self.store.wakes_in_last_hour(s["agent"]) >= self.caps.max_wakes_per_agent_per_hour:
                self.store.set_seat_state(topic_id, s["agent"], "capped")
                continue
            return s["agent"]
        return None

    def _park(self, topic_id: int, reason: str) -> None:
        topic = self.store.topic(topic_id)
        if topic["status"] == "open":
            self.store.set_topic_status(topic_id, "paused", "agora", reason)
            self.store.post(topic_id, "agora", f"paused: {reason}", kind="system", count_turn=False)

    # ----------------------------------------------------------------- prompting

    def build_prompt(self, topic_id: int, agent: str) -> tuple[str, int]:
        """What the agent is told when woken, plus the cursor that covers it.

        The cursor is returned rather than committed, because it may only be
        advanced if the wake succeeds -- otherwise a dropped turn silently eats
        the messages the agent never saw.
        """
        topic = self.store.topic(topic_id)
        row = self.store.seat(topic_id, agent)
        cursor = row["last_seen"] if row else 0
        head = self.store.head()

        new = self.store.events_since(cursor, topic_id, limit=200)
        msg_ids = [e.payload.get("message_id") for e in new if e.kind == "message"]
        msgs = [m for m in self.store.transcript(topic_id) if m["id"] in set(filter(None, msg_ids))]

        seats = ", ".join(
            f"{s['agent']} ({s['kind']}, {s['turns_used']}/{min(s['max_turns'], self.caps.max_turns_per_seat)} turns)"
            for s in self.store.seats(topic_id)
        )
        turns_left = min(row["max_turns"], self.caps.max_turns_per_seat) - row["turns_used"]

        lines = [
            f"You are **{agent}**, holding a seat on an Agora council.",
            "",
            f"## Topic: {topic['title']}  (`{topic['slug']}`, round {topic['round'] + 1}/{topic['max_rounds']})",
            "",
            topic["brief"],
            "",
            f"**Council:** {seats}",
            f"**Your budget:** {turns_left} turn(s) left. Spend them on disagreement that changes the outcome.",
            "",
        ]

        # A direct ask goes first and is named as such. Burying "@you, what about
        # X?" inside a wall of catch-up is how a question gets answered vaguely or
        # not at all.
        asks = self.store.open_mentions(topic_id, agent)
        if asks:
            lines.append("## Asked of you directly")
            lines.append("")
            for a in asks:
                lines.append(f"**{a['asker']}** asked you:")
                lines.append(a["question"].strip())
                lines.append("")
            lines.append("Answer this first. Posting anything clears the question,")
            lines.append("so if you cannot answer it, say so explicitly rather than changing the subject.")
            lines.append("")

        if msgs:
            lines.append("## Since you last spoke")
            lines.append("")
            for m in msgs:
                if m["author"] == agent:
                    continue
                lines.append(f"**{m['author']}** ({m['kind']}):")
                lines.append(m["body"].strip())
                lines.append("")
        else:
            lines += ["## Since you last spoke", "", "_Nothing new -- you are opening._", ""]

        open_props = self.store.proposals(topic_id, status="open")
        if open_props:
            lines.append("## Open proposals")
            lines.append("")
            for p in open_props:
                stances = ", ".join(f"{v['agent']}:{v['stance']}" for v in self.store.votes(p["id"])) or "no votes yet"
                lines.append(f"- **#{p['id']} {p['title']}** by {p['author']} — {stances}")
                lines.append(f"  {p['body'].strip()[:600]}")
            lines.append("")

        lines += [
            "## What to do now",
            "",
            "Use the `agora` MCP tools. Your reply text here is not read by anyone —",
            "**only what you post through the tools reaches the council.**",
            "",
            "- `agora_read(topic)` — full transcript, if the excerpt above is not enough.",
            "- `agora_say(topic, body)` — argue, add evidence, or disagree. One point, made well.",
            "- `agora_propose(topic, title, body)` — a concrete decision you want taken.",
            "- `agora_vote(proposal_id, stance, rationale)` — `support` / `object` / `abstain`.",
            "- `agora_pass(topic, why)` — nothing to add. Passing is a real answer; say so and stop.",
            "",
            "You cannot approve a proposal, including your own. Votes are advisory;",
            "a human holds every decision. Do not edit files — this council deliberates.",
        ]
        return "\n".join(lines), head


async def run(store: Store, drivers: dict[str, Driver], topic_id: int, caps: Caps | None = None) -> str:
    return await Supervisor(store, drivers, caps).run_topic(topic_id)


def next_speaker(store: Store, topic_id: int, caps: Caps | None = None) -> str | None:
    """Exposed for `agora status` and for tests that assert turn order."""
    return Supervisor(store, {}, caps)._next_speaker(topic_id)


__all__ = ["Supervisor", "Caps", "run", "next_speaker"]
