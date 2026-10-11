"""
Post-only gate for xyz, Welf's Light Phone III tool (github.com/welfvh/xyz-lp).

main.py mounts this app at /xyz. It has its own bearer tokens, and the rest of
the service does not accept them. It does two things on X and nothing else:
upload media (chunked: initialize, append, finalize, then status until X has
processed a video) and create a post. It cannot read, delete, like or DM.

Tokens: the service holds only SHA-256 hashes of them.
  XYZ_POST_TOKEN_SHA256    the phone's token: posts for real.
  XYZ_DRYRUN_TOKEN_SHA256  test token: uploads media to X for real, but
                           /posts never creates a post (it answers with
                           dry_run: true and no post id).

The phone retries a post until it gets a post id, so /posts is idempotent per
client_id: a retry of a post that went out returns the same id. A client_id
names one post: a retry with other text or media gets client_id_reused.

If the request reached X but the answer got lost, X may have published the
post. A 5xx answer to the post is the same: X (or a proxy in front of it) may
fail after X published the post. So is a 2xx answer that the gate cannot read
(not JSON, or no post id in it). That outcome is sticky: the gate never sends
this client_id to X again, and every try answers maybe_posted, whatever its
text or media. A person checks the profile and, if the post is missing, posts
it again as a new draft (a new client_id).

Errors answer {"error": <code>, "detail": <text>}. The phone retries only
x_unreachable, x_server_error, x_rate_limited and in_flight; every other code
needs a person (bad token, X credits at zero, X rejected the post). Only the
media endpoints answer x_server_error: a repeated upload publishes nothing.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from requests_oauthlib import OAuth1
from starlette.concurrency import run_in_threadpool
from urllib3.exceptions import NewConnectionError

log = logging.getLogger("x-vibepoastry.xyz")

X_API = "https://api.x.com/2"
X_TIMEOUT_S = (10, 120)  # connect, read

# X's media rules (developer.x.com, media upload v2): JPEG/PNG/WebP images up
# to 5 MB, MP4 video up to 512 MB, segments up to 5 MB, at most 1000 of them.
MEDIA_TYPES = {
    "image/jpeg": "tweet_image",
    "image/png": "tweet_image",
    "image/webp": "tweet_image",
    "video/mp4": "tweet_video",
}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_VIDEO_BYTES = 512 * 1024 * 1024
MAX_SEGMENT_BYTES = 5 * 1024 * 1024
CHUNK_BYTES = 4 * 1024 * 1024  # what the phone sends per segment
MAX_SEGMENTS = 1000
MAX_TEXT_CHARS = 25_000  # @_welf is Premium (long posts)
MAX_POSTS_PER_DAY = 50  # a stolen phone token cannot flood the account
# Media initialized per UTC day, per token. The dry-run token uploads to X for
# real too, so it has its own count with the same limit.
MAX_MEDIA_PER_DAY = 200
POSTING_STALE_S = 180  # a "posting" row older than this lost its request: X may have posted it

CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
MEDIA_ID_RE = re.compile(r"^[0-9]{1,30}$")
POST_ID_RE = re.compile(r"^[0-9]{1,30}$")


class GateError(Exception):
    def __init__(self, status: int, code: str, detail: str = "", extra: Optional[dict] = None):
        super().__init__(f"{status} {code}: {detail}")
        self.status = status
        self.code = code
        self.detail = detail
        self.extra = extra or {}


# ── storage ──────────────────────────────────────────────────

def _data_dir() -> Path:
    return Path(os.environ.get("XVP_DATA_DIR", "/var/lib/x-vibepoastry"))


_db_lock = threading.Lock()  # serializes read-modify-write of media rows
_init_lock = threading.Lock()
_db_ready: set[Path] = set()


@contextmanager
def _db():
    # Starlette never runs the startup events of a mounted app, so the tables
    # are created on first use.
    path = _data_dir() / "xyz.db"
    if path not in _db_ready:
        with _init_lock:
            if path not in _db_ready:
                _create_tables(path)
                _db_ready.add(path)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _create_tables(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with sqlite3.connect(path, timeout=30) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS media (
                media_id TEXT PRIMARY KEY,
                media_type TEXT NOT NULL,
                total_bytes INTEGER NOT NULL,
                segments TEXT NOT NULL DEFAULT '{}',
                state TEXT NOT NULL DEFAULT 'uploading',
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                dry_run INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS posts (
                client_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                tweet_id TEXT,
                error TEXT,
                media_ids TEXT NOT NULL,
                text_sha256 TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                dry_run INTEGER NOT NULL
            );
            """
        )


