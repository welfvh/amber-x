"""
x-vibepoastry VPS service — FastAPI app that handles X/Twitter API calls via tweepy.
Runs on VPS (162.55.60.42:8142) because api.x.com is blocked locally.
Provides: posting, scheduling, feed retrieval, stats, media upload.
"""

import os
import json
import uuid
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

from fastapi import FastAPI, UploadFile, File, HTTPException
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
UPLOAD_DIR = Path("/tmp/xvp_uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("x-vibepoastry")

app = FastAPI(title="x-vibepoastry", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8142)
