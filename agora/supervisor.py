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

#: How the room is framed to a seat. This is the whole difference between the two
#: topic modes, and it is a real one: told that disagreement is the product, a
#: capable model will manufacture disagreement rather than spend a turn agreeing.
#: That is what you want when a decision hangs on finding the flaw, and actively
#: harmful when the room is trying to build something.
FRAMING = {
    "debate": (
        "**This is a debate.** Disagreement is the product. Spend your turns on "
        "objections that would change the outcome, and say what specifically would "
        "have to be true for you to withdraw one. If you agree, say so in a "
        "sentence and stop -- do not restate the argument in your own words."
    ),
    "discuss": (
        "**This is a working discussion, not a debate.** Build on what others have "
        "said: add what is missing, supply the evidence someone asked for, sharpen a "
        "half-formed idea. Agreeing is a real contribution and needs no apology -- do "
        "not manufacture an objection to justify your turn. Disagree only where you "
        "actually do, and then say it plainly."
    ),
}


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
    #: Reasoning effort for every seat unless the topic or the seat overrides it.
    #: `medium` is the default because the endpoints were measured: on a real
    #: council prompt `low` ran 8.8x faster than default effort, but the argument
    #: quality is the thing being traded away. Use `low` for routine rounds,
    #: `high` when the ruling turns on catching a flaw.
    effort: str = "medium"
    #: Consecutive silent turns across all seats that mean the debate is spent.
    quiet_rounds_to_settle: int = 1


