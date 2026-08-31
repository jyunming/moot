"""A council in a chat — the parts that do not need a bot token.

The renderer and the pairing fence are where this either works or quietly
ruins a council, so they are what is tested here. The transport is a thin
wrapper around them.
"""

from __future__ import annotations

import pathlib
import pytest

from mooting.store import NotAuthorised, StoreError, connect
from mooting.telegram import (LIMIT, Throttle, blocks, chunks, event_text,
                              plain)


# ---------------------------------------------------------------- rendering

def test_escapes_the_three_characters_html_mode_cares_about():
    """HTML mode was chosen over MarkdownV2 precisely because it is three
    characters and not eighteen. If they are not escaped, Telegram rejects the
    whole message and the seat's turn is simply lost."""
    out = " ".join(blocks("compare a < b & c > d"))
    assert "&lt;" in out and "&gt;" in out and "&amp;" in out
    assert "a < b" not in out


def test_a_table_survives_as_monospace():
    """There are no tables in Telegram. Agents produce them unprompted -- one
    appeared in the first real debate on this board -- and the alignment is the
    part that carried the meaning."""
    md = "| Option | Annual |\n|---|---|\n| Oil | 1750 |\n| Air | 1435 |"
    out = blocks(md)
    assert len(out) == 1 and out[0].startswith("<pre>")
    assert "| Oil | 1750 |" in out[0]


def test_fenced_code_keeps_its_language_and_is_not_re_read_as_markdown():
    md = "```python\nx = a_*_b  # **not bold**\n```"
    out = blocks(md)[0]
    assert 'class="language-python"' in out
    assert "<b>" not in out and "<i>" not in out, "markdown inside code was rendered"


def test_inline_markdown_becomes_tags():
    out = " ".join(blocks("**bold** and *italic* and `code`"))
    assert "<b>bold</b>" in out
    assert "<i>italic</i>" in out
    assert "<code>code</code>" in out


def test_bullets_and_quotes_survive():
    out = " ".join(blocks("- first\n- second\n\n> I withdraw one thing."))
    assert "• first" in out
    assert "<blockquote>I withdraw one thing.</blockquote>" in out


# ----------------------------------------------------------------- splitting

def test_nothing_exceeds_the_message_limit():
    """4096 is a hard ceiling; a longer message is refused outright."""
    md = "\n\n".join(f"paragraph {i} " + "word " * 200 for i in range(30))
    for piece in chunks(md):
        assert len(piece) <= LIMIT


def test_an_oversized_code_block_is_split_but_every_piece_stays_closed():
    """Half a `<pre>` is not a message. This is the split most likely to be got
    wrong, because it is the one a long log triggers."""
    pieces = chunks("```\n" + ("x" * 9000) + "\n```", limit=500)
    assert len(pieces) > 1
    for p in pieces:
        assert p.startswith("<pre>") and p.endswith("</pre>"), p[:60]
        assert len(p) <= 500


def test_splitting_happens_between_blocks_not_inside_tags():
    md = "**one**\n\n**two**\n\n**three**"
    for p in chunks(md, limit=20):
        assert p.count("<b>") == p.count("</b>"), p


def test_plain_is_a_real_fallback():
    """A reply that cannot be formatted must still arrive."""
    assert plain("<b>Direct answer</b> and <code>a &lt; b</code>") == \
        "Direct answer and a < b"


# ---------------------------------------------------------------- throttling

def test_the_throttle_respects_both_ceilings():
    """One per second per chat, twenty per minute in a group. A ten-round
    council with three seats is thirty replies, so this is load-bearing."""
    t = Throttle()
    assert t.delay(now=100.0) == 0.0
    t.record(100.0)
    assert t.delay(100.2) == pytest.approx(0.8, abs=0.01), "sent two in one second"

    burst = Throttle()
    for i in range(20):
        burst.record(200.0 + i * 0.001)
    assert burst.delay(200.5) > 50.0, "twenty in a minute was not enforced"


# ------------------------------------------------------------------- pairing

@pytest.fixture()
def board(tmp_path):
    s = connect(tmp_path / "board.db", init=True)
    s.add_agent("jeremy", "human")
    s.add_agent("santa", "claude", driver="spawn")
    yield s
    s.close()


