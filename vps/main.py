"""
x-vibepoastry VPS service — FastAPI app that handles X/Twitter API calls via tweepy.
Runs on VPS (162.55.60.42:8142) because api.x.com is blocked locally.
Provides: posting, scheduling, feed retrieval, stats, media upload, bookmarks.
Bookmarks require OAuth 2.0 PKCE — one-time browser auth, then auto-refreshes.
"""

import os
import json
import uuid
import sqlite3
import logging
import hashlib
import base64
import secrets
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Depends
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv

import tweepy
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

# ── config ────────────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")
DB_PATH = Path(__file__).parent / "schedule.db"
OAUTH2_TOKEN_FILE = Path(__file__).parent / "oauth2_tokens.json"
UPLOAD_DIR = Path("/tmp/xvp_uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("x-vibepoastry")

# ── auth middleware ──────────────────────────────────────────
# Require Bearer token on all endpoints. Token set via XVP_AUTH_TOKEN env var.

AUTH_EXEMPT_PATHS = {"/auth/callback", "/docs", "/openapi.json"}

def verify_token(request: Request):
    """Dependency that checks Bearer token on every request (except auth callback)."""
    if request.url.path in AUTH_EXEMPT_PATHS:
        return
    expected = os.environ.get("XVP_AUTH_TOKEN")
    if not expected:
        raise HTTPException(500, "XVP_AUTH_TOKEN not configured")
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header[7:] != expected:
        raise HTTPException(401, "Unauthorized")

app = FastAPI(title="x-vibepoastry", version="1.0.0", dependencies=[Depends(verify_token)])
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3131"], allow_methods=["*"], allow_headers=["*"])

# ── tweepy clients ────────────────────────────────────────────

def get_v2_client():
    """Tweepy v2 Client for posting tweets, reading metrics."""
    return tweepy.Client(
        bearer_token=os.environ["BEARER_TOKEN"],
        consumer_key=os.environ["API_KEY"],
        consumer_secret=os.environ["API_SECRET"],
        access_token=os.environ["ACCESS_TOKEN"],
        access_token_secret=os.environ["ACCESS_TOKEN_SECRET"],
    )

def get_v1_api():
    """Tweepy v1.1 API for media uploads."""
    auth = tweepy.OAuth1UserHandler(
        os.environ["API_KEY"],
        os.environ["API_SECRET"],
        os.environ["ACCESS_TOKEN"],
        os.environ["ACCESS_TOKEN_SECRET"],
    )
    return tweepy.API(auth)

# ── OAuth 2.0 PKCE for bookmarks ─────────────────────────────
# Bookmarks endpoint requires OAuth 2.0 User Context.
# One-time auth via /auth/init + /auth/callback, then auto-refresh.

OAUTH2_CLIENT_ID = os.environ.get("OAUTH2_CLIENT_ID", "")
OAUTH2_CLIENT_SECRET = os.environ.get("OAUTH2_CLIENT_SECRET", "")
OAUTH2_REDIRECT_URI = os.environ.get("OAUTH2_REDIRECT_URI", "http://localhost:3000/callback")
OAUTH2_SCOPES = "bookmark.read tweet.read users.read offline.access"
X_USER_ID = "212963105"

# In-memory PKCE state (only needed during auth flow)
_pkce_state: dict = {}


def _load_oauth2_tokens() -> dict | None:
    """Load stored OAuth 2.0 tokens from disk."""
    if OAUTH2_TOKEN_FILE.exists():
        try:
            return json.loads(OAUTH2_TOKEN_FILE.read_text())
        except Exception:
            return None
    return None


def _save_oauth2_tokens(tokens: dict):
    """Persist OAuth 2.0 tokens to disk."""
    tokens["saved_at"] = datetime.now(timezone.utc).isoformat()
    OAUTH2_TOKEN_FILE.write_text(json.dumps(tokens, indent=2))
    log.info("OAuth 2.0 tokens saved")


def _refresh_oauth2_token(refresh_token: str) -> dict:
    """Exchange refresh token for new access token."""
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OAUTH2_CLIENT_ID,
    }).encode()

    # Confidential client: use Basic auth with client_id:client_secret
    credentials = base64.b64encode(
        f"{OAUTH2_CLIENT_ID}:{OAUTH2_CLIENT_SECRET}".encode()
    ).decode()

    req = urllib.request.Request(
        "https://api.twitter.com/2/oauth2/token",
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {credentials}",
        },
    )
    resp = urllib.request.urlopen(req)
    tokens = json.loads(resp.read())
    _save_oauth2_tokens(tokens)
    return tokens