# ── auth ─────────────────────────────────────────────────────

def _token_hashes() -> tuple[str, str]:
    return (
        os.environ.get("XYZ_POST_TOKEN_SHA256", "").strip().lower(),
        os.environ.get("XYZ_DRYRUN_TOKEN_SHA256", "").strip().lower(),
    )


def require_token(request: Request) -> bool:
    """Returns True for the dry-run token, False for the real one."""
    post_sha, dry_sha = _token_hashes()
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer ") or not (post_sha or dry_sha):
        raise HTTPException(401, {"error": "unauthorized", "detail": "missing or unknown token"})
    digest = hashlib.sha256(header[7:].strip().encode()).hexdigest()
    if post_sha and hmac.compare_digest(digest, post_sha):
        return False
    if dry_sha and hmac.compare_digest(digest, dry_sha):
        return True
    raise HTTPException(401, {"error": "unauthorized", "detail": "missing or unknown token"})


# ── X calls ──────────────────────────────────────────────────

def _oauth() -> OAuth1:
    return OAuth1(
        os.environ["API_KEY"],
        os.environ["API_SECRET"],
        os.environ["ACCESS_TOKEN"],
        os.environ["ACCESS_TOKEN_SECRET"],
    )


def x_request(method: str, path: str, **kwargs) -> requests.Response:
    """One call to the X API v2 as @_welf (OAuth 1.0a user context).
    Tests replace this function."""
    return requests.request(method, X_API + path, auth=_oauth(), timeout=X_TIMEOUT_S, **kwargs)


def _x_message(resp: requests.Response) -> str:
    # An error body of an odd shape must not hide X's status code, so any
    # failure to read it falls back to the raw text.
    try:
        body = resp.json()
        errors = body.get("errors") or []
        parts = [body.get("detail") or body.get("title") or ""]
        parts += [e.get("message") or e.get("detail") or "" for e in errors if isinstance(e, dict)]
        return "; ".join(p for p in parts if p)[:300] or f"HTTP {resp.status_code}"
    except Exception:
        return resp.text[:300] or f"HTTP {resp.status_code}"


def _never_reached_x(exc: Optional[BaseException]) -> bool:
    """True only if the request certainly never reached X.

    Two transport failures happen before the gate sends a byte: the TCP
    connect timed out (requests.ConnectTimeout), or the connection never
    opened (urllib3 NewConnectionError: refused, unreachable, DNS failure).
    Every other failure may come after X got the request: a read timeout, a
    reset or a TLS error mid-answer, a broken chunked answer. When in doubt,
    the request reached X."""
    if isinstance(exc, requests.ConnectTimeout):
        return True
    reason = getattr(exc.args[0], "reason", None) if isinstance(exc, requests.ConnectionError) and exc.args else None
    return isinstance(reason, NewConnectionError)