def test_an_unknown_sender_can_do_nothing_until_approved(board):
    """openclaw's rule, and the right one: an unknown sender is inert until
    somebody who already has authority approves them. A chat has no operating
    system to ask who is speaking."""
    assert board.seat_for_chat("-100", "42") is None

    pid = board.pair_request("-100", "42", "Someone")
    assert board.seat_for_chat("-100", "42") is None, "a request granted access"

    board.pair_approve(pid, "jeremy", "jeremy")
    assert board.seat_for_chat("-100", "42") == "jeremy"


def test_a_person_cannot_be_paired_onto_an_agent_seat(board):
    """It would let them speak as an agent, and put a non-human name against
    something only a human may do."""
    pid = board.pair_request("-100", "43", "Sneaky")
    with pytest.raises(NotAuthorised):
        board.pair_approve(pid, "santa", "jeremy")
    assert board.seat_for_chat("-100", "43") is None


def test_a_denied_request_stays_denied(board):
    pid = board.pair_request("-100", "44", "Nope")
    board.pair_deny(pid, "jeremy")
    assert board.seat_for_chat("-100", "44") is None


def test_the_same_person_in_another_chat_is_a_separate_question(board):
    """Approval is per room. Being trusted in one council is not being trusted
    in another."""
    pid = board.pair_request("-100", "42", "Someone")
    board.pair_approve(pid, "jeremy", "jeremy")
    assert board.seat_for_chat("-999", "42") is None


def test_approving_something_that_was_never_requested_is_refused(board):
    with pytest.raises(StoreError):
        board.pair_approve(9999, "jeremy", "jeremy")


# --------------------------------------------------------------- the pump

def test_system_noise_stays_out_of_the_chat(board):
    """Round markers and pause notices are terminal furniture. In a chat they
    are twenty messages a council nobody wanted, against a 20/minute ceiling."""
    tid = board.open_topic("t", "T", "T", "jeremy", seats=("santa", "jeremy"))
    board.post(tid, "mooting", "--- round 2 of 10 ---", kind="system",
               count_turn=False)
    board.post(tid, "santa", "**a real argument**", count_turn=False)

    said = [event_text(board, e) for e in board.events_since(0, tid)]
    kept = [s for s in said if s]
    assert any("a real argument" in s for s in kept)
    assert not any("round 2 of 10" in s for s in kept), "system noise reached the chat"


def test_a_seat_is_derived_from_the_persons_own_name(board):
    """Approving somebody should not also mean inventing a name for them."""
    fresh = board.seat_name_for("Wilhelmina Vasquez")
    assert fresh == "Wilhelmina"
    assert board.seat_name_for("Wilhelmina Vasquez") == fresh, "made a second seat"

    # and somebody whose name already has a human seat gets that seat, whatever
    # the capitalisation -- `Jeremy` beside `jeremy` would be two of one person
    assert board.seat_name_for("Jeremy Chen") == "jeremy"


def test_a_derived_seat_never_collides_with_an_agent(board):
    """`Santa` beside the agent `santa` is two seats one letter apart, and an
    @mention could mean either."""
    got = board.seat_name_for("Santa Claus")
    assert got.lower() != "santa", got
    assert board.agent("santa")["kind"] == "claude", "the agent seat was taken over"
    assert board.agent(got)["kind"] == "human"


def test_a_derived_seat_is_a_human_seat_so_it_may_rule(board):
    """The whole point of pairing is to produce someone who can rule. A seat
    that is not human cannot, and `pair_approve` would refuse it."""
    name = board.seat_name_for("A Colleague")
    assert board.is_human(name)
    pid = board.pair_request("-100", "77", "A Colleague")
    assert board.pair_approve(pid, name, "jeremy")["seat"] == name


def test_a_seat_name_is_never_one_letter(board):
    """"A Colleague" gave the seat `A`, which is nobody."""
    assert board.seat_name_for("A Colleague") == "AColleague"
    assert board.seat_name_for("Bo Li") == "BoLi"


def test_a_non_latin_name_stays_a_name(board):
    """`slugify` keeps non-Latin scripts on purpose, and a name is no different.
    An ASCII-only filter turns such a name into `guest`."""
    assert board.seat_name_for("Ясна Ковач") == "Ясна"
    assert board.seat_name_for("Δ Κ") == "ΔΚ"


