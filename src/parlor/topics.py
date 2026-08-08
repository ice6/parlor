"""Practice scenarios: the scene a session role-plays, so a learner has
something concrete to say instead of staring at a live mic.

A topic is data, not behavior — the same discipline as modes.py. It
contributes exactly one clause to the system prompt (server.SCENARIO_NOTE)
and nothing else: no gating, no new turn shape, no state the turn loop has
to consult. The seed catalogue below covers eight everyday situations;
anything past it is invented on demand by the same local model through a
grammar-forced schema and cached on disk, so a scene invented once is free
forever after.
"""

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from parlor import llama


@dataclass(frozen=True)
class Topic:
    id: str          # stable slug — what set_topic carries over the wire
    category: str
    title: str       # card headline, 2-4 words
    scene: str       # ONE sentence of situation, interpolated into SCENARIO_NOTE
    role: str        # who Parlor plays ("the barista taking their order")
    user_role: str   # who the user plays
    # A hand-written first line. It is the opening turn's FALLBACK only, and
    # is never sent to the client: showing the first line on the card spoils
    # the one moment the scene has to feel like it started on its own.
    opening: str
    generated: bool = False


CATEGORIES = {
    "business": "Business",
    "dining": "Dining",
    "sports": "Sports",
    "travel": "Travel",
    "daily": "Daily life",
    "health": "Healthcare",
    "shopping": "Shopping",
    "tech": "Tech",
    "kids": "Kids",
}