def _x_call(method: str, path: str, **kwargs) -> requests.Response:
    """X call with X's failures mapped to gate errors. Returns only 2xx.
    A transport failure raises x_unreachable from the requests exception."""
    try:
        resp = x_request(method, path, **kwargs)
    except requests.RequestException as e:
        raise GateError(503, "x_unreachable", type(e).__name__) from e
    if 200 <= resp.status_code < 300:
        return resp
    msg = _x_message(resp)
    status = resp.status_code
    log.warning("xyz: X %s %s -> %s %s", method, path, status, msg)
    if status == 402:
        raise GateError(402, "x_credits_empty", "X API credits are at zero: top up in the X developer portal")
    if status == 429:
        reset = resp.headers.get("x-rate-limit-reset")
        raise GateError(429, "x_rate_limited", msg, {"retry_at": int(reset) if reset and reset.isdigit() else None})
    if status >= 500:
        raise GateError(502, "x_server_error", msg)
    if status in (401, 403) and "duplicate" in msg.lower():
        raise GateError(409, "x_duplicate", msg)
    if status == 401:
        raise GateError(502, "x_auth_failed", msg)
    if status == 403:
        raise GateError(403, "x_forbidden", msg)
    if status == 404:
        raise GateError(404, "x_media_not_found", msg)
    raise GateError(422, "x_rejected", msg)


@contextmanager
def _media_answer(what: str):
    """Wraps the reading of a 2xx answer to a media call. If the body is not
    JSON or has an odd shape, the call answers x_server_error. A repeated
    media call publishes nothing, so the phone may retry."""
    try:
        yield
    except Exception as e:
        log.warning("xyz: X answered %s with a body the gate cannot read: %r", what, e)
        raise GateError(502, "x_server_error", f"X answered {what} with a body the gate cannot read") from e


def _processing(data: dict) -> dict:
    """The phone's view of X's processing_info. Raises if it has an odd shape."""
    info = (data or {}).get("processing_info")
    if not info:
        return {"state": "succeeded", "check_after_secs": 0, "progress": 100}
    state = info.get("state", "pending")
    if not isinstance(state, str):
        raise TypeError("processing state is not text")
    out = {
        "state": state,
        "check_after_secs": int(info.get("check_after_secs") or 0),
        "progress": int(info.get("progress_percent") or 0),
    }
    if out["state"] == "failed":
        err = info.get("error") or {}
        out["error"] = err.get("message") or err.get("name") or "X could not process this video"
    return out


# ── models ───────────────────────────────────────────────────

class MediaInit(BaseModel):
    media_type: str
    total_bytes: int


class PostIn(BaseModel):
    client_id: str
    text: str = ""
    media_ids: list[str] = []


# ── app ──────────────────────────────────────────────────────

xyz_app = FastAPI(
    title="xyz post gate",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    dependencies=[Depends(require_token)],
)


@xyz_app.exception_handler(GateError)
def _gate_error(_request: Request, exc: GateError):
    return JSONResponse(status_code=exc.status, content={"error": exc.code, "detail": exc.detail, **exc.extra})


@xyz_app.exception_handler(HTTPException)
def _http_error(_request: Request, exc: HTTPException):
    content = exc.detail if isinstance(exc.detail, dict) else {"error": "bad_request", "detail": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content=content)


def _media_row(media_id: str, dry_run: bool) -> sqlite3.Row:
    if not MEDIA_ID_RE.match(media_id):
        raise GateError(404, "media_not_found", "unknown media id")
    with _db() as db:
        row = db.execute("SELECT * FROM media WHERE media_id=?", (media_id,)).fetchone()
    if row is None or bool(row["dry_run"]) != dry_run:
        raise GateError(404, "media_not_found", "unknown media id")
    if row["expires_at"] < time.time():
        raise GateError(410, "media_expired", "upload again")
    return row


@xyz_app.get("/ping")
def ping(dry_run: bool = Depends(require_token)):
    """Checks the token. Makes no X call, so it costs no credits."""
    return {"ok": True, "dry_run": dry_run, "chunk_bytes": CHUNK_BYTES}


def _utc_day_start() -> float:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


_media_init_lock = threading.Lock()