def test_terminal_colour_never_reaches_a_chat(tmp_path):
    """The console writes for a terminal. In Telegram an escape code is not
    invisible, it is literal noise: `[2mnow on ...` is what arrives."""
    from mooting.telegram import ANSI, ChatBoard

    s = connect(tmp_path / "board.db", init=True)
    s.add_agent("me", "human")
    s.close()

    b = ChatBoard(tmp_path / "board.db", None, "me")
    try:
        out = b.handle("/topic new should we cap retries?")
    finally:
        b.close()
    assert ANSI.search(out) is None, f"escape codes reached the chat: {out!r}"
    # not only the bracket form: a bare ESC left behind is still a stray byte
    assert chr(27) not in out, f"an escape byte survived: {out!r}"


def test_the_chat_stays_on_its_topic_between_messages(tmp_path):
    """A council spans many messages. Rebuilding per message forgot the last
    one, so `/topic agenda` answered "no topic yet" about a topic just made."""
    from mooting.telegram import ChatBoard

    s = connect(tmp_path / "board.db", init=True)
    s.add_agent("me", "human")
    s.close()

    first = ChatBoard(tmp_path / "board.db", None, "me")
    try:
        first.handle("/topic new should we cap retries?")
        slug = first.topic
    finally:
        first.close()
    assert slug, "the topic it just opened was not remembered"

    # the next message arrives on a fresh board, as it does in the bot
    second = ChatBoard(tmp_path / "board.db", slug, "me")
    try:
        out = second.handle("/topic agenda cap the retries; jitter")
    finally:
        second.close()
    assert "no topic" not in out.lower(), out
    assert "cap the retries" in out


# ------------------------------------------------------- failing to start

def test_every_way_a_token_can_fail_gets_a_sentence():
    """A stack trace through aiogram's internals is the least useful possible
    answer to "my token is wrong", and that is the commonest first run there
    is. Telegram answers 401 for a wrong token and 404 for a revoked one, and
    a token that is not even shaped like one fails earlier still, inside
    `Bot()`. All three used to traceback; only the first was handled."""
    from aiogram.exceptions import (TelegramNetworkError, TelegramNotFound,
                                    TelegramUnauthorizedError)
    from aiogram.utils.token import TokenValidationError

    from mooting.telegram import explain_start_failure as explain

    said = explain(TokenValidationError("Token is invalid!"))
    assert said and "does not look like a bot token" in said[0]

    for exc in (TelegramUnauthorizedError(method=None, message="Unauthorized"),
                TelegramNotFound(method=None, message="Not Found")):
        said = explain(exc)
        assert said, f"{type(exc).__name__} produced no explanation"
        assert "will not accept that token" in said[0], said

    said = explain(TelegramNetworkError(method=None, message="down"))
    assert said and "Could not reach Telegram" in said[0]

    # and anything that is not Telegram's fault is left to raise
    assert explain(ValueError("something else")) is None


# ------------------------------------------------------- remembering a token

def test_the_token_is_asked_for_once(tmp_path, monkeypatch, capsys):
    """A bot you have to re-authorise every time is a bot you stop using."""
    from mooting.cli import main

    db = tmp_path / "board.db"
    s = connect(db, init=True)
    s.add_agent("me", "human")
    s.close()

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("MOOTING_TELEGRAM_TOKEN", raising=False)

    # nothing saved: it says so, and says it will only ask once
    assert main(["--db", str(db), "--as", "me", "telegram"]) == 1
    err = capsys.readouterr().err
    assert "need a bot token, once" in err
    assert "@BotFather" in err

    # a token Telegram has accepted is remembered by `run`, not by the CLI --
    # so simulate what run does, then check the second call needs no token
    b = connect(db)
    b.set_setting("telegram.token", "8123:AAFpretend-this-one-worked")
    b.close()

    seen = {}

    def fake_run(db_, *, bot_token, chats, human, topic, remember):
        seen.update(token=bot_token, remember=remember, chats=chats)
        return 0

    import mooting.telegram as tg
    monkeypatch.setattr(tg, "run", fake_run)
    assert main(["--db", str(db), "--as", "me", "telegram"]) == 0
    assert seen["token"] == "8123:AAFpretend-this-one-worked"
    assert seen["remember"] is False, "re-saved a token it had just read back"
    assert "remembered from a previous run" in capsys.readouterr().out