# Three per category (kids has more — one audience, more everyday moments
# worth covering). Every scene sentence starts "The user is…" and says
# both where they are and what they WANT — a scene with no want gives the
# learner nothing to push against, and the conversation dies in two turns.
SEED = [
    # ── Business ──
    Topic("standup", "business", "Daily standup",
          "The user is giving their update in a short remote engineering standup.",
          "their team lead running the standup",
          "an engineer giving a status update",
          "Alright, let's go round — what did you get to yesterday?"),
    Topic("job_interview", "business", "Job interview",
          "The user is interviewing for a job they want and has just sat down.",
          "the hiring manager conducting the interview",
          "a candidate interviewing for a role they really want",
          "Thanks for coming in. So — tell me what you're working on right now."),
    Topic("raise_ask", "business", "Asking for a raise",
          "The user has booked time with their manager to ask for a raise.",
          "their manager, fair but budget-conscious",
          "an employee making the case for more money",
          "You said you wanted to talk about something — what's on your mind?"),

    # ── Dining ──
    Topic("cafe_order", "dining", "Ordering coffee",
          "The user is at the counter of a busy neighbourhood café at morning rush.",
          "the barista taking their order",
          "a customer ordering coffee and something to eat",
          "Morning! What can I get started for you?"),
    Topic("send_it_back", "dining", "Sending a dish back",
          "The user's main course arrived cold and not what they ordered.",
          "the waiter who is looking after their table",
          "a diner who needs to send a dish back without making a scene",
          "How is everything over here — all good?"),
    Topic("dinner_plans", "dining", "Picking a restaurant",
          "The user is deciding with a friend where to eat tonight, and they disagree.",
          "a close friend with strong opinions about food",
          "someone trying to talk their friend into a different restaurant",
          "Okay, I'm starving. Where are we going — and don't say the noodle place again."),

    # ── Sports ──
    Topic("gym_intro", "sports", "First day at the gym",
          "The user has just joined a gym and wants a programme that fits their week.",
          "a trainer doing their intake session",
          "someone new to the gym with a goal in mind",
          "Welcome in! So what are we trying to get out of this?"),
    Topic("pickup_game", "sports", "Joining a game",
          "The user walks up to a pickup basketball game in a park and wants in.",
          "a regular player already on the court",
          "a newcomer asking to join the next game",
          "Hey — you looking to run? We've got next in about five minutes."),
    Topic("match_talk", "sports", "Talking about a match",
          "The user is watching a match in a bar next to a fan of the other team.",
          "a friendly fan of the opposing team",
          "someone making conversation about the game",
          "Well, that was a terrible call. You watching this properly, or just here for the beer?"),

    # ── Travel ──
    Topic("gate_change", "travel", "A gate change",
          "The user's connecting flight moved gates and boarding closes in twenty minutes.",
          "an airline agent at the service desk",
          "a traveller trying to make a tight connection",
          "Sorry about the change — let me see what I can do. Where are you flying to?"),
    Topic("hotel_checkin", "travel", "Hotel check-in",
          "The user is checking into a hotel and the room they booked is not available.",
          "the receptionist at the front desk",
          "a tired guest checking in after a long flight",
          "Good evening — welcome. Could I get the name on the booking?"),
    Topic("lost_directions", "travel", "Asking directions",
          "The user is lost in an unfamiliar city and their phone is dead.",
          "a local who happens to be walking past",
          "a visitor trying to find their way back",
          "You look a bit lost — need a hand?"),

    # ── Daily life ──
    Topic("new_neighbour", "daily", "Meeting a neighbour",
          "The user has just moved in and runs into their neighbour in the hallway.",
          "the neighbour from the flat next door",
          "someone who moved in a few days ago",
          "Oh — you're the new one, aren't you? How's the move going?"),
    Topic("reschedule_call", "daily", "Rescheduling plans",
          "The user has to cancel plans tonight and doesn't want to upset a friend.",
          "the friend they were supposed to meet",
          "someone who has to back out of tonight's plans",
          "Hey! Still on for tonight, right? I've been looking forward to this all week."),
    Topic("party_smalltalk", "daily", "Small talk at a party",
          "The user is at a party where they only know the host.",
          "another guest who also barely knows anyone",
          "someone making conversation with a stranger",
          "I don't think we've met — how do you know the host?"),

    # ── Healthcare ──
    Topic("pharmacy", "health", "At the pharmacy",
          "The user is at a pharmacy counter describing symptoms and asking what to take.",
          "the pharmacist on duty",
          "someone who has not been feeling well for a few days",
          "Hi there — what seems to be the trouble?"),
    Topic("doctor_visit", "health", "Seeing a doctor",
          "The user is at a clinic about a pain that has not gone away in three weeks.",
          "the doctor seeing them",
          "a patient describing a problem that won't go away",
          "Come in, have a seat. So what's been going on?"),
    Topic("dentist_call", "health", "Booking a dentist",
          "The user is phoning a dental clinic with a broken tooth and no appointment.",
          "the receptionist answering the phone",
          "someone who needs to be seen today",
          "Good morning, dental surgery — how can I help?"),

    # ── Shopping ──
    Topic("return_item", "shopping", "Returning something",
          "The user is returning something that broke, without the receipt.",
          "the shop assistant handling returns",
          "a customer returning a faulty item",
          "Hi — returns or exchange today?"),
    Topic("buying_shoes", "shopping", "Buying shoes",
          "The user is buying running shoes and is not sure what they need.",
          "a shop assistant who actually knows about running",
          "a customer who has never bought proper running shoes",
          "Anything I can help you find? Are these for the road or the trail?"),
    Topic("market_haggle", "shopping", "Haggling at a market",
          "The user has found something they want at a market stall and the price is high.",
          "the stallholder, cheerful and not in a hurry to drop the price",
          "a shopper trying to talk the price down",
          "Good eye, that one. For you — a very good price. How many are you taking?"),

    # ── Tech ──
    Topic("internet_down", "tech", "Internet is down",
          "The user's home internet has been dropping for three days and they've called support.",
          "a support agent working through a script",
          "a customer who has already tried restarting the router twice",
          "Thanks for holding. Can you confirm the address on the account for me?"),
    Topic("explain_bug", "tech", "Explaining a bug",
          "The user has to explain a bug to a colleague who did not write the code.",
          "a colleague who needs to understand the problem, not the code",
          "an engineer explaining what broke and why it matters",
          "You said something's on fire — walk me through it. What's actually breaking?"),
    Topic("phone_plan", "tech", "Changing a phone plan",
          "The user is in a phone shop trying to move to a cheaper plan without losing their number.",
          "a salesperson who would rather sell them an upgrade",
          "a customer who wants to pay less, not more",
          "Hi! Looking for a new handset today?"),

    # ── Kids ── (short, everyday moments for a young learner — simple
    # words, warm and encouraging adults, nothing that needs small talk
    # about jobs or money)
    Topic("lunch_line", "kids", "School lunch",
          "The user is in the school cafeteria line and can't decide what to get for lunch.",
          "the lunch lady serving today's choices",
          "a student deciding what to eat",
          "Hey there — chicken or pasta today, or are we doing both?"),
    Topic("new_friend", "kids", "Making a new friend",
          "The user is standing alone at recess and sees a new kid who just moved to town.",
          "a new kid at school who looks a little nervous",
          "a student saying hello for the first time",
          "Hi, I'm new here — is it okay if I stand with you?"),
    Topic("show_and_tell", "kids", "Show and tell",
          "The user brought something special from home to share with the class today.",
          "the teacher running show and tell",
          "a student about to show something they love",
          "Okay, who wants to go first and tell us what they brought?"),
    Topic("borrow_toy", "kids", "Borrowing a toy",
          "The user really wants a turn with a toy their friend brought to the playground.",
          "a friend playing with a toy the user wants to try",
          "someone hoping to take a turn",
          "You want a turn? Ask nicely and maybe I'll let you."),
    Topic("birthday_plan", "kids", "Planning a birthday party",
          "The user is turning ten soon and is telling a friend what they want for their party.",
          "a friend who is excited to help plan",
          "someone planning their own birthday party",
          "Okay, your birthday is coming up — what kind of party are we talking about?"),
    Topic("pet_store", "kids", "Picking a pet",
          "The user is at a pet store trying to convince their parent to get a hamster.",
          "a pet store worker who loves hamsters",
          "a kid hoping to bring home a new pet",
          "Thinking about a hamster? Want to hold one and see how you like it?"),
    Topic("library_help", "kids", "Finding a book",
          "The user is at the library looking for a book about dinosaurs for a school project.",
          "the librarian who helps kids find books",
          "a student working on a dinosaur project",
          "Dinosaurs, huh? Do you want the ones with pictures, or the ones with all the facts?"),
    Topic("grandma_call", "kids", "Calling grandma",
          "The user is on a video call with their grandmother, telling her about their day at school.",
          "their grandmother, happy to hear from them",
          "a kid sharing what happened at school today",
          "There's my favorite person! Tell me — what was the best part of your day?"),
    Topic("lost_jacket", "kids", "A lost jacket",
          "The user can't find their jacket after recess and is checking the school lost and found.",
          "the school office worker who keeps the lost and found box",
          "a student trying to find their missing jacket",
          "Lost something? Let's look in the box — what does it look like?"),
]