class Supervisor:
    def __init__(
        self,
        store: Store,
        drivers: dict[str, Driver],
        caps: Caps | None = None,
        turn_taking: str = "concurrent",
    ) -> None:
        #: keyed by agent *kind* (claude/codex/...), or by agent name for overrides
        self.drivers = drivers
        self.store = store
        self.caps = caps or Caps()
        #: concurrent | sequential -- see run_topic.
        self.turn_taking = turn_taking

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

            speakers = self._eligible(topic_id)
            if speakers and self.turn_taking == "concurrent":
                # Everyone answers the same board state at once, then reacts to
                # each other next round. Round wall-clock becomes max(seat) rather
                # than sum(seat) -- the only structural latency win available,
                # since spawn is ~2% of a turn and the rest is inference.
                #
                # One shared cursor for the whole round is what "simultaneous"
                # means: every prompt is built against the same head, and every
                # seat that succeeds advances to it. Nobody sees a peer's message
                # from this round, which also removes first-speaker anchoring.
                #
                # A proposal opened mid-round does not abort turns already in
                # flight; they finish, and _blocking_reason catches it at the top
                # of the next iteration. Per-seat caps bound the overrun.
                head = self.store.head()
                await asyncio.gather(
                    *(self.wake_seat(topic_id, a, head=head) for a in speakers),
                    return_exceptions=True,
                )
                # A concurrent round IS a round. Without this the counter only
                # advanced in the "nobody left to speak" branch, which concurrency
                # never reaches while seats still have peers to react to -- so
                # max_rounds was silently unenforced and only per-seat caps stopped
                # the loop.
                if not self._advance_round(topic_id):
                    return "rounds_exhausted"
                continue

            speaker = speakers[0] if speakers else None
            if speaker is None:
                # Everyone has spoken and nobody has anything new. Advance, or settle.
                if not self._advance_round(topic_id):
                    return "rounds_exhausted"
                continue

            await self.wake_seat(topic_id, speaker)

    def _advance_round(self, topic_id: int) -> bool:
        """Tick the round counter. False means the topic is out of rounds."""
        topic = self.store.topic(topic_id)
        if topic["round"] + 1 >= topic["max_rounds"]:
            self._park(topic_id, "rounds exhausted -- needs a human to extend or rule")
            return False
        with self.store.tx() as c:
            c.execute("UPDATE topics SET round = round + 1 WHERE id = ?", (topic_id,))
        self.store.post(
            topic_id, "agora",
            f"--- round {topic['round'] + 2} of {topic['max_rounds']} ---",
            kind="system", count_turn=False,
        )
        return True

    def _effort_for(self, topic, agent: str) -> str | None:
        """Topic override beats seat default beats council default."""
        cfg = json.loads(self.store.agent(agent)["driver_cfg"])
        try:
            topic_effort = topic["effort"]
        except (IndexError, KeyError):
            topic_effort = None
        return topic_effort or cfg.get("effort") or self.caps.effort

    async def wake_seat(self, topic_id: int, agent: str, *, head: int | None = None) -> WakeResult:
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
            effort=self._effort_for(topic, agent),
        )
        prompt, cursor = self.build_prompt(topic_id, agent, head=head)

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

    def _eligible(self, topic_id: int) -> list[str]:
        """Every seat that has something to react to and budget to react with.

        A seat qualifies when its cursor is behind the board, which is what makes
        the loop terminate on its own: once everyone is caught up, the round
        yields nobody and the topic settles. Directed asks come first -- in
        sequential mode that is a real queue jump, and in concurrent mode it only
        orders the list, since everyone runs together anyway.
        """
        head = self.store.head()
        seats = {s["agent"]: s for s in self.store.seats(topic_id)}

        def can_speak(s) -> bool:
            if s["kind"] in {"human", "external"} or not s["enabled"]:
                return False        # humans answer on their own schedule
            if s["turns_used"] >= min(s["max_turns"], self.caps.max_turns_per_seat):
                self.store.set_seat_state(topic_id, s["agent"], "capped")
                return False
            if self.store.wakes_in_last_hour(s["agent"]) >= self.caps.max_wakes_per_agent_per_hour:
                self.store.set_seat_state(topic_id, s["agent"], "capped")
                return False
            return True

        ordered: list[str] = []
        # A mention buys priority, not budget: a capped seat is still not woken.
        for m in self.store.open_mentions(topic_id):
            s = seats.get(m["target"])
            if s is not None and s["agent"] not in ordered and can_speak(s):
                ordered.append(s["agent"])

        for s in seats.values():
            if s["agent"] in ordered or s["last_seen"] >= head:
                continue
            if can_speak(s):
                ordered.append(s["agent"])
        return ordered

    def _next_speaker(self, topic_id: int) -> str | None:
        eligible = self._eligible(topic_id)
        return eligible[0] if eligible else None

    def _park(self, topic_id: int, reason: str) -> None:
        topic = self.store.topic(topic_id)
        if topic["status"] == "open":
            self.store.set_topic_status(topic_id, "paused", "agora", reason)
            self.store.post(topic_id, "agora", f"paused: {reason}", kind="system", count_turn=False)

    # ----------------------------------------------------------------- prompting

    def build_prompt(self, topic_id: int, agent: str, head: int | None = None) -> tuple[str, int]:
        """What the agent is told when woken, plus the cursor that covers it.

        The cursor is returned rather than committed, because it may only be
        advanced if the wake succeeds -- otherwise a dropped turn silently eats
        the messages the agent never saw.
        """
        topic = self.store.topic(topic_id)
        row = self.store.seat(topic_id, agent)
        cursor = row["last_seen"] if row else 0
        # A caller driving a concurrent round passes one shared head so every seat
        # in the round sees the same board.
        head = self.store.head() if head is None else head

        new = self.store.events_since(cursor, topic_id, limit=200, until=head)
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
            f"**Your budget:** {turns_left} turn(s) left.",
            "",
            FRAMING[topic["mode"]],
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

        # Bounded by the same head, for the same reason as the events above.
        open_props = [p for p in self.store.proposals(topic_id, status="open")
                      if self.store.proposal_event_id(p["id"]) <= head]
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
            "- `agora_ask(topic, agent, question)` — put a question to one councillor by name.",
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