def test_a_token_telegram_rejected_is_not_remembered(tmp_path, monkeypatch):
    """Saving before the first call meant a bad token was kept, and every later
    run failed the same way with nothing to explain it."""
    from mooting.cli import main

    db = tmp_path / "board.db"
    s = connect(db, init=True)
    s.add_agent("me", "human")
    s.close()

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("MOOTING_TELEGRAM_TOKEN", raising=False)

    import mooting.telegram as tg
    # `run` returning non-zero is what a refused token looks like; it saves
    # nothing, because saving happens inside `run` after Telegram accepts.
    monkeypatch.setattr(tg, "run", lambda *a, **k: 1)
    assert main(["--db", str(db), "--as", "me", "telegram", "--token", "1:bad"]) == 1

    b = connect(db)
    try:
        assert b.setting("telegram.token") is None, "a refused token was kept"
    finally:
        b.close()


def test_forgetting_the_token_leaves_the_board_alone(tmp_path, monkeypatch):
    from mooting.cli import main

    db = tmp_path / "board.db"
    s = connect(db, init=True)
    s.add_agent("me", "human")
    s.set_setting("telegram.token", "8123:AAFsomething")
    tid = s.open_topic("t", "T", "T", "me", seats=("me",))
    s.close()

    assert main(["--db", str(db), "--as", "me", "telegram", "--forget-token"]) == 0
    b = connect(db)
    try:
        assert b.setting("telegram.token") is None
        assert len(b.topics()) == 1, "forgetting a token touched the council"
    finally:
        b.close()


# ------------------------------------------------------- ruling from a chat

def test_a_decision_reaches_the_chat_however_it_was_taken(board):
    """A proposal approved at the terminal used to be invisible from a phone:
    `event_text` rendered messages and new proposals and dropped decisions, so
    somebody following along watched a proposal arrive and never learned what
    happened to it."""
    from mooting.telegram import event_text

    tid = board.open_topic("t", "T", "b", "jeremy", seats=["jeremy", "santa"])
    pid = board.propose(tid, "santa", "Cap retries at 6", "body")
    head = board.head()
    board.decide(pid, "jeremy", approve=True, rationale="agreed, cap at 6")

    said = [event_text(board, ev) for ev in board.events_since(head, tid)]
    said = [x for x in said if x]
    assert any("approved" in x and "jeremy" in x for x in said), said
    assert any("agreed, cap at 6" in x for x in said), said


def test_a_decision_with_no_reason_still_announces(board):
    """`/approve 4` with no words is legal, and used to render a trailing dash."""
    from mooting.telegram import event_text

    tid = board.open_topic("t2", "T2", "b", "jeremy", seats=["jeremy", "santa"])
    pid = board.propose(tid, "santa", "Something", "body")
    head = board.head()
    board.decide(pid, "jeremy", approve=False, rationale="")

    said = [x for x in (event_text(board, ev)
                        for ev in board.events_since(head, tid)) if x]
    line = next(x for x in said if "rejected" in x)
    assert not line.rstrip().endswith("—"), line


def test_nudging_a_mention_does_not_die_in_a_thread(tmp_path):
    """`/nudge @Santa` is what a phone gives you -- Telegram completes a mention
    with its `@` attached. The seat is `Santa`, so the wake raised
    `@Santa holds no seat on topic 5` inside a daemon thread, where nobody was
    listening: the chat said "waking @Santa..." and then stayed silent for ever.
    Seen in a live session, not in a test."""
    from mooting.store import connect
    from mooting.telegram import ChatBoard

    db = tmp_path / "b.db"
    st = connect(db, init=True)
    st.add_agent("Jeremy", "human")
    st.add_agent("Santa", "claude", driver="spawn")
    st.open_topic("t", "T", "b", "Jeremy", seats=["Jeremy", "Santa"])
    st.close()

    chat = ChatBoard(db, "t", "Jeremy")
    try:
        # The `@` is tolerated, and the seat resolves whatever the case.
        assert chat.console._seat_named("@Santa") == "Santa"
        assert chat.console._seat_named("santa") == "Santa"
        assert chat.console._seat_named("Santa") == "Santa"

        # A name that is not here says so, rather than raising out of sight.
        assert chat.console._seat_named("@Nobody") is None
    finally:
        chat.close()