# Not a scene: the picker's escape hatch. It carries no SCENARIO_NOTE at
# all — an empty scene must add ZERO tokens to the system prompt, not an
# empty clause the model then tries to obey.
FREEFORM = Topic("freeform", "", "Just chat", "", "", "",
                 "Hey — good to hear you. What's on your mind today?")


# ── Generation ────────────────────────────────────────────────────────────

TOPIC_SCHEMA = {
    "type": "object",
    "properties": {
        "topics": {
            "type": "array", "minItems": 1, "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "scene": {"type": "string"},
                    "role": {"type": "string"},
                    "user_role": {"type": "string"},
                    "opening": {"type": "string"},
                },
                "required": ["title", "scene", "role", "user_role", "opening"],
            },
        },
    },
    "required": ["topics"],
}

# Grammar-forced like the action head (actions.py): the shape is guaranteed
# to parse, so the only failure left is a dull scene, never a crash in the
# picker. Temperature is HIGH here, unlike every other call in Parlor — the
# whole point is that pressing the button twice gives two different scenes.
# "avoid" carries the titles already on screen: without it the model
# regenerates "Ordering coffee" every time, deterministically.
# The role/user_role wording is spelled out because the model's first
# instinct is to write "The user" and "A guest" — which read as nonsense
# once server.SCENARIO_NOTE interpolates them ("they play The user"). The
# ban on naming English or practice is the same class of leak: a scene that
# says "wanting to practise conversational English" makes the model explain
# the exercise instead of playing the barista. Both were observed live on
# the first generation run.
GENERATE_PROMPT = (
    "You write role-play scenes for someone practising spoken English. "
    "Invent {n} DIFFERENT {category} situations, each one an ordinary "
    "everyday moment two people would actually talk through — not a "
    "lesson, not a quiz. For each: title, 2-4 words; scene, one sentence "
    'starting "The user is" that says where they are and what they want; '
    "role, the person the assistant plays, as a lowercase noun phrase like "
    '"the barista taking their order"; user_role, the person the user '
    'plays, as a lowercase noun phrase like "a customer in a hurry"; '
    "opening, the first line the assistant says out loud, ending in a "
    "question. Never mention English, practice, learning or the exercise "
    "itself — inside the scene these people are just talking. Never write "
    '"the user" as a role. Do not repeat any of these: {avoid}.'
)

MAX_CACHED = 60  # this is a cache, not a library
CACHE_VERSION = 1
_lock = threading.Lock()  # generation runs in an executor thread
_cache: list[Topic] | None = None


def _cache_path() -> Path:
    override = os.environ.get("PARLOR_TOPICS_CACHE")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "parlor" / "topics.json"


