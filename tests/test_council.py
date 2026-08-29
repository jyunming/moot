"""Behavioural tests for the walking skeleton.

These deliberately assert on *computed board state* after a debate actually runs
-- turn counts, cursors, proposal status -- rather than on the shape of the code.
A test that greps for a guard clause goes green while the guard is bypassed
somewhere else; a test that runs the loop and counts the turns does not.
"""

from __future__ import annotations

import asyncio

import pytest

from agora import store as store_mod
from agora.drivers import FakeDriver
from agora.store import NotAuthorised, StoreError, connect
from agora.supervisor import Caps, Supervisor


@pytest.fixture()
def board(tmp_path):
    s = connect(tmp_path / "board.db", init=True)
    s.add_agent("human", "human", display="the arbiter")
    for name in ("claude", "codex", "gemini"):
        s.add_agent(name, name, driver="spawn")
    yield s
    s.close()


def open_debate(board, **kw):
    return board.open_topic(
        "retry-policy", "Should failed webhook deliveries use exponential backoff?",
        "The gateway retries on a fixed 30s schedule. Ops says that stampedes on recovery. Decide.",
        "human", seats=("claude", "codex", "gemini", "human"), **kw,
    )


def run(sup, topic_id):
    return asyncio.run(sup.run_topic(topic_id))


# ----------------------------------------------------------------- turn-taking

def test_debate_runs_and_every_seat_speaks(board):
    topic = open_debate(board, max_rounds=2)
    driver = FakeDriver(board)
    sup = Supervisor(board, {"claude": driver, "codex": driver, "gemini": driver},
                     Caps(max_rounds=2, max_turns_per_seat=4))

    reason = run(sup, topic)

    spoke = {m["author"] for m in board.transcript(topic) if m["kind"] == "say"}
    assert spoke == {"claude", "codex", "gemini"}, f"not everyone spoke: {spoke}"
    # The human seat is never woken -- it reads the board on its own schedule.
    assert not any(a == "human" for a, _ in driver.calls)
    assert reason  # terminated with a stated reason rather than spinning


def test_loop_terminates_when_nobody_has_anything_new(board):
    """Settling is structural: a seat only speaks if its cursor is behind. Once
    everyone is caught up, the round yields no speaker and the topic parks."""
    topic = open_debate(board, max_rounds=5)
    silent = FakeDriver(board, script=lambda st, seat, p: None)
    sup = Supervisor(board, {"claude": silent, "codex": silent, "gemini": silent})

    reason = run(sup, topic)

    assert board.topic(topic)["status"] == "paused"
    assert reason == "rounds_exhausted" or "awaits" in reason
    # Silence must still be bounded: no seat may be woken past its ceiling.
    for s in board.seats(topic):
        assert s["turns_used"] <= s["max_turns"]


# ---------------------------------------------------------------------- caps

def test_seat_turn_cap_is_a_hard_stop(board):
    topic = open_debate(board, max_rounds=99, max_turns=2)
    driver = FakeDriver(board)
    sup = Supervisor(board, {"claude": driver, "codex": driver, "gemini": driver},
                     Caps(max_rounds=99, max_turns_per_seat=2))

    run(sup, topic)

    for s in board.seats(topic):
        if s["kind"] in {"human", "external"}:
            continue
        assert s["turns_used"] <= 2, f"{s['agent']} overspent: {s['turns_used']}"
    assert board.topic(topic)["status"] == "paused", "hitting a cap must pause for a human"


def test_hourly_wake_cap_counts_failures_too(board):
    """A metered CLI charges for a wake that produced nothing, so the ledger
    counts attempts. Otherwise a flapping adapter burns quota invisibly."""
    topic = open_debate(board)
    broken = FakeDriver(board, fail_agents={"claude"})
    sup = Supervisor(board, {"claude": broken}, Caps(max_wakes_per_agent_per_hour=3))

    for _ in range(3):
        asyncio.run(sup.wake_seat(topic, "claude"))

    assert board.wakes_in_last_hour("claude") == 3
    assert sup._next_speaker(topic) != "claude", "capped seat must not be selected again"


# --------------------------------------------------------- human holds the gate

def test_agent_cannot_decide_its_own_proposal(board):
    topic = open_debate(board)
    pid = board.propose(topic, "codex", "Adopt exponential backoff with jitter", "Per the incident review.")

    with pytest.raises(NotAuthorised):
        board.decide(pid, "codex", approve=True)
    with pytest.raises(NotAuthorised):
        board.decide(pid, "claude", approve=True, rationale="I concur")

    assert board.proposal(pid)["status"] == "open"


def test_human_decision_closes_the_proposal_and_lands_on_the_board(board):
    topic = open_debate(board)
    pid = board.propose(topic, "codex", "Adopt exponential backoff with jitter", "Per the incident review.")
    board.vote(pid, "claude", "support", "Matches the sources I read.")

    board.decide(pid, "human", approve=True, rationale="Agreed; ship it behind a flag.")

    p = board.proposal(pid)
    assert (p["status"], p["decided_by"]) == ("approved", "human")
    rulings = [m for m in board.transcript(topic) if m["kind"] == "ruling"]
    assert len(rulings) == 1 and "approved" in rulings[0]["body"]