def test_nudging_an_unknown_seat_answers_in_the_chat(tmp_path):
    """The failure has to arrive where the person is looking."""
    from mooting.store import connect
    from mooting.telegram import ChatBoard

    db = tmp_path / "b2.db"
    st = connect(db, init=True)
    st.add_agent("Jeremy", "human")
    st.add_agent("Santa", "claude", driver="spawn")
    st.open_topic("t", "T", "b", "Jeremy", seats=["Jeremy", "Santa"])
    st.close()

    chat = ChatBoard(db, "t", "Jeremy")
    try:
        out = chat.handle("/nudge @Nobody")
        assert "Nobody" in out and "Santa" in out, out
    finally:
        chat.close()


def test_a_stop_notice_stays_on_one_line_and_italicises():
    """Seen on a phone: the pause notice arrived with literal underscores around
    it and stopped mid-word at `tapeou_`.

    Two causes, one symptom. The reason embedded 200 raw characters of somebody
    else's turn, newlines and all; and Telegram-HTML is assembled per line, so an
    italic span opening in one paragraph and closing in another matches nothing
    and both underscores survive as text."""
    from mooting.supervisor import snippet
    from mooting.telegram import blocks

    question = ("@Santa Direct response on both points:\n\n"
                "1. On hyperscalers/fabless: I concede that fabless giants "
                "(Apple, Nvidia, Google) do not run production mask "
                "calibration - the foundry owns the final tapeout and the "
                "OPC recipes, which is the part that carries the liability.")
    reason = f"Kevin is waiting on you: {snippet(question)}"

    assert "\n" not in reason, "a multi-line reason breaks the italics"

    out = blocks(f"_council stopped: {reason}_")
    assert len(out) == 1, out
    assert out[0].startswith("<i>") and out[0].endswith("</i>"), out[0]
    assert "_" not in out[0], "an underscore reached the chat as text"


def test_a_snippet_is_cut_at_a_word_not_through_one():
    """`tapeou` is what a blind slice gives you."""
    from mooting.supervisor import snippet

    assert snippet("short enough already") == "short enough already"

    long = "word " * 100
    got = snippet(long, limit=40)
    assert got.endswith("…")
    assert len(got) <= 41
    assert not got.rstrip("…").endswith("wor"), got

    # Whitespace of every kind collapses, so the result is one line.
    assert snippet("a\n\nb   c\td") == "a b c d"


def test_asking_for_a_proposal_by_number_gets_the_buttons_back():
    """The pump starts at the board's head and never replays, so a proposal
    opened before the bot started -- or while the council ran at the terminal --
    could never show its buttons. Asking for it by number is the way back."""
    from mooting.telegram import proposal_ref

    assert proposal_ref("/proposals 3") == 3
    assert proposal_ref("/proposal 3") == 3
    assert proposal_ref("/proposals #3") == 3
    # Telegram appends the bot name to commands in group chats.
    assert proposal_ref("/proposals@mooting_bot 12") == 12
    assert proposal_ref("  /PROPOSALS 7  ") == 7

    # The bare listing keeps its plain-text form, and neighbouring commands are
    # not swallowed.
    assert proposal_ref("/proposals") is None
    assert proposal_ref("/proposals all") is None
    assert proposal_ref("/propose something") is None
    assert proposal_ref("hello") is None


def test_a_button_carries_which_proposal_it_meant():
    """`/approve 3` typed from memory on a phone is how a ruling lands on the
    wrong proposal. The button carries the id, so it cannot."""
    from mooting.telegram import parse_rule, rule_callback

    for action in ("ok", "no", "full"):
        data = rule_callback(action, 1234)
        assert len(data.encode()) <= 64, "over Telegram's callback_data limit"
        assert parse_rule(data) == (action, 1234)

    # anything that is not one of ours is left alone rather than guessed at
    for junk in ("", "rule:", "rule:ok", "rule:maybe:1", "rule:ok:abc",
                 "other:ok:1", "rule:ok:1:2"):
        assert parse_rule(junk) is None, junk

    with pytest.raises(ValueError):
        rule_callback("maybe", 1)


