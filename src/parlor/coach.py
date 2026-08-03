"""The English-coach layer: HOW a reply is worded, orthogonal to what the
turn is.

Deliberately NOT a Mode (modes.py). A mode chooses what a turn IS — whether
smart-turn gates it, whether the camera is read, whether a research answer
may land, which prompt replaces the respond instruction — and exactly one
is active at a time. Coaching changes none of that: a coached turn is an
ordinary conversation turn with different wording, and it has to MULTIPLY
with both the scene and the mode (a beginner in the café scene who then
asks for a timer is one session, not three). Modelling it as a Mode would
need 'coach', 'coach_translate' and 'coach_listen' entries plus a matching
clause in actions._MODE_CLAUSES for each — a combinatorial table in
exchange for what is, in the end, one clause in the system prompt.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Level:
    name: str        # beginner | intermediate | advanced
    label: str       # UI, and interpolated into server.COACH_NOTE
    register: str    # vocabulary, pace, what to push the learner on
    sentences: str   # spoken length budget, phrased for the prompt


LEVELS = {l.name: l for l in [
    Level("beginner", "beginner",
          "Use short, common words and simple present or past tense, one "
          "idea per sentence; when they don't follow, rephrase rather than "
          "repeat.",
          "one or two short sentences"),
    Level("intermediate", "intermediate",
          "Use everyday vocabulary at a normal pace with natural "
          "contractions, and slip in one useful phrase or idiom when it "
          "genuinely fits.",
          "two or three short sentences"),
    # The point of 'advanced' is friction: a partner who agrees with
    # everything gives an advanced learner nothing to stretch against.
    Level("advanced", "advanced",
          "Speak like a native at full pace — idioms, phrasal verbs, asides "
          "— and push back or ask for detail so they have to defend an "
          "opinion.",
          "two to four short sentences"),
]}

# The level a fresh session starts at. "off" (or any unknown name) disables
# coaching entirely: Parlor is still a good conversation partner without
# it, and a user who just wants to talk shouldn't have to click it away.
DEFAULT = os.environ.get("COACH_LEVEL", "intermediate")


def default_level() -> Level | None:
    return LEVELS.get(DEFAULT)