def test_debate_pauses_once_a_proposal_needs_a_human(board):
    """The loop must not route around a pending decision by debating on."""
    topic = open_debate(board, max_rounds=9)

    def proposer(st, seat, prompt):
        if seat.agent == "codex" and not st.proposals(seat.topic_id):
            st.propose(seat.topic_id, seat.agent, "Adopt backoff with jitter", "Body.")
            return None
        return f"[{seat.agent}] I have read it."

    driver = FakeDriver(board, script=proposer)
    sup = Supervisor(board, {k: driver for k in ("claude", "codex", "gemini")},
                     Caps(max_rounds=9, max_turns_per_seat=8))

    reason = run(sup, topic)

    assert "awaits a human decision" in reason
    assert board.topic(topic)["status"] == "paused"
    assert board.proposals(topic, status="open"), "proposal should still be open"


# ----------------------------------------------- failed wake degrades, not dies

def test_failed_wake_leaves_the_cursor_alone(board):
    """The invariant that keeps a flaky adapter from losing messages."""
    topic = open_debate(board)
    board.post(topic, "claude", "First argument.")
    before = board.seat(topic, "codex")["last_seen"]

    broken = FakeDriver(board, fail_agents={"codex"})
    result = asyncio.run(Supervisor(board, {"codex": broken}).wake_seat(topic, "codex"))

    assert not result.ok
    assert board.seat(topic, "codex")["last_seen"] == before, "cursor moved despite failure"
    assert board.seat(topic, "codex")["state"] == "failed"

    # ...and the catch-up actually contains the message it missed.
    prompt, _ = Supervisor(board, {}).build_prompt(topic, "codex")
    assert "First argument." in prompt


def test_successful_wake_advances_the_cursor(board):
    topic = open_debate(board)
    board.post(topic, "claude", "First argument.")
    driver = FakeDriver(board)

    asyncio.run(Supervisor(board, {"codex": driver}).wake_seat(topic, "codex"))

    assert board.seat(topic, "codex")["last_seen"] > 0
    prompt, _ = Supervisor(board, {}).build_prompt(topic, "codex")
    assert "First argument." not in prompt, "already-seen message replayed"


# ------------------------------------------------------------------- encoding

def test_chinese_text_survives_the_round_trip(board):
    """cp950 is this machine's default codepage; the board must not care."""
    topic = open_debate(board)
    body = "重試一定要加抖動（jitter），否則恢復時會同時湧入——見事故報告 §3。"
    mid = board.post(topic, "claude", body)

    reopened = connect(board.path)
    got = [m for m in reopened.transcript(topic) if m["id"] == mid][0]["body"]
    assert got == body
    reopened.close()


def test_topic_is_closed_to_posts_once_resolved(board):
    topic = open_debate(board)
    board.set_topic_status(topic, "resolved", "human")
    with pytest.raises(StoreError):
        board.post(topic, "claude", "one more thing")


# ------------------------------------------------------------------- mentions

def test_at_mention_makes_the_target_answer_next(board):
    """A directed ask jumps the rotation. Waiting for someone's turn to come round
    is not what "@codex, what about X?" means."""
    topic = open_debate(board)
    board.post(topic, "claude", "Fixed interval is fine. @gemini you profiled the recovery — does that hold?")

    sup = Supervisor(board, {})
    assert sup._next_speaker(topic) == "gemini", "mention did not take priority"

    # ...and the question is put in front of them, not buried in catch-up.
    prompt, _ = sup.build_prompt(topic, "gemini")
    assert "Asked of you directly" in prompt
    assert "claude" in prompt.split("Asked of you directly")[1][:200]


def test_answering_discharges_the_mention(board):
    topic = open_debate(board)
    board.post(topic, "claude", "@gemini does that hold?")
    assert len(board.open_mentions(topic)) == 1

    board.post(topic, "gemini", "No — the p99 recovery numbers say otherwise.")
    assert board.open_mentions(topic) == []
    assert Supervisor(board, {})._next_speaker(topic) != "gemini"


def test_explicit_ask_targets_one_seat(board):
    topic = open_debate(board)
    board.ask(topic, "human", "codex", "What does the engine actually do today?")

    asks = board.open_mentions(topic)
    assert len(asks) == 1 and asks[0]["target"] == "codex" and asks[0]["asker"] == "human"
    assert Supervisor(board, {})._next_speaker(topic) == "codex"
    # A human asking must not spend an agent's metered turn.
    assert board.seat(topic, "codex")["turns_used"] == 0


def test_mention_of_an_unseated_name_is_just_text(board):
    """Adding a seat spends money on someone's subscription, so an @ cannot do it."""
    topic = open_debate(board)
    board.post(topic, "claude", "@copilot and @nobody should weigh in")
    assert board.open_mentions(topic) == []
    with pytest.raises(StoreError):
        board.ask(topic, "human", "copilot", "?")


def test_mention_grants_priority_not_extra_budget(board):
    topic = open_debate(board, max_turns=1)
    board.post(topic, "gemini", "spent my only turn")
    board.post(topic, "claude", "@gemini one more thing?")

    sup = Supervisor(board, {}, Caps(max_turns_per_seat=1))
    assert sup._next_speaker(topic) != "gemini", "capped seat woken by a mention"