def _load_cache() -> list[Topic]:
    """Read the generated-topic cache. A corrupt or unreadable file is
    logged and ignored, never raised: the picker must open even when the
    cache is garbage."""
    global _cache
    if _cache is not None:
        return _cache
    _cache = []
    path = _cache_path()
    try:
        if path.exists():
            data = json.loads(path.read_text())
            if data.get("version") == CACHE_VERSION:
                _cache = [Topic(id=t["id"], category=t["category"],
                                title=t["title"], scene=t["scene"],
                                role=t["role"], user_role=t["user_role"],
                                opening=t["opening"], generated=True)
                          for t in data.get("topics", [])]
    except Exception as e:
        print(f"Topic cache unreadable ({path}): {e} — starting empty")
        _cache = []
    return _cache


def remember(new: list[Topic]) -> None:
    """Append generated topics to the cache, atomically. Oldest entries are
    dropped past MAX_CACHED — a scene nobody has picked in months is not
    worth carrying."""
    global _cache
    if not new:
        return
    with _lock:
        kept = (_load_cache() + list(new))[-MAX_CACHED:]
        _cache = kept
        path = _cache_path()
        payload = {"version": CACHE_VERSION, "created": time.time(),
                   "topics": [{"id": t.id, "category": t.category,
                               "title": t.title, "scene": t.scene,
                               "role": t.role, "user_role": t.user_role,
                               "opening": t.opening} for t in kept]}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
            tmp.replace(path)  # atomic: a crash mid-write can't truncate it
        except OSError as e:
            print(f"Could not write topic cache ({path}): {e}")


def catalogue() -> list[Topic]:
    """Every playable scene: the seeds, then whatever has been generated.
    Deduped by id, seeds winning — a generated scene must never shadow a
    hand-written one."""
    seen = {t.id for t in SEED}
    return SEED + [t for t in _load_cache() if t.id not in seen]


def get(topic_id: str) -> Topic | None:
    if topic_id == FREEFORM.id:
        return FREEFORM
    return next((t for t in catalogue() if t.id == topic_id), None)


def as_card(t: Topic) -> dict:
    """The client's view of a topic — deliberately WITHOUT `opening`."""
    return {"id": t.id, "category": t.category, "title": t.title,
            "scene": t.scene, "role": t.role, "user_role": t.user_role,
            "generated": t.generated}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "scene"


def _scenes(raw: str) -> list[dict]:
    """Every complete scene object in a grammar-forced response.

    Not json.loads on the whole thing: Gemma spends an unbounded reasoning
    block before the JSON (~250 tokens, and it keeps doing so despite
    enable_thinking=False — the text is stripped from `content` but still
    spends the budget), so a batch intermittently comes back cut off
    mid-object, or empty. The grammar guarantees every object that DID
    finish is well-formed, so scanning brace depth recovers two scenes out
    of a truncated three instead of throwing all of them away. Measured:
    at max_tokens=512 this fired on most batches, at 2048 it is rare —
    but "rare" on an interactive button is still a dead button."""
    scenes: list[dict] = []
    opens: list[int] = []  # positions of currently-unclosed braces
    for i, ch in enumerate(raw):
        if ch == "{":
            opens.append(i)
        elif ch == "}" and opens:
            # The innermost span that just closed — a scene object sits at
            # depth 2 (inside {"topics": [...]}), so matching on the
            # payload's shape beats guessing at a depth.
            try:
                obj = json.loads(raw[opens.pop():i + 1])
            except ValueError:
                continue
            if isinstance(obj, dict) and "title" in obj:
                scenes.append(obj)
    return scenes


def generate(category: str, n: int, avoid: list[str]) -> list[Topic]:
    """Invent n new scenes in a category (BLOCKING — run in an executor).
    Entries missing any field are dropped rather than shipped half-built:
    a card with no role produces a scene the model cannot play."""
    label = CATEGORIES.get(category, category)
    prompt = GENERATE_PROMPT.format(n=max(1, min(4, n)), category=label,
                                    avoid=", ".join(avoid[:20]) or "none")
    # Generous because this runs while the picker is open, never on the
    # turn path, and the reasoning block above has to fit inside it too.
    raw = llama.chat_blocking([{"role": "user", "content": prompt}],
                              max_tokens=2048, temperature=0.9,
                              json_schema=TOPIC_SCHEMA)
    taken = {t.id for t in catalogue()}
    out: list[Topic] = []
    for item in _scenes(raw):
        fields = {k: str(item.get(k, "")).strip()
                  for k in ("title", "scene", "role", "user_role", "opening")}
        if not all(fields.values()):
            print(f"Generated topic dropped (incomplete): {item!r}")
            continue
        base = f"{category}_{_slug(fields['title'])}"
        topic_id, i = base, 2
        while topic_id in taken:
            topic_id, i = f"{base}_{i}", i + 1
        taken.add(topic_id)
        out.append(Topic(id=topic_id, category=category, generated=True, **fields))
    print(f"Generated {len(out)} {category} scenes")
    return out