def test_a_ruling_from_chat_is_the_pressers_own(board):
    """Whoever tapped the button is not necessarily whoever the bot runs as, and
    a ruling recorded under the wrong name is worse than no ruling."""
    board.add_agent("alice", "human")
    tid = board.open_topic("t", "T", "T", "jeremy", seats=("santa", "jeremy", "alice"))
    pid = board.propose(tid, "santa", "Cap at 6", "body")

    # both are paired in the same room, as themselves
    for user, seat in (("42", "jeremy"), ("77", "alice")):
        board.pair_approve(board.pair_request("-100", user, seat), seat, "jeremy")
    assert board.seat_for_chat("-100", "42") == "jeremy"
    assert board.seat_for_chat("-100", "77") == "alice"

    # Jeremy opened this meeting, so alice may argue in it but not close it.
    with pytest.raises(NotAuthorised):
        board.decide(pid, board.seat_for_chat("-100", "77"), approve=True,
                     rationale="capped at 6")

    # handed the chair, alice presses and alice rules
    board.set_chair(tid, "alice", "jeremy")
    board.decide(pid, board.seat_for_chat("-100", "77"), approve=True,
                 rationale="capped at 6")
    assert board.proposal(pid)["status"] == "approved"
    decisions = [e for e in board.events_since(0, tid) if e.kind == "decision"]
    assert decisions and decisions[-1].actor == "alice", \
        "the ruling was attributed to the wrong person"


def test_an_unpaired_tap_cannot_rule(board):
    """Pairing is the fence, and a button does not get to skip it."""
    tid = board.open_topic("t", "T", "T", "jeremy", seats=("santa", "jeremy"))
    pid = board.propose(tid, "santa", "Cap at 6", "body")

    assert board.seat_for_chat("-100", "9999") is None
    # which is what the callback handler checks before it reaches `decide`
    assert board.proposal(pid)["status"] == "open"


def test_a_second_person_can_speak_not_only_rule(board):
    """`decide` asks only whether you are human; `post` requires a seat on the
    topic. Pairing granted the first and not the second, so the second person in
    a room could approve a plan and not be able to say why."""
    from mooting.store import NotAuthorised

    board.add_agent("alice", "human")
    tid = board.open_topic("t", "T", "T", "jeremy", seats=("santa", "jeremy"))

    assert board.seat(tid, "alice") is None
    assert board.seat_human(tid, "alice") is True, "the seat was not granted"
    assert board.seat_human(tid, "alice") is False, "granted twice"

    board.post(tid, "alice", "what about the windows?")
    assert any(m["author"] == "alice" for m in board.transcript(tid))

    # seating an agent is a decision about whose subscription gets spent
    with pytest.raises(NotAuthorised):
        board.seat_human(tid, "santa")


# ------------------------------------------------------------- topic picker
#
# Switching used to be `/topic switch <slug>`: an identifier retyped from
# memory on a phone keyboard, where a near miss moves the whole room somewhere
# nobody meant. These cover the parts that decide what a tap does, which are
# pure and need no bot.


def test_a_bare_topic_command_asks_for_the_picker_and_a_verb_does_not():
    from mooting.telegram import wants_picker

    assert wants_picker("/topic")
    assert wants_picker("/topics")
    assert wants_picker("  /Topics  ")
    assert wants_picker("/topic@council_bot")
    # A verb still means what it always did.
    assert not wants_picker("/topic new should we cap retries")
    assert not wants_picker("/topic switch retries")
    assert not wants_picker("/topical")


def test_the_picker_marks_where_the_room_is_and_shortens_long_titles():
    from mooting.telegram import picker_rows

    rows = [
        {"id": 3, "slug": "retries", "status": "open",
         "title": "Should failed webhook deliveries use exponential backoff"},
        {"id": 2, "slug": "aircon", "status": "paused", "title": "Aircon"},
        {"id": 1, "slug": "old", "status": "resolved", "title": "Old thing"},
    ]
    labels = [lbl for lbl, _ in picker_rows(rows, "retries")]

    assert labels[0].startswith("●")
    assert "●" not in labels[1]
    assert labels[1].startswith("⏸")
    assert labels[2].startswith("✓")
    # A button is one line on a phone, so a long title is cut at a word.
    assert len(labels[0]) <= 40
    assert labels[0].endswith("…") and not labels[0].endswith(" …")


