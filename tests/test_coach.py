"""E2E English-coach coverage: level switching, one question per turn,
recast-not-lecture correction, and that none of it leaks into a mode whose
turn instruction replaces the respond prompt wholesale.
"""

from util import assert_spoken_prose, audio, switch_by_voice

LECTURE_WORDS = ("mistake", "incorrect", "wrong", "should say", "should have said",
                 "correction", "grammar")


def set_level(session, name: str) -> dict | None:
    session.send({"type": "set_level", "level": name})
    return session.wait_for("level_changed", timeout=10)


def start_scene(session, topic_id: str = "cafe_order", level: str = "beginner"):
    assert session.wait_for("session_ready", timeout=10)
    assert set_level(session, level)
    session.send({"type": "set_topic", "id": topic_id, "speak_opening": False})
    assert session.wait_for("topic_changed", timeout=15)


def test_level_change_is_acknowledged(session):
    assert session.wait_for("session_ready", timeout=10)
    assert set_level(session, "beginner") == {"type": "level_changed",
                                              "level": "beginner"}
    assert set_level(session, "advanced")["level"] == "advanced"
    # Anything unknown — including the UI's "no coaching" — turns it off
    # rather than erroring, so a stale client can never wedge a session.
    assert set_level(session, "")["level"] is None


def test_coach_asks_one_question_per_turn(session):
    start_scene(session)
    for fixture in ("topic_reply_coffee", "learner_error"):
        t = session.turn(audio(fixture))
        assert t.marker == "complete"
        assert_spoken_prose(t.text)
        assert t.text.count("?") <= 1, \
            f"stacked questions — the learner answers only the last: {t.text!r}"


def test_coach_recasts_instead_of_lecturing(session):
    start_scene(session, topic_id="freeform")
    t = session.turn(audio("learner_error"))
    assert t.marker == "complete"
    reply = t.text.lower()
    # The negative half is the robust half: whatever else it does, it must
    # not name the error — that is what makes a learner go quiet.
    assert not any(w in reply for w in LECTURE_WORDS), \
        f"lectured instead of recasting: {t.text!r}"
    assert "went" in reply or "bought" in reply, \
        f"no recast of the past tense at all: {t.text!r}"


def test_coach_does_not_leak_into_translate(session):
    """translate/listen replace the turn instruction wholesale. Coaching is
    a system-prompt clause, so it outlives that replacement unless the note
    says otherwise — this is the test that earns COACH_NOTE's last
    sentence. If it ever fails, the fallback is rebuilding the system
    prompt on switch_mode (drop the scene/coach clauses on entry, restore
    them on exit)."""
    start_scene(session, level="advanced")
    switch_by_voice(session, "cmd_translate", "translate")

    t = session.turn(audio("filler_garden"))
    assert t.marker == "complete"
    rendering = t.text.lower()
    assert "?" not in t.text, \
        f"appended a coaching question to a translation: {t.text!r}"
    assert not any(w in rendering for w in LECTURE_WORDS), \
        f"coaching commentary in a translation: {t.text!r}"