@xyz_app.post("/media")
def media_init(body: MediaInit, dry_run: bool = Depends(require_token)):
    category = MEDIA_TYPES.get(body.media_type)
    if category is None:
        raise GateError(415, "media_type_not_allowed", f"allowed: {', '.join(sorted(MEDIA_TYPES))}")
    limit = MAX_VIDEO_BYTES if category == "tweet_video" else MAX_IMAGE_BYTES
    if not 0 < body.total_bytes <= limit:
        raise GateError(413, "media_too_large", f"{body.media_type} must be 1..{limit} bytes")
    # The lock covers the count, the X call and the insert, so parallel
    # requests cannot pass the daily limit together.
    with _media_init_lock:
        with _db() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM media WHERE dry_run=? AND created_at>=?", (int(dry_run), _utc_day_start())
            ).fetchone()[0]
        if count >= MAX_MEDIA_PER_DAY:
            raise GateError(429, "daily_media_limit", f"at most {MAX_MEDIA_PER_DAY} uploads per day")
        resp = _x_call(
            "POST",
            "/media/upload/initialize",
            json={"media_type": body.media_type, "total_bytes": body.total_bytes, "media_category": category},
        )
        with _media_answer("initialize"):
            data = resp.json().get("data") or {}
            media_id = str(data.get("id") or "")
            ttl = min(int(data.get("expires_after_secs") or 86400), 86400) - 3600
        if not MEDIA_ID_RE.match(media_id):
            raise GateError(502, "x_server_error", "X returned no media id")
        now = time.time()
        expires_at = now + max(ttl, 3600)
        with _db_lock, _db() as db:
            db.execute(
                "INSERT OR REPLACE INTO media (media_id, media_type, total_bytes, created_at, expires_at, dry_run)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (media_id, body.media_type, body.total_bytes, now, expires_at, int(dry_run)),
            )
    log.info("xyz: media %s initialized (%s, %d bytes, dry_run=%s)", media_id, body.media_type, body.total_bytes, dry_run)
    return {"media_id": media_id, "expires_at": int(expires_at), "chunk_bytes": CHUNK_BYTES}


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        raise GateError(413, "segment_too_large", f"segments are at most {limit} bytes")
    chunks, size = [], 0
    async for part in request.stream():
        size += len(part)
        if size > limit:
            raise GateError(413, "segment_too_large", f"segments are at most {limit} bytes")
        chunks.append(part)
    return b"".join(chunks)


@xyz_app.put("/media/{media_id}/segments/{index}")
async def media_append(media_id: str, index: int, request: Request, dry_run: bool = Depends(require_token)):
    row = _media_row(media_id, dry_run)
    if row["state"] != "uploading":
        raise GateError(409, "media_finalized", "this upload is already finalized")
    if not 0 <= index < MAX_SEGMENTS:
        raise GateError(422, "bad_segment", f"segment index must be 0..{MAX_SEGMENTS - 1}")
    data = await _read_body(request, MAX_SEGMENT_BYTES)
    if not data:
        raise GateError(422, "bad_segment", "empty segment")
    segments = {int(k): v for k, v in json.loads(row["segments"]).items()}
    segments[index] = len(data)
    if sum(segments.values()) > row["total_bytes"]:
        raise GateError(413, "media_too_large", "more bytes than declared")
    # The X call blocks: keep it off the event loop.
    await run_in_threadpool(
        _x_call,
        "POST",
        f"/media/upload/{media_id}/append",
        data={"segment_index": str(index)},
        files={"media": ("segment", data, "application/octet-stream")},
    )
    with _db_lock, _db() as db:
        current = json.loads(db.execute("SELECT segments FROM media WHERE media_id=?", (media_id,)).fetchone()[0])
        current[str(index)] = len(data)
        db.execute("UPDATE media SET segments=? WHERE media_id=?", (json.dumps(current), media_id))
    return {"ok": True, "received_bytes": sum(current.values())}