def test_every_picker_button_carries_its_own_topic_id():
    """A chat scrolls. A button meaning "the third one" would drift with it."""
    from mooting.telegram import parse_pick, pick_callback, picker_rows

    rows = [{"id": 41, "slug": "a", "status": "open", "title": "A"},
            {"id": 7, "slug": "b", "status": "open", "title": "B"}]
    picked = [parse_pick(pick_callback(tid)) for _, tid in picker_rows(rows, "a")]

    assert picked == [41, 7]
    assert all(len(pick_callback(tid).encode("utf-8")) <= 64
               for _, tid in picker_rows(rows, None))
    # A sign-off button is not a picker button, and neither is anything else.
    assert parse_pick("rule:ok:3") is None
    assert parse_pick("") is None
    assert parse_pick("pick:notanumber") is None


def test_the_picker_shows_at_most_one_screen_of_topics():
    from mooting.telegram import PICKER_LIMIT, picker_rows

    many = [{"id": i, "slug": f"t{i}", "status": "open", "title": f"T{i}"}
            for i in range(40)]
    assert len(picker_rows(many, None)) == PICKER_LIMIT


def test_the_help_a_chat_receives_is_the_one_that_is_maintained():
    """`HELP` was defined twice, and the second copy won.

    The live text was the older one, which had lost the line telling somebody
    how to ask to join — the single thing a person who is not paired needs.
    Editing the first copy changed nothing, silently.
    """
    import mooting.telegram as tg

    source = pathlib.Path(tg.__file__).read_text(encoding="utf-8")
    assert source.count("\nHELP = (") == 1, "HELP is defined more than once"
    assert "ask to join" in tg.HELP
    assert "/topics" in tg.HELP


def test_a_chat_still_pointing_at_a_deleted_topic_can_open_a_new_one(tmp_path):
    """Found live: `/reset` cleared the board and the chat went completely mute.

    The room kept standing on the topic it was on. Building the session for the
    next message raised in the constructor, before any command was dispatched,
    so nothing answered at all — including `/topic new`, which was the only way
    back. Two people each read it as "I am not allowed to create a topic".
    """
    from mooting.store import connect
    from mooting.telegram import ChatBoard

    db = tmp_path / "board.db"
    s = connect(db, init=True)
    s.add_agent("Jeremy", "human")
    s.add_agent("Santa", "claude", driver="spawn")
    s.open_topic("gone", "Gone", "brief", "Jeremy", seats=("Jeremy", "Santa"))
    s.clear_topics()
    s.close()

    board = ChatBoard(db, "gone", "Jeremy")
    try:
        assert board.topic is None, "still standing on a topic that is not there"
        out = board.handle("/topic new can we open one after a reset")
        assert "can-we-open-one-after-a-reset" in out
        assert board.topic == "can-we-open-one-after-a-reset"
    finally:
        board.close()


def test_every_command_in_the_menu_actually_exists(tmp_path):
    """The menu is a promise: a thumb taps it and something has to happen.

    `/attach` sat in the help and the Telegram menu for weeks while the console
    answered "unknown /attach", because nothing checked that an advertised
    command was a reachable one. Driving each entry is the check — a command
    that is missing answers "unknown", and one that merely needs arguments
    answers with its usage.
    """
    from mooting.store import connect
    from mooting.telegram import MENU, ChatBoard

    #: Handled by the bot itself rather than by the shared console dispatch.
    bot_side = {"pair", "topics", "help"}

    db = tmp_path / "board.db"
    s = connect(db, init=True)
    s.add_agent("Jeremy", "human")
    s.add_agent("Santa", "claude", driver="spawn")
    s.open_topic("t", "A topic", "brief", "Jeremy", seats=("Jeremy", "Santa"))
    s.close()

    missing = []
    for command, _ in MENU:
        if command in bot_side:
            continue
        board = ChatBoard(db, "t", "Jeremy")
        try:
            out = board.handle(f"/{command}")
        finally:
            board.close()
        if f"unknown /{command}" in out:
            missing.append(command)

    assert not missing, f"advertised in the menu and not reachable: {missing}"