def get_oauth2_access_token() -> str:
    """Get a valid OAuth 2.0 access token, refreshing if needed."""
    tokens = _load_oauth2_tokens()
    if not tokens:
        raise HTTPException(401, "OAuth 2.0 not authorized. Call /auth/init first.")

    # Try using existing access token; if it fails, refresh
    return tokens["access_token"]


def _oauth2_api_request(url: str) -> dict:
    """Make an authenticated OAuth 2.0 API request with auto-refresh on 401."""
    tokens = _load_oauth2_tokens()
    if not tokens:
        raise HTTPException(401, "OAuth 2.0 not authorized. Call /auth/init first.")

    # First attempt with current access token
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tokens['access_token']}"})
    try:
        resp = urllib.request.urlopen(req)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 401 and "refresh_token" in tokens:
            # Token expired — refresh and retry
            log.info("OAuth 2.0 token expired, refreshing...")
            new_tokens = _refresh_oauth2_token(tokens["refresh_token"])
            req2 = urllib.request.Request(url, headers={"Authorization": f"Bearer {new_tokens['access_token']}"})
            resp2 = urllib.request.urlopen(req2)
            return json.loads(resp2.read())
        raise


# ── SQLite for schedule queue ─────────────────────────────────

def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schedule (
            id TEXT PRIMARY KEY,
            tweets_json TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            posted_at TEXT,
            tweet_url TEXT,
            error TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()

@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

init_db()

# ── APScheduler ──────────────────────────────────────────────

scheduler = BackgroundScheduler(
    jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{DB_PATH}")},
)
scheduler.start()

# ── models ───────────────────────────────────────────────────

class TweetPayload(BaseModel):
    text: str
    media_paths: Optional[list[str]] = None
    quote_tweet_id: Optional[str] = None  # for quote-retweet via tws sprout flow

class PostRequest(BaseModel):
    tweets: list[TweetPayload]

class ScheduleRequest(BaseModel):
    tweets: list[TweetPayload]
    scheduled_at: str  # ISO 8601 datetime

# ── helpers ──────────────────────────────────────────────────

def post_tweets(tweets: list[TweetPayload]) -> dict:
    """Post a tweet or thread via tweepy. Returns {url, id}."""
    client = get_v2_client()
    api = get_v1_api()

    previous_id = None
    first_id = None

    for tweet in tweets:
        media_ids = []
        if tweet.media_paths:
            for path in tweet.media_paths:
                if os.path.exists(path):
                    media = api.media_upload(path)
                    media_ids.append(media.media_id)
                    log.info(f"Uploaded media: {path} -> {media.media_id}")

        kwargs = {"text": tweet.text or " "}
        if media_ids:
            kwargs["media_ids"] = media_ids
        if previous_id:
            kwargs["in_reply_to_tweet_id"] = previous_id
        if tweet.quote_tweet_id:
            kwargs["quote_tweet_id"] = tweet.quote_tweet_id

        result = client.create_tweet(**kwargs)
        previous_id = result.data["id"]
        if not first_id:
            first_id = previous_id

    # Get username for URL
    me = client.get_me()
    username = me.data.username
    url = f"https://x.com/{username}/status/{first_id}"
    log.info(f"Posted: {url}")
    return {"url": url, "id": first_id}


def execute_scheduled_post(schedule_id: str, tweets_json: str):
    """Called by APScheduler to execute a scheduled post."""
    try:
        tweets_data = json.loads(tweets_json)
        tweets = [TweetPayload(**t) for t in tweets_data]
        result = post_tweets(tweets)

        with get_db() as db:
            db.execute(
                "UPDATE schedule SET status='posted', posted_at=?, tweet_url=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), result["url"], schedule_id),
            )
        log.info(f"Scheduled post {schedule_id} completed: {result['url']}")
    except Exception as e:
        log.error(f"Scheduled post {schedule_id} failed: {e}")
        with get_db() as db:
            db.execute(
                "UPDATE schedule SET status='failed', error=? WHERE id=?",
                (str(e), schedule_id),
            )

# In-flight dedup: prevent concurrent identical posts
import threading
_post_lock = threading.Lock()
_posts_in_flight: set[str] = set()

# ── routes ───────────────────────────────────────────────────

@app.get("/status")
def status():
    """Verify credentials and return username."""
    try:
        client = get_v2_client()
        me = client.get_me()
        return {"ok": True, "username": me.data.username}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    """Receive a media file, save to temp dir, return path for use in /post."""
    ext = Path(file.filename).suffix or ".bin"
    filename = f"xvp_{uuid.uuid4().hex[:8]}{ext}"
    filepath = UPLOAD_DIR / filename

    content = await file.read()
    filepath.write_bytes(content)
    log.info(f"Uploaded: {filepath} ({len(content)} bytes)")
    return {"path": str(filepath)}


@app.post("/post")
def post(req: PostRequest):
    """Post a tweet or thread immediately. Includes duplicate protection."""
    if not req.tweets:
        raise HTTPException(400, "No tweets provided")
    for t in req.tweets:
        if len(t.text or "") > 280:
            raise HTTPException(400, f"Tweet over 280 characters: {len(t.text)}")

    # Content-based dedup key
    content_key = "||".join(t.text or "" for t in req.tweets)
    with _post_lock:
        if content_key in _posts_in_flight:
            raise HTTPException(409, "Duplicate post already in flight")
        _posts_in_flight.add(content_key)

    try:
        result = post_tweets(req.tweets)
        return {"ok": True, **result}
    except Exception as e:
        log.error(f"Post failed: {e}")
        raise HTTPException(500, str(e))
    finally:
        with _post_lock:
            _posts_in_flight.discard(content_key)


@app.delete("/tweet/{tweet_id}")
def delete_tweet(tweet_id: str):
    """Delete a tweet by ID."""
    try:
        client = get_v2_client()
        client.delete_tweet(tweet_id)
        log.info(f"Deleted tweet {tweet_id}")
        return {"ok": True, "deleted": tweet_id}
    except Exception as e:
        log.error(f"Delete failed: {e}")
        raise HTTPException(500, str(e))


@app.post("/schedule")
def schedule(req: ScheduleRequest):
    """Schedule a post for a future time."""
    if not req.tweets:
        raise HTTPException(400, "No tweets provided")

    schedule_id = uuid.uuid4().hex[:12]
    scheduled_dt = datetime.fromisoformat(req.scheduled_at)

    if scheduled_dt <= datetime.now(timezone.utc):
        raise HTTPException(400, "Scheduled time must be in the future")

    tweets_json = json.dumps([t.model_dump() for t in req.tweets])

    # Store in our schedule table
    with get_db() as db:
        db.execute(
            "INSERT INTO schedule (id, tweets_json, scheduled_at) VALUES (?, ?, ?)",
            (schedule_id, tweets_json, req.scheduled_at),
        )

    # Register APScheduler job
    scheduler.add_job(
        execute_scheduled_post,
        "date",
        run_date=scheduled_dt,
        args=[schedule_id, tweets_json],
        id=f"xvp_{schedule_id}",
        replace_existing=True,
    )

    log.info(f"Scheduled post {schedule_id} for {req.scheduled_at}")
    return {"ok": True, "id": schedule_id, "scheduled_at": req.scheduled_at}


@app.get("/queue")
def queue():
    """List all scheduled posts (pending + completed + failed)."""
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM schedule ORDER BY scheduled_at DESC"
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.delete("/queue/{schedule_id}")
def cancel_scheduled(schedule_id: str):
    """Cancel a pending scheduled post."""
    with get_db() as db:
        row = db.execute("SELECT status FROM schedule WHERE id=?", (schedule_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Not found")
        if row["status"] != "pending":
            raise HTTPException(400, f"Cannot cancel: status is {row['status']}")

        db.execute("UPDATE schedule SET status='cancelled' WHERE id=?", (schedule_id,))

    # Remove APScheduler job
    try:
        scheduler.remove_job(f"xvp_{schedule_id}")
    except Exception:
        pass

    return {"ok": True}


@app.get("/tweets")
def tweets(count: int = 20):
    """Get recent own tweets with public_metrics."""
    try:
        client = get_v2_client()
        me = client.get_me()
        user_id = me.data.id

        response = client.get_users_tweets(
            user_id,
            max_results=min(count, 100),
            tweet_fields=["created_at", "public_metrics", "conversation_id"],
            exclude=["retweets"],
        )

        if not response.data:
            return {"tweets": [], "username": me.data.username}

        tweets_list = []
        for tweet in response.data:
            pm = tweet.public_metrics or {}
            tweets_list.append({
                "id": tweet.id,
                "text": tweet.text,
                "created_at": tweet.created_at.isoformat() if tweet.created_at else None,
                "metrics": {
                    "likes": pm.get("like_count", 0),
                    "retweets": pm.get("retweet_count", 0),
                    "replies": pm.get("reply_count", 0),
                    "impressions": pm.get("impression_count", 0),
                    "bookmarks": pm.get("bookmark_count", 0),
                },
            })

        return {"tweets": tweets_list, "username": me.data.username}
    except Exception as e:
        log.error(f"Tweets fetch failed: {e}")
        raise HTTPException(500, str(e))


@app.get("/timeline")
def timeline(
    max: int = 100,
    since_id: Optional[str] = None,
    pagination_token: Optional[str] = None,
):
    """Get the authenticated user's home timeline (reverse-chronological).
    Used by the tws-feed ingester. Returns same shape as /tweets plus an
    authors map and a next pagination_token when more pages exist."""
    try:
        client = get_v2_client()
        me = client.get_me()

        response = client.get_home_timeline(
            user_auth=True,
            max_results=min(max, 100),
            since_id=since_id,
            pagination_token=pagination_token,
            tweet_fields=[
                "created_at", "public_metrics", "conversation_id",
                "author_id", "referenced_tweets", "lang",
            ],
            expansions=["author_id"],
            user_fields=["username", "name", "profile_image_url"],
        )

        # Build author lookup from expansions
        authors = {}
        if response.includes and "users" in response.includes:
            for u in response.includes["users"]:
                authors[str(u.id)] = {
                    "id": str(u.id),
                    "username": u.username,
                    "name": u.name,
                    "profile_image_url": u.profile_image_url,
                }

        items = []
        for tw in (response.data or []):
            pm = tw.public_metrics or {}
            author = authors.get(str(tw.author_id), {})
            items.append({
                "id": str(tw.id),
                "text": tw.text,
                "created_at": tw.created_at.isoformat() if tw.created_at else None,
                "author_id": str(tw.author_id) if tw.author_id else None,
                "author_username": author.get("username"),
                "author_name": author.get("name"),
                "conversation_id": str(tw.conversation_id) if tw.conversation_id else None,
                "lang": tw.lang,
                "metrics": {
                    "likes": pm.get("like_count", 0),
                    "retweets": pm.get("retweet_count", 0),
                    "replies": pm.get("reply_count", 0),
                    "impressions": pm.get("impression_count", 0),
                    "bookmarks": pm.get("bookmark_count", 0),
                },
                "referenced": [
                    {"type": r.type, "id": str(r.id)} for r in (tw.referenced_tweets or [])
                ],
            })

        meta = response.meta or {}
        return {
            "items": items,
            "authors": authors,
            "next_token": meta.get("next_token"),
            "newest_id": meta.get("newest_id"),
            "result_count": meta.get("result_count", len(items)),
            "username": me.data.username,
        }
    except Exception as e:
        log.error(f"Timeline fetch failed: {e}")
        raise HTTPException(500, str(e))


@app.get("/activity")
def activity(count: int = 20):
    """Get recent mentions and replies."""
    try:
        client = get_v2_client()
        me = client.get_me()
        user_id = me.data.id

        response = client.get_users_mentions(
            user_id,
            max_results=min(count, 100),
            tweet_fields=["created_at", "public_metrics", "author_id", "in_reply_to_user_id"],
            expansions=["author_id"],
        )

        if not response.data:
            return {"mentions": [], "username": me.data.username}

        # Build author lookup from includes
        authors = {}
        if response.includes and "users" in response.includes:
            for user in response.includes["users"]:
                authors[user.id] = user.username

        mentions = []
        for tweet in response.data:
            pm = tweet.public_metrics or {}
            mentions.append({
                "id": tweet.id,
                "text": tweet.text,
                "author_id": tweet.author_id,
                "author_username": authors.get(tweet.author_id, "unknown"),
                "created_at": tweet.created_at.isoformat() if tweet.created_at else None,
                "metrics": {
                    "likes": pm.get("like_count", 0),
                    "retweets": pm.get("retweet_count", 0),
                    "replies": pm.get("reply_count", 0),
                },
            })

        return {"mentions": mentions, "username": me.data.username}
    except Exception as e:
        log.error(f"Activity fetch failed: {e}")
        raise HTTPException(500, str(e))


@app.get("/search")
def search(q: str, count: int = 25):
    """Recent tweet search (last 7 days) for the keyword-curated topical feed."""
    try:
        client = get_v2_client()
        response = client.search_recent_tweets(
            q,
            max_results=max(10, min(count, 100)),
            tweet_fields=["created_at", "public_metrics", "author_id"],
            expansions=["author_id"],
            user_fields=["username", "name", "profile_image_url"],
        )

        if not response.data:
            return {"tweets": [], "query": q}

        authors = {}
        if response.includes and "users" in response.includes:
            for user in response.includes["users"]:
                authors[user.id] = {
                    "username": user.username,
                    "name": user.name,
                    "profile_image_url": getattr(user, "profile_image_url", None),
                }

        tweets_list = []
        for tweet in response.data:
            pm = tweet.public_metrics or {}
            author = authors.get(tweet.author_id, {})
            tweets_list.append({
                "id": tweet.id,
                "text": tweet.text,
                "author_id": tweet.author_id,
                "author_username": author.get("username", "unknown"),
                "author_name": author.get("name", ""),
                "author_image": author.get("profile_image_url"),
                "created_at": tweet.created_at.isoformat() if tweet.created_at else None,
                "metrics": {
                    "likes": pm.get("like_count", 0),
                    "retweets": pm.get("retweet_count", 0),
                    "replies": pm.get("reply_count", 0),
                    "impressions": pm.get("impression_count", 0),
                },
            })

        return {"tweets": tweets_list, "query": q}
    except Exception as e:
        log.error(f"Search failed: {e}")
        raise HTTPException(500, str(e))


@app.get("/stats")
def stats():
    """Profile stats + aggregate tweet metrics."""
    try:
        client = get_v2_client()
        me = client.get_me(user_fields=["public_metrics", "description", "profile_image_url"])
        pm = me.data.public_metrics or {}

        # Fetch last 20 tweets for aggregate metrics
        user_id = me.data.id
        response = client.get_users_tweets(
            user_id,
            max_results=20,
            tweet_fields=["public_metrics"],
            exclude=["retweets"],
        )

        total_likes = 0
        total_retweets = 0
        total_replies = 0
        total_impressions = 0
        tweet_count = 0

        if response.data:
            for tweet in response.data:
                tpm = tweet.public_metrics or {}
                total_likes += tpm.get("like_count", 0)
                total_retweets += tpm.get("retweet_count", 0)
                total_replies += tpm.get("reply_count", 0)
                total_impressions += tpm.get("impression_count", 0)
                tweet_count += 1

        return {
            "username": me.data.username,
            "name": me.data.name,
            "description": me.data.description,
            "profile_image_url": getattr(me.data, "profile_image_url", None),
            "followers": pm.get("followers_count", 0),
            "following": pm.get("following_count", 0),
            "tweet_count": pm.get("tweet_count", 0),
            "recent_metrics": {
                "tweets_sampled": tweet_count,
                "total_likes": total_likes,
                "total_retweets": total_retweets,
                "total_replies": total_replies,
                "total_impressions": total_impressions,
            },
        }
    except Exception as e:
        log.error(f"Stats fetch failed: {e}")
        raise HTTPException(500, str(e))


# ── OAuth 2.0 PKCE auth routes ───────────────────────────────
# These are exempt from the XVP_AUTH_TOKEN check (the callback comes from X).

auth_router = FastAPI()


@app.get("/auth/init")
def auth_init():
    """Start OAuth 2.0 PKCE flow. Returns auth URL to open in browser."""
    if not OAUTH2_CLIENT_ID:
        raise HTTPException(500, "OAUTH2_CLIENT_ID not configured in .env")

    code_verifier = secrets.token_urlsafe(64)[:128]
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)

    # Store PKCE state for callback
    _pkce_state["code_verifier"] = code_verifier
    _pkce_state["state"] = state

    auth_url = (
        f"https://twitter.com/i/oauth2/authorize?"
        f"response_type=code&client_id={OAUTH2_CLIENT_ID}"
        f"&redirect_uri={urllib.parse.quote(OAUTH2_REDIRECT_URI)}"
        f"&scope={urllib.parse.quote(OAUTH2_SCOPES)}"
        f"&state={state}"
        f"&code_challenge={code_challenge}"
        f"&code_challenge_method=S256"
    )

    return {"auth_url": auth_url, "message": "Open this URL in a browser to authorize."}


@app.get("/auth/callback", dependencies=[])
def auth_callback(code: str = "", state: str = "", error: str = ""):
    """OAuth 2.0 callback — exchanges auth code for tokens."""
    if error:
        raise HTTPException(400, f"Auth error: {error}")
    if not code:
        raise HTTPException(400, "No authorization code received")
    if state != _pkce_state.get("state"):
        raise HTTPException(400, "State mismatch — possible CSRF")

    code_verifier = _pkce_state.get("code_verifier")
    if not code_verifier:
        raise HTTPException(400, "No PKCE state found — call /auth/init first")

    # Exchange code for tokens using confidential client (Basic auth)
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": OAUTH2_REDIRECT_URI,
        "code_verifier": code_verifier,
        "client_id": OAUTH2_CLIENT_ID,
    }).encode()

    credentials = base64.b64encode(
        f"{OAUTH2_CLIENT_ID}:{OAUTH2_CLIENT_SECRET}".encode()
    ).decode()

    req = urllib.request.Request(
        "https://api.twitter.com/2/oauth2/token",
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {credentials}",
        },
    )

    try:
        resp = urllib.request.urlopen(req)
        tokens = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        log.error(f"Token exchange failed: {e.code} {body}")
        raise HTTPException(500, f"Token exchange failed: {body}")

    _save_oauth2_tokens(tokens)
    _pkce_state.clear()

    return {"ok": True, "message": "OAuth 2.0 authorized! Bookmarks endpoint is now available."}