@xyz_app.post("/media/{media_id}/finalize")
def media_finalize(media_id: str, dry_run: bool = Depends(require_token)):
    row = _media_row(media_id, dry_run)
    received = sum(json.loads(row["segments"]).values())
    if received != row["total_bytes"]:
        raise GateError(409, "media_incomplete", f"received {received} of {row['total_bytes']} bytes")
    if row["state"] == "uploading":
        resp = _x_call("POST", f"/media/upload/{media_id}/finalize")
        with _media_answer("finalize"):
            status = _processing(resp.json().get("data") or {})
        with _db_lock, _db() as db:
            db.execute("UPDATE media SET state=? WHERE media_id=?", (status["state"], media_id))
        log.info("xyz: media %s finalized: %s", media_id, status["state"])
        return status
    return media_status(media_id, dry_run)


@xyz_app.get("/media/{media_id}")
def media_status(media_id: str, dry_run: bool = Depends(require_token)):
    row = _media_row(media_id, dry_run)
    if row["state"] == "uploading":
        raise GateError(409, "media_not_finalized", "finalize first")
    if row["state"] in ("succeeded", "failed"):
        return {"state": row["state"], "check_after_secs": 0, "progress": 100}
    resp = _x_call("GET", "/media/upload", params={"command": "STATUS", "media_id": media_id})
    with _media_answer("status"):
        status = _processing(resp.json().get("data") or {})
    if status["state"] in ("succeeded", "failed"):
        with _db_lock, _db() as db:
            db.execute("UPDATE media SET state=? WHERE media_id=?", (status["state"], media_id))
    return status


def _today_count(db: sqlite3.Connection, client_id: str) -> int:
    """Real posts that count against today's limit: every row of this UTC day
    that X may have published (all states but failed), and every request in
    flight. The row of client_id does not count against itself."""
    return db.execute(
        "SELECT COUNT(*) FROM posts WHERE dry_run=0 AND client_id!=?"
        " AND ((state!='failed' AND updated_at>=?) OR (state='posting' AND updated_at>=?))",
        (client_id, _utc_day_start(), time.time() - POSTING_STALE_S),
    ).fetchone()[0]


def _validate_post(body: PostIn, dry_run: bool) -> None:
    if not body.text.strip() and not body.media_ids:
        raise GateError(422, "empty_post", "a post needs text or media")
    if len(body.text) > MAX_TEXT_CHARS:
        raise GateError(422, "text_too_long", f"at most {MAX_TEXT_CHARS} characters")
    if len(body.media_ids) > 4 or len(set(body.media_ids)) != len(body.media_ids):
        raise GateError(422, "bad_media", "at most 4 distinct media")
    types = []
    for media_id in body.media_ids:
        row = _media_row(media_id, dry_run)
        if row["state"] != "succeeded":
            raise GateError(409, "media_not_ready", f"media {media_id} is {row['state']}")
        types.append(row["media_type"])
    if any(t.startswith("video/") for t in types) and len(types) > 1:
        raise GateError(422, "bad_media", "a video must be the only media")


_post_lock = threading.Lock()