def test_the_destructive_commands_stay_off_the_menu(tmp_path):
    """`/reset` clears every topic, and has already been run here by accident."""
    from mooting.telegram import MENU, OFF_MENU

    listed = {command for command, _ in MENU}
    assert not (listed & set(OFF_MENU)), \
        "a destructive command is one tap from a thumb"


def test_a_meeting_opened_in_a_room_starts_with_that_rooms_team(tmp_path):
    from mooting.store import connect
    from mooting.telegram import ChatBoard

    db = tmp_path / "board.db"
    s = connect(db, init=True)
    s.add_agent("Jeremy", "human")
    for name in ("Santa", "Sam", "Kevin"):
        s.add_agent(name, "claude", driver="spawn")
    s.close()

    room = ("telegram", "-100111")
    chat = ChatBoard(db, None, "Jeremy", room=room)
    try:
        chat.handle("/team Santa Kevin")
        chat.handle("/topic new which engine should we use")
        seats = {r["agent"] for r in chat.console.store.seats(chat.console.topic_id)}
    finally:
        chat.close()

    assert seats == {"Santa", "Kevin", "Jeremy"}, seats
    assert "Sam" not in seats, "a seat outside the team was seated anyway"


def test_two_rooms_on_one_board_seat_their_own_teams(tmp_path):
    """The scenario this exists for: agents with A/B/C in one chat and D/E/F in
    another, on one board, without either team leaking into the other."""
    from mooting.store import connect
    from mooting.telegram import ChatBoard

    db = tmp_path / "board.db"
    s = connect(db, init=True)
    s.add_agent("Jeremy", "human")
    for name in ("Santa", "Sam", "Kevin"):
        s.add_agent(name, "claude", driver="spawn")
    s.close()

    seated = {}
    for room_id, team, question in ((("telegram", "-100111"), "Santa Sam", "engine choice"),
                                    (("telegram", "-100222"), "Kevin", "aircon efficiency")):
        chat = ChatBoard(db, None, "Jeremy", room=room_id)
        try:
            chat.handle(f"/team {team}")
            chat.handle(f"/topic new {question}")
            seated[room_id[1]] = {r["agent"] for r in
                                  chat.console.store.seats(chat.console.topic_id)}
        finally:
            chat.close()

    assert seated["-100111"] == {"Santa", "Sam", "Jeremy"}
    assert seated["-100222"] == {"Kevin", "Jeremy"}


def test_seating_somebody_for_one_meeting_does_not_join_them_to_the_team(tmp_path):
    """`/seats add` is the temporary gesture; `/team` is the one that sticks."""
    from mooting.store import connect
    from mooting.telegram import ChatBoard

    db = tmp_path / "board.db"
    s = connect(db, init=True)
    s.add_agent("Jeremy", "human")
    for name in ("Santa", "Sam"):
        s.add_agent(name, "claude", driver="spawn")
    s.close()

    room = ("telegram", "-100111")
    chat = ChatBoard(db, None, "Jeremy", room=room)
    try:
        chat.handle("/team Santa")
        chat.handle("/topic new first question")
        chat.handle("/seats add Sam")
        first = {r["agent"] for r in chat.console.store.seats(chat.console.topic_id)}
        assert "Sam" in first, "the temporary seat did not take"

        # The next meeting starts from the team, not from what the last one grew into.
        chat.handle("/topic new second question")
        second = {r["agent"] for r in chat.console.store.seats(chat.console.topic_id)}
        assert second == {"Santa", "Jeremy"}, second
    finally:
        chat.close()


def test_a_terminal_session_has_a_room_of_its_own(tmp_path):
    """No chat behind it, and still not a special case."""
    from mooting.console import Console
    from mooting.store import Store, connect

    db = tmp_path / "board.db"
    s = connect(db, init=True)
    s.add_agent("me", "human")
    s.add_agent("claude", "claude", driver="spawn")
    s.close()

    c = Console(db, None, "me")
    try:
        assert c.room == Store.LOCAL_ROOM
        c.emit = lambda *_: None
        c.handle("/team claude")
        c.handle("/topic new a question at the desk")
        assert {r["agent"] for r in c.store.seats(c.topic_id)} == {"claude", "me"}
        # and it is a different room from any chat
        assert c.store.room_team(c.store.ensure_room("telegram", "-100111")) == []
    finally:
        c.store.close()