@app.get("/auth/status")
def auth_status():
    """Check if OAuth 2.0 tokens are available."""
    tokens = _load_oauth2_tokens()
    if tokens:
        return {
            "authorized": True,
            "saved_at": tokens.get("saved_at"),
            "has_refresh": "refresh_token" in tokens,
        }
    return {"authorized": False}


# ── bookmarks ─────────────────────────────────────────────────

@app.get("/bookmarks")
def bookmarks(count: int = 25):
    """Fetch user's bookmarks via OAuth 2.0. Auto-refreshes tokens."""
    params = urllib.parse.urlencode({
        "max_results": min(count, 100),
        "tweet.fields": "created_at,text,author_id",
        "user.fields": "username,name",
        "expansions": "author_id",
    })
    url = f"https://api.twitter.com/2/users/{X_USER_ID}/bookmarks?{params}"

    try:
        data = _oauth2_api_request(url)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        log.error(f"Bookmarks failed: {e.code} {body}")
        raise HTTPException(e.code, f"X API error: {body}")

    # Format response with author info
    users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
    tweets = []
    for tw in data.get("data", []):
        author = users.get(tw.get("author_id", ""), {})
        tweets.append({
            "id": tw["id"],
            "text": tw["text"],
            "author": f"@{author.get('username', 'unknown')}",
            "author_name": author.get("name", ""),
            "created_at": tw.get("created_at"),
        })

    return {"bookmarks": tweets, "count": len(tweets)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8142)