@xyz_app.post("/posts")
def create_post(body: PostIn, dry_run: bool = Depends(require_token)):
    if not CLIENT_ID_RE.match(body.client_id):
        raise GateError(422, "bad_client_id", "client_id must be 8-64 letters, digits or dashes")
    text_sha = hashlib.sha256(body.text.encode()).hexdigest()
    with _post_lock:
        now = time.time()
        with _db() as db:
            row = db.execute("SELECT * FROM posts WHERE client_id=?", (body.client_id,)).fetchone()
        if row is not None and bool(row["dry_run"]) != dry_run:
            raise GateError(409, "bad_client_id", "client_id belongs to another token")
        # A retry of a post that went out gets its id before the media checks:
        # the media may have expired since.
        if row is not None and row["state"] == "posted":
            if row["text_sha256"] != text_sha or json.loads(row["media_ids"]) != body.media_ids:
                raise GateError(409, "client_id_reused", "this client_id already posted other text or media")
            return _post_out(row["tweet_id"], dry_run)
        if row is not None and row["state"] == "posting" and now - row["updated_at"] < POSTING_STALE_S:
            raise GateError(409, "in_flight", "this post is being sent")
        # An uncertain outcome is sticky. Every state but failed means X may
        # have this post (unknown, or a posting row that lost its request), so
        # the gate never calls X again for it and leaves the row as it is.
        if row is not None and row["state"] != "failed":
            raise _maybe_posted()
        _validate_post(body, dry_run)
        with _db() as db:
            # The limit applies to every attempt that calls X, also to retries.
            if not dry_run and _today_count(db, body.client_id) >= MAX_POSTS_PER_DAY:
                raise GateError(429, "daily_limit", f"at most {MAX_POSTS_PER_DAY} posts per day")
            db.execute(
                "INSERT INTO posts (client_id, state, media_ids, text_sha256, created_at, updated_at, dry_run)"
                " VALUES (?, 'posting', ?, ?, ?, ?, ?)"
                " ON CONFLICT(client_id) DO UPDATE SET state='posting', error=NULL, media_ids=excluded.media_ids,"
                " text_sha256=excluded.text_sha256, updated_at=excluded.updated_at",
                (body.client_id, json.dumps(body.media_ids), text_sha, now, now, int(dry_run)),
            )

    def finish(state: str, tweet_id: Optional[str] = None, error: Optional[str] = None):
        with _db() as db:
            db.execute(
                "UPDATE posts SET state=?, tweet_id=?, error=?, updated_at=? WHERE client_id=?",
                (state, tweet_id, error, time.time(), body.client_id),
            )

    if dry_run:
        finish("posted", "dry-run")
        log.info("xyz: dry-run post %s accepted (%d media), nothing published", body.client_id, len(body.media_ids))
        return _post_out("dry-run", True)

    payload: dict = {}
    if body.text.strip():
        payload["text"] = body.text
    if body.media_ids:
        payload["media"] = {"media_ids": body.media_ids}
    try:
        resp = _x_call("POST", "/tweets", json=payload)
    except GateError as e:
        # A lost answer or a 5xx answer: X may have published the post.
        if e.code == "x_server_error" or (e.code == "x_unreachable" and not _never_reached_x(e.__cause__)):
            finish("unknown", error=f"{e.code}: {e.detail}")
            raise _maybe_posted() from e
        # X refused the post (4xx), or the request never reached X: the row
        # fails, and a retry may call X again.
        finish("failed", error=f"{e.code}: {e.detail}")
        raise
    except Exception as e:
        # A failure the gate does not know: it cannot prove that X never got
        # the request.
        log.exception("xyz: post %s: unexpected failure in the X call", body.client_id)
        finish("unknown", error=f"unexpected: {type(e).__name__}")
        raise _maybe_posted() from e
    # X answered 2xx, so X may have published the post, whatever the body says.
    tweet_id = _post_id(resp)
    if tweet_id is None:
        log.warning("xyz: post %s: X answered %s without a readable post id", body.client_id, resp.status_code)
        finish("unknown", error=f"X answered {resp.status_code} without a readable post id")
        raise _maybe_posted()
    finish("posted", tweet_id)
    log.info("xyz: posted %s for client %s", tweet_id, body.client_id)
    return _post_out(tweet_id, False)


def _post_id(resp: requests.Response) -> Optional[str]:
    """The post id in a 2xx answer to POST /2/tweets, or None if the gate
    cannot read one: the body is not JSON, has no data.id, or the id is not
    a string of digits."""
    try:
        post_id = resp.json()["data"]["id"]
    except Exception:
        return None
    return post_id if isinstance(post_id, str) and POST_ID_RE.match(post_id) else None


def _maybe_posted() -> GateError:
    return GateError(409, "maybe_posted", "X may have this post: its answer got lost or failed. Check the profile")


def _post_out(tweet_id: str, dry_run: bool) -> dict:
    if dry_run:
        return {"id": None, "url": None, "dry_run": True}
    return {"id": tweet_id, "url": f"https://x.com/i/status/{tweet_id}", "dry_run": False}
