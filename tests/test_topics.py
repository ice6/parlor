"""E2E practice-scene coverage: the catalogue, the spoken opening line,
scene→conversation grounding, and on-demand generation.

Not automatable here (needs a browser): the picker overlay itself, and
that the session really refuses to listen until a scene is chosen.
"""

from util import Session, assert_spoken_prose, audio


def ready(session) -> dict:
    """The catalogue frame the server pushes on connect."""
    msg = session.wait_for("session_ready", timeout=10)
    assert msg, "no session_ready — the picker would have nothing to show"
    return msg


def pick(session, topic_id: str, speak_opening: bool = True) -> dict | None:
    session.send({"type": "set_topic", "id": topic_id,
                  "speak_opening": speak_opening})
    return session.wait_for("topic_changed", timeout=15)


# ── The catalogue ─────────────────────────────────────────────────────────

def test_session_ready_carries_the_catalogue(session):
    msg = ready(session)
    # Self-consistency, not a pinned list: adding a category to topics.py
    # is an ordinary edit and must not need a test change. What matters is
    # that every card belongs to a category the picker was told about —
    # one that isn't leaves cards unreachable behind the filter chips.
    assert len(msg["categories"]) >= 8
    assert {t["category"] for t in msg["topics"]} == set(msg["categories"]), \
        "a card's category is missing from the picker's chip row"
    assert msg["topic"] is None, "a fresh session starts with no scene"
    assert {l["name"] for l in msg["levels"]} == \
        {"beginner", "intermediate", "advanced"}
    for card in msg["topics"]:
        assert card["id"] and card["title"] and card["scene"]
        assert card["role"] and card["user_role"]
        # The card must NOT carry the hand-written first line: showing it
        # spoils the one moment the scene has to feel self-started.
        assert "opening" not in card, f"opening leaked: {card}"


# ── The opening line ──────────────────────────────────────────────────────

def test_set_topic_speaks_an_opening_line(session):
    ready(session)
    changed = pick(session, "cafe_order")
    assert changed and changed["topic"]["id"] == "cafe_order"

    t = session.collect_turn(90)
    assert t.marker == "complete", "the scene opened in silence"
    assert t.audio_chunks > 0
    assert_spoken_prose(t.text)
    assert t.text.rstrip().endswith("?"), \
        f"an opening the user can't answer: {t.text!r}"


def test_opening_can_be_suppressed(session):
    """The reconnect path. A regression here re-greets the user on every
    dropped socket, which the browser reopens every 2 seconds."""
    ready(session)
    changed = pick(session, "cafe_order", speak_opening=False)
    assert changed and changed["topic"]["id"] == "cafe_order"
    assert session.wait_for("audio_start", timeout=6) is None, \
        "spoke an opening line on a silent restore"


def test_freeform_still_greets(session):
    ready(session)
    assert pick(session, "freeform")
    t = session.collect_turn(90)
    assert t.marker == "complete" and t.audio_chunks > 0
    assert_spoken_prose(t.text)


def test_unknown_topic_is_ignored(session):
    ready(session)
    session.send({"type": "set_topic", "id": "definitely_not_a_topic"})
    assert session.wait_for("topic_changed", timeout=5) is None
    # …and the session is still usable, not wedged on the bad id.
    session.send({"type": "ready"})
    t = session.turn(audio("capital_france"))
    assert "paris" in t.text.lower()


# ── The scene reaches the conversation ────────────────────────────────────

def test_topic_carries_into_the_conversation(session):
    ready(session)
    assert pick(session, "cafe_order")
    session.collect_turn(90)  # drain the opening

    t = session.turn(audio("topic_reply_coffee"))
    assert t.marker == "complete"
    assert_spoken_prose(t.text)
    reply = t.text.lower()
    assert any(w in reply for w in
               ("oat", "milk", "flat white", "size", "here", "to go", "name")), \
        f"reply is not in the café scene: {t.text!r}"


def test_topic_change_resets_the_scene(server, session):
    """Asserted on the server log, not on the model's memory: a "did it
    forget?" assertion against an LLM is a coin flip."""
    server.require_managed()
    ready(session)
    assert pick(session, "cafe_order", speak_opening=False)
    assert pick(session, "gate_change", speak_opening=False)
    assert "Topic → gate_change (history reset)" in server.log()


# ── Generation ────────────────────────────────────────────────────────────

def test_generate_topics_returns_playable_cards(session):
    ready(session)
    session.send({"type": "generate_topics", "category": "dining", "n": 3})
    gen = session.wait_for("topics_generated", timeout=120)
    assert gen and gen["category"] == "dining"
    cards = gen["topics"]
    assert 1 <= len(cards) <= 4, f"unusable batch: {cards}"
    assert len({c["id"] for c in cards}) == len(cards), "duplicate ids"
    for c in cards:
        assert c["scene"] and c["role"] and c["user_role"]
        assert c["generated"] is True
        # "they play the user" is nonsense once SCENARIO_NOTE interpolates
        # it — the generation prompt bans it explicitly.
        assert c["user_role"].strip().lower() != "the user", c

    # A generated card must actually be playable, not just well-formed.
    assert pick(session, cards[0]["id"])
    t = session.collect_turn(90)
    assert t.marker == "complete" and t.audio_chunks > 0
    assert_spoken_prose(t.text)


def test_generation_is_refused_once_a_conversation_exists(session):
    """It shares llama-server's single slot: generating mid-session evicts
    the conversation's prefix cache and the next turn pays a full
    re-prefill of history plus every stored audio clip."""
    ready(session)
    session.turn(audio("capital_france"))
    session.send({"type": "generate_topics", "category": "dining", "n": 3})
    gen = session.wait_for("topics_generated", timeout=30)
    assert gen and gen["topics"] == []


def test_generated_topics_are_cached(server, session):
    """A scene invented once is free forever after — including for the
    next connection, which is what makes the cache worth its risk."""
    ready(session)
    session.send({"type": "generate_topics", "category": "travel", "n": 2})
    gen = session.wait_for("topics_generated", timeout=120)
    assert gen and gen["topics"], "nothing generated to cache"
    fresh_ids = {c["id"] for c in gen["topics"]}

    with Session(server.url) as second:
        catalogue = {c["id"] for c in ready(second)["topics"]}
    assert fresh_ids <= catalogue, "generated scenes did not survive the session"
