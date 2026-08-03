"""The served page is templated (server.root substitutes the model label,
the pinned CDN versions, and the avatar config). Nothing tested that until
the avatar added four more placeholders — one unsubstituted {{…}} is a
broken import map and a dead avatar, with no server-side symptom at all.
"""

import urllib.request


def fetch(server, path: str) -> tuple[int, str]:
    url = server.url.replace("ws://", "http://").removesuffix("/ws") + path
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read().decode()


def test_index_has_no_unsubstituted_placeholders(server):
    status, html = fetch(server, "/")
    assert status == 200
    assert "{{" not in html, "a template placeholder reached the browser"


def test_index_carries_the_avatar_and_picker(server):
    _, html = fetch(server, "/")
    for marker in ('id="avatar"', 'id="topicOverlay"', 'id="topicChip"',
                   'src="/static/avatar.js"'):
        assert marker in html, f"missing from the page: {marker}"


def test_avatar_script_is_served(server):
    status, body = fetch(server, "/static/avatar.js")
    assert status == 200 and body.strip()
    # The seam is the contract app.js relies on — if the module stops
    # registering itself, the face silently never appears.
    assert "window.setAvatarBackend" in body
