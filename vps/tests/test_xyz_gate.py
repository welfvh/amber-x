"""Tests for the xyz post gate (vps/xyz_gate.py), with a fake X API.

Run: cd vps && uv run --with-requirements requirements.txt --with pytest --with httpx pytest tests
"""

import hashlib
import importlib
import sys
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient
from urllib3.exceptions import MaxRetryError, NameResolutionError, NewConnectionError, ProtocolError

VPS = Path(__file__).resolve().parents[1]
POST_TOKEN = "post-token-0123456789"
DRY_TOKEN = "dry-token-0123456789"
MASTER = "master-token-0123456789"


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = str(self._body)

    def json(self):
        return self._body


class FakeX:
    """Records calls; answers like X's media upload v2 and /2/tweets."""

    def __init__(self):
        self.calls = []
        self.next_media = 1000
        self.tweet_status = 201
        self.tweet_body = None
        self.tweet_raises = None
        self.status_states = []  # successive STATUS answers for a video
        self.fail = {}  # path prefix -> FakeResponse

    def __call__(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        for prefix, resp in self.fail.items():
            if path.startswith(prefix):
                return resp
        if path == "/media/upload/initialize":
            self.next_media += 1
            return FakeResponse(200, {"data": {"id": str(self.next_media), "expires_after_secs": 86400}})
        if path.endswith("/append"):
            return FakeResponse(204)
        if path.endswith("/finalize"):
            if self.status_states:
                return FakeResponse(200, {"data": {"processing_info": {"state": "pending", "check_after_secs": 1}}})
            return FakeResponse(200, {"data": {"id": path.split("/")[3]}})
        if path == "/media/upload":
            state = self.status_states.pop(0) if self.status_states else "succeeded"
            return FakeResponse(200, {"data": {"processing_info": {"state": state, "progress_percent": 50}}})
        if path == "/tweets":
            if self.tweet_raises:
                raise self.tweet_raises
            body = self.tweet_body or {"data": {"id": "1977000000000000001", "text": "x"}}
            return FakeResponse(self.tweet_status, body)
        raise AssertionError(f"unexpected X call {method} {path}")

    def tweets(self):
        return [c for c in self.calls if c[1] == "/tweets"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XVP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XVP_AUTH_TOKEN", MASTER)
    monkeypatch.setenv("XYZ_POST_TOKEN_SHA256", hashlib.sha256(POST_TOKEN.encode()).hexdigest())
    monkeypatch.setenv("XYZ_DRYRUN_TOKEN_SHA256", hashlib.sha256(DRY_TOKEN.encode()).hexdigest())
    for key in ("BEARER_TOKEN", "API_KEY", "API_SECRET", "ACCESS_TOKEN", "ACCESS_TOKEN_SECRET"):
        monkeypatch.setenv(key, "test")
    monkeypatch.syspath_prepend(str(VPS))
    for mod in ("xyz_gate", "main"):
        sys.modules.pop(mod, None)
    gate = importlib.import_module("xyz_gate")
    fake = FakeX()
    monkeypatch.setattr(gate, "x_request", fake)
    return gate, fake


@pytest.fixture
def client(env):
    gate, _ = env
    from fastapi import FastAPI

    app = FastAPI()
    app.mount("/xyz", gate.xyz_app)
    return TestClient(app)


def auth(token=POST_TOKEN):
    return {"Authorization": f"Bearer {token}"}


def upload(client, data: bytes, media_type="image/jpeg", token=POST_TOKEN, chunk=4):
    r = client.post("/xyz/media", json={"media_type": media_type, "total_bytes": len(data)}, headers=auth(token))
    assert r.status_code == 200, r.text
    media_id = r.json()["media_id"]
    for i in range(0, len(data), chunk):
        r = client.put(f"/xyz/media/{media_id}/segments/{i // chunk}", content=data[i : i + chunk], headers=auth(token))
        assert r.status_code == 200, r.text
    r = client.post(f"/xyz/media/{media_id}/finalize", headers=auth(token))
    assert r.status_code == 200, r.text
    return media_id, r.json()


# ── auth ─────────────────────────────────────────────────────

def test_ping_needs_a_gate_token(client):
    assert client.get("/xyz/ping").status_code == 401
    assert client.get("/xyz/ping", headers=auth("wrong")).status_code == 401
    assert client.get("/xyz/ping", headers=auth(MASTER)).status_code == 401
    r = client.get("/xyz/ping", headers=auth())
    assert r.status_code == 200 and r.json()["dry_run"] is False
    assert client.get("/xyz/ping", headers=auth(DRY_TOKEN)).json()["dry_run"] is True


def test_bad_token_fails_before_x(client, env):
    _, fake = env
    r = client.post("/xyz/posts", json={"client_id": "abcdefgh-1", "text": "hi"}, headers=auth("nope"))
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"
    r = client.post("/xyz/media", json={"media_type": "image/jpeg", "total_bytes": 3}, headers=auth("nope"))
    assert r.status_code == 401
    assert fake.calls == []


def test_gate_token_opens_nothing_else_in_main(env):
    main = importlib.import_module("main")
    c = TestClient(main.app)
    # the phone's token cannot use the full service
    for method, path in [("get", "/status"), ("get", "/timeline"), ("get", "/bookmarks"), ("delete", "/tweet/1")]:
        assert getattr(c, method)(path, headers=auth()).status_code == 401, path
    # the master token cannot use the gate
    assert c.get("/xyz/ping", headers=auth(MASTER)).status_code == 401
    assert c.get("/xyz/ping", headers=auth()).status_code == 200


# ── media ────────────────────────────────────────────────────

def test_image_upload_is_init_append_finalize(client, env):
    _, fake = env
    media_id, status = upload(client, b"0123456789")
    assert status["state"] == "succeeded"
    paths = [c[1] for c in fake.calls]
    assert paths == [
        "/media/upload/initialize",
        f"/media/upload/{media_id}/append",
        f"/media/upload/{media_id}/append",
        f"/media/upload/{media_id}/append",
        f"/media/upload/{media_id}/finalize",
    ]
    init = fake.calls[0][2]["json"]
    assert init == {"media_type": "image/jpeg", "total_bytes": 10, "media_category": "tweet_image"}
    append = fake.calls[2][2]
    assert append["data"] == {"segment_index": "1"} and append["files"]["media"][1] == b"4567"


def test_video_polls_status_until_x_is_done(client, env):
    _, fake = env
    fake.status_states = ["in_progress", "succeeded"]
    media_id, status = upload(client, b"v" * 9, media_type="video/mp4")
    assert fake.calls[0][2]["json"]["media_category"] == "tweet_video"
    assert status["state"] == "pending"
    assert client.get(f"/xyz/media/{media_id}", headers=auth()).json()["state"] == "in_progress"
    assert client.get(f"/xyz/media/{media_id}", headers=auth()).json()["state"] == "succeeded"
    # once done, the gate answers without asking X again
    calls = len(fake.calls)
    assert client.get(f"/xyz/media/{media_id}", headers=auth()).json()["state"] == "succeeded"
    assert len(fake.calls) == calls


def test_media_limits(client, env):
    gate, fake = env
    r = client.post("/xyz/media", json={"media_type": "image/gif", "total_bytes": 10}, headers=auth())
    assert r.status_code == 415
    r = client.post("/xyz/media", json={"media_type": "image/jpeg", "total_bytes": gate.MAX_IMAGE_BYTES + 1}, headers=auth())
    assert r.status_code == 413
    r = client.post("/xyz/media", json={"media_type": "video/mp4", "total_bytes": gate.MAX_VIDEO_BYTES + 1}, headers=auth())
    assert r.status_code == 413
    r = client.post("/xyz/media", json={"media_type": "image/jpeg", "total_bytes": 4}, headers=auth())
    media_id = r.json()["media_id"]
    # more bytes than declared
    r = client.put(f"/xyz/media/{media_id}/segments/0", content=b"12345", headers=auth())
    assert r.status_code == 413
    # a segment larger than X allows
    big = b"x" * (gate.MAX_SEGMENT_BYTES + 1)
    r = client.put(f"/xyz/media/{media_id}/segments/0", content=big, headers=auth())
    assert r.status_code == 413
    # finalize before all bytes arrived
    client.put(f"/xyz/media/{media_id}/segments/0", content=b"12", headers=auth())
    r = client.post(f"/xyz/media/{media_id}/finalize", headers=auth())
    assert r.status_code == 409 and r.json()["error"] == "media_incomplete"
    # resending a segment replaces it instead of counting twice
    client.put(f"/xyz/media/{media_id}/segments/0", content=b"12", headers=auth())
    client.put(f"/xyz/media/{media_id}/segments/1", content=b"34", headers=auth())
    assert client.post(f"/xyz/media/{media_id}/finalize", headers=auth()).status_code == 200
    assert not [c for c in fake.calls if c[1] == "/tweets"]


def test_media_ids_are_scoped_to_their_token(client):
    media_id, _ = upload(client, b"abc", token=DRY_TOKEN)
    r = client.get(f"/xyz/media/{media_id}", headers=auth())
    assert r.status_code == 404
    r = client.put("/xyz/media/999/segments/0", content=b"a", headers=auth())
    assert r.status_code == 404


# ── posts ────────────────────────────────────────────────────

def test_post_with_media_and_idempotent_retry(client, env):
    _, fake = env
    media_id, _ = upload(client, b"img")
    body = {"client_id": "draft-0001", "text": "hello", "media_ids": [media_id]}
    r = client.post("/xyz/posts", json=body, headers=auth())
    assert r.status_code == 200, r.text
    assert r.json() == {"id": "1977000000000000001", "url": "https://x.com/i/status/1977000000000000001", "dry_run": False}
    assert fake.tweets()[0][2]["json"] == {"text": "hello", "media": {"media_ids": [media_id]}}
    # the phone lost the answer and retries: same id, no second post
    r = client.post("/xyz/posts", json=body, headers=auth())
    assert r.json()["id"] == "1977000000000000001"
    assert len(fake.tweets()) == 1


def test_dry_run_token_never_posts(client, env):
    _, fake = env
    media_id, _ = upload(client, b"img", token=DRY_TOKEN)
    r = client.post(
        "/xyz/posts", json={"client_id": "draft-0002", "text": "test", "media_ids": [media_id]}, headers=auth(DRY_TOKEN)
    )
    assert r.status_code == 200 and r.json() == {"id": None, "url": None, "dry_run": True}
    assert fake.tweets() == []


def test_post_rules(client, env):
    _, fake = env
    ok = lambda body: client.post("/xyz/posts", json=body, headers=auth())  # noqa: E731
    assert ok({"client_id": "short", "text": "x"}).json()["error"] == "bad_client_id"
    assert ok({"client_id": "draft-0003", "text": "  "}).json()["error"] == "empty_post"
    assert ok({"client_id": "draft-0003", "text": "x" * 25001}).json()["error"] == "text_too_long"
    assert ok({"client_id": "draft-0003", "media_ids": ["123"]}).json()["error"] == "media_not_found"
    video, _ = upload(client, b"vid", media_type="video/mp4")
    image, _ = upload(client, b"img")
    assert ok({"client_id": "draft-0003", "media_ids": [video, image]}).json()["error"] == "bad_media"
    images = [upload(client, b"i%d" % n)[0] for n in range(5)]
    assert ok({"client_id": "draft-0003", "media_ids": images}).json()["error"] == "bad_media"
    # media that X has not finished is not postable yet
    fake.status_states = ["in_progress"]
    pending, _ = upload(client, b"vid2", media_type="video/mp4")
    r = ok({"client_id": "draft-0003", "media_ids": [pending]})
    assert r.status_code == 409 and r.json()["error"] == "media_not_ready"
    assert fake.tweets() == []


@pytest.mark.parametrize(
    "status,body,code,http",
    [
        (402, {"title": "Payment Required"}, "x_credits_empty", 402),
        (429, {"title": "Too Many Requests"}, "x_rate_limited", 429),
        (403, {"detail": "You are not allowed to create a Tweet with duplicate content."}, "x_duplicate", 409),
        (403, {"detail": "You are not permitted to perform this action."}, "x_forbidden", 403),
        (400, {"errors": [{"message": "Your media IDs are invalid."}]}, "x_rejected", 422),
    ],
)
def test_x_failures_map_to_codes(client, env, status, body, code, http):
    _, fake = env
    fake.tweet_status, fake.tweet_body = status, body
    r = client.post("/xyz/posts", json={"client_id": "draft-0004", "text": "hi"}, headers=auth())
    assert r.status_code == http and r.json()["error"] == code


def test_failed_post_can_be_retried(client, env):
    _, fake = env
    fake.tweet_status, fake.tweet_body = 402, {"title": "Payment Required"}
    body = {"client_id": "draft-0005", "text": "after top-up"}
    assert client.post("/xyz/posts", json=body, headers=auth()).status_code == 402
    fake.tweet_status, fake.tweet_body = 201, None
    r = client.post("/xyz/posts", json=body, headers=auth())
    assert r.status_code == 200 and r.json()["id"]
    assert len(fake.tweets()) == 2


def post_row(gate, client_id):
    with gate._db() as db:
        return dict(db.execute("SELECT * FROM posts WHERE client_id=?", (client_id,)).fetchone())


def _refused():
    return requests.ConnectionError(MaxRetryError(None, "/2/tweets", NewConnectionError(None, "Connection refused")))


def _no_dns():
    return requests.ConnectionError(MaxRetryError(None, "/2/tweets", NameResolutionError("api.x.com", None, OSError())))


# Transport failures that may come after X got the request: X may have posted.
AFTER_SEND = {
    "read_timeout": requests.ReadTimeout,
    "reset_mid_answer": lambda: requests.ConnectionError(ProtocolError("Connection aborted.", ConnectionResetError())),
    "broken_chunks": requests.exceptions.ChunkedEncodingError,
    "tls_error": requests.exceptions.SSLError,
    "bare_connection_error": requests.ConnectionError,
}
# Transport failures before the gate sent a byte: X never got the request.
BEFORE_SEND = {"connect_timeout": requests.ConnectTimeout, "refused": _refused, "no_dns": _no_dns}


@pytest.mark.parametrize("failure", AFTER_SEND.values(), ids=AFTER_SEND.keys())
def test_media_only_lost_answer_is_maybe_posted_without_another_x_call(client, env, failure):
    gate, fake = env
    media_id, _ = upload(client, b"img")
    # no text, so X cannot refuse a second copy as a duplicate
    body = {"client_id": "draft-0006", "media_ids": [media_id]}
    fake.tweet_raises = failure()
    r = client.post("/xyz/posts", json=body, headers=auth())
    assert r.status_code == 409 and r.json()["error"] == "maybe_posted"
    assert post_row(gate, "draft-0006")["state"] == "unknown"
    fake.tweet_raises = None
    calls = len(fake.calls)
    r = client.post("/xyz/posts", json=body, headers=auth())
    assert r.status_code == 409 and r.json()["error"] == "maybe_posted"
    assert len(fake.calls) == calls


def test_lost_answer_then_changed_content_keeps_the_row(client, env):
    gate, fake = env
    media_id, _ = upload(client, b"img")
    other, _ = upload(client, b"other")
    body = {"client_id": "draft-0007", "text": "first words", "media_ids": [media_id]}
    fake.tweet_raises = requests.ReadTimeout()
    assert client.post("/xyz/posts", json=body, headers=auth()).json()["error"] == "maybe_posted"
    fake.tweet_raises = None
    before = post_row(gate, "draft-0007")
    for changed in ({**body, "text": "edited", "media_ids": [other]}, {**body, "text": ""}, {**body, "media_ids": []}):
        r = client.post("/xyz/posts", json=changed, headers=auth())
        assert r.status_code == 409 and r.json()["error"] == "maybe_posted", changed
    assert post_row(gate, "draft-0007") == before
    assert len(fake.tweets()) == 1


def test_maybe_posted_is_sticky_and_counts_toward_the_daily_limit(client, env):
    gate, fake = env
    gate.MAX_POSTS_PER_DAY = 1
    body = {"client_id": "draft-0008", "text": "did it go out?"}
    fake.tweet_raises = requests.ReadTimeout()
    for _ in range(3):
        r = client.post("/xyz/posts", json=body, headers=auth())
        assert r.status_code == 409 and r.json()["error"] == "maybe_posted"
        fake.tweet_raises = None  # X answers again, but the gate does not ask
    assert post_row(gate, "draft-0008")["state"] == "unknown"
    # X may have published it, so it uses the only post of the day
    r = client.post("/xyz/posts", json={"client_id": "draft-0009", "text": "next"}, headers=auth())
    assert r.status_code == 429 and r.json()["error"] == "daily_limit"
    assert len(fake.tweets()) == 1


@pytest.mark.parametrize(
    "status,media_only",
    [(500, True), (502, True), (503, False), (504, False)],
    ids=["500_media_only", "502_proxy_media_only", "503_text", "504_proxy_text"],
)
def test_x_server_error_on_post_is_maybe_posted_without_another_x_call(client, env, status, media_only):
    gate, fake = env
    gate.MAX_POSTS_PER_DAY = 1
    if media_only:
        media_id, _ = upload(client, b"img")
        body = {"client_id": "draft-0013", "media_ids": [media_id]}
    else:
        body = {"client_id": "draft-0013", "text": "hi"}
    # X (or a proxy in front of it) may fail after X published the post
    fake.tweet_status, fake.tweet_body = status, {"title": "Server Error"}
    r = client.post("/xyz/posts", json=body, headers=auth())
    assert r.status_code == 409 and r.json()["error"] == "maybe_posted"
    assert post_row(gate, "draft-0013")["state"] == "unknown"
    fake.tweet_status, fake.tweet_body = 201, None
    calls = len(fake.calls)
    r = client.post("/xyz/posts", json=body, headers=auth())
    assert r.status_code == 409 and r.json()["error"] == "maybe_posted"
    assert len(fake.calls) == calls
    # X may have published it, so it uses the only post of the day
    r = client.post("/xyz/posts", json={"client_id": "draft-0014", "text": "next"}, headers=auth())
    assert r.status_code == 429 and r.json()["error"] == "daily_limit"
    assert len(fake.tweets()) == 1


def test_x_server_error_on_media_stays_retryable(client, env):
    _, fake = env
    x500 = FakeResponse(500, {"title": "Internal Server Error"})
    init = {"media_type": "video/mp4", "total_bytes": 4}
    fake.fail["/media/upload/initialize"] = x500
    r = client.post("/xyz/media", json=init, headers=auth())
    assert r.status_code == 502 and r.json()["error"] == "x_server_error"
    fake.fail.clear()
    media_id = client.post("/xyz/media", json=init, headers=auth()).json()["media_id"]
    fake.status_states = ["succeeded"]  # finalize answers pending, then STATUS answers succeeded
    for prefix, call in (
        (f"/media/upload/{media_id}/append", lambda: client.put(f"/xyz/media/{media_id}/segments/0", content=b"vvvv", headers=auth())),
        (f"/media/upload/{media_id}/finalize", lambda: client.post(f"/xyz/media/{media_id}/finalize", headers=auth())),
        ("/media/upload", lambda: client.get(f"/xyz/media/{media_id}", headers=auth())),
    ):
        fake.fail[prefix] = x500
        r = call()
        assert r.status_code == 502 and r.json()["error"] == "x_server_error", prefix
        fake.fail.clear()
        assert call().status_code == 200, prefix
    assert client.get(f"/xyz/media/{media_id}", headers=auth()).json()["state"] == "succeeded"
    r = client.post("/xyz/posts", json={"client_id": "draft-0015", "media_ids": [media_id]}, headers=auth())
    assert r.status_code == 200 and r.json()["id"] == "1977000000000000001"
    assert len(fake.tweets()) == 1


@pytest.mark.parametrize("failure", BEFORE_SEND.values(), ids=BEFORE_SEND.keys())
def test_failure_before_x_got_the_request_stays_retryable(client, env, failure):
    gate, fake = env
    media_id, _ = upload(client, b"img")
    body = {"client_id": "draft-0010", "media_ids": [media_id]}
    fake.tweet_raises = failure()
    r = client.post("/xyz/posts", json=body, headers=auth())
    assert r.status_code == 503 and r.json()["error"] == "x_unreachable"
    assert post_row(gate, "draft-0010")["state"] == "failed"
    fake.tweet_raises = None
    r = client.post("/xyz/posts", json=body, headers=auth())
    assert r.status_code == 200 and r.json()["id"] == "1977000000000000001"
    assert len(fake.tweets()) == 2


def test_answer_without_id_and_lost_request_are_maybe_posted(client, env):
    gate, fake = env
    fake.tweet_body = {"data": {}}
    r = client.post("/xyz/posts", json={"client_id": "draft-0011", "text": "a"}, headers=auth())
    assert r.status_code == 409 and r.json()["error"] == "maybe_posted"
    # a posting row whose request never finished (the gate restarted mid-call)
    old = gate.time.time() - gate.POSTING_STALE_S - 1
    with gate._db() as db:
        db.execute(
            "INSERT INTO posts (client_id, state, media_ids, text_sha256, created_at, updated_at, dry_run)"
            " VALUES ('draft-0012', 'posting', '[]', 'x', ?, ?, 0)",
            (old, old),
        )
    r = client.post("/xyz/posts", json={"client_id": "draft-0012", "text": "b"}, headers=auth())
    assert r.status_code == 409 and r.json()["error"] == "maybe_posted"
    assert len(fake.tweets()) == 1


def test_daily_limit(client, env):
    gate, fake = env
    gate.MAX_POSTS_PER_DAY = 2
    for n in range(2):
        fake.tweet_body = {"data": {"id": str(10 + n)}}
        assert client.post("/xyz/posts", json={"client_id": f"draft-01{n:02d}", "text": f"{n}"}, headers=auth()).status_code == 200
    r = client.post("/xyz/posts", json={"client_id": "draft-0199", "text": "3"}, headers=auth())
    assert r.status_code == 429 and r.json()["error"] == "daily_limit"
    # a retry of a post that already went out still answers
    assert client.post("/xyz/posts", json={"client_id": "draft-0100", "text": "0"}, headers=auth()).status_code == 200


def test_daily_limit_counts_posts_in_flight(client, env):
    gate, fake = env
    gate.MAX_POSTS_PER_DAY = 2
    assert client.post("/xyz/posts", json={"client_id": "draft-0200", "text": "a"}, headers=auth()).status_code == 200
    # another request is sending a post right now
    now = gate.time.time()
    with gate._db() as db:
        db.execute(
            "INSERT INTO posts (client_id, state, media_ids, text_sha256, created_at, updated_at, dry_run)"
            " VALUES ('draft-0201', 'posting', '[]', 'x', ?, ?, 0)",
            (now, now),
        )
    r = client.post("/xyz/posts", json={"client_id": "draft-0202", "text": "c"}, headers=auth())
    assert r.status_code == 429 and r.json()["error"] == "daily_limit"
    assert len(fake.tweets()) == 1


def test_daily_limit_applies_to_retries(client, env):
    gate, fake = env
    gate.MAX_POSTS_PER_DAY = 1
    fake.tweet_status, fake.tweet_body = 402, {"title": "Payment Required"}
    failed = {"client_id": "draft-0300", "text": "before top-up"}
    assert client.post("/xyz/posts", json=failed, headers=auth()).status_code == 402
    # a failed post does not count
    fake.tweet_status, fake.tweet_body = 201, None
    assert client.post("/xyz/posts", json={"client_id": "draft-0301", "text": "b"}, headers=auth()).status_code == 200
    # but its retry would call X, so the limit applies
    r = client.post("/xyz/posts", json=failed, headers=auth())
    assert r.status_code == 429 and r.json()["error"] == "daily_limit"
    assert len(fake.tweets()) == 2


def test_retry_after_media_expired_returns_the_post(client, env):
    gate, fake = env
    media_id, _ = upload(client, b"img")
    body = {"client_id": "draft-0400", "text": "hello", "media_ids": [media_id]}
    first = client.post("/xyz/posts", json=body, headers=auth())
    assert first.status_code == 200
    with gate._db() as db:
        db.execute("UPDATE media SET expires_at=0")
    r = client.post("/xyz/posts", json=body, headers=auth())
    assert r.status_code == 200 and r.json() == first.json()
    assert len(fake.tweets()) == 1
    # a new post with the expired media still fails
    r = client.post("/xyz/posts", json={**body, "client_id": "draft-0401"}, headers=auth())
    assert r.status_code == 410 and r.json()["error"] == "media_expired"


def test_reused_client_id_with_other_content_is_refused(client, env):
    _, fake = env
    media_id, _ = upload(client, b"img")
    other, _ = upload(client, b"other")
    body = {"client_id": "draft-0500", "text": "hello", "media_ids": [media_id]}
    assert client.post("/xyz/posts", json=body, headers=auth()).status_code == 200
    for changed in ({**body, "text": "hello!"}, {**body, "media_ids": [other]}, {**body, "media_ids": []}):
        r = client.post("/xyz/posts", json=changed, headers=auth())
        assert r.status_code == 409 and r.json()["error"] == "client_id_reused", changed
    assert len(fake.tweets()) == 1


def test_daily_media_limit(client, env):
    gate, fake = env
    gate.MAX_MEDIA_PER_DAY = 2
    upload(client, b"one")
    upload(client, b"two")
    r = client.post("/xyz/media", json={"media_type": "image/jpeg", "total_bytes": 3}, headers=auth())
    assert r.status_code == 429 and r.json()["error"] == "daily_media_limit"
    assert len([c for c in fake.calls if c[1] == "/media/upload/initialize"]) == 2
    # the dry-run token has its own count
    upload(client, b"dry", token=DRY_TOKEN)
