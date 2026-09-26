"""
x-mcp — MCP wrapper around x-vibepoastry REST service.

Exposes X/Twitter posting tools over MCP (Streamable HTTP, /mcp path) for
consumer AI clients like Poke (interaction.co). Proxies every call to the
existing x-vibepoastry FastAPI service on localhost:8142.

Auth: Bearer token in Authorization header, value from X_MCP_AUTH_TOKEN env.
"""

import os
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

load_dotenv(Path(__file__).parent / ".env")

XVP_URL = os.environ.get("XVP_URL", "http://localhost:8142")
XVP_TOKEN = os.environ["XVP_AUTH_TOKEN"]
MCP_TOKEN = os.environ["X_MCP_AUTH_TOKEN"]

mcp = FastMCP("x-mcp")

xvp = httpx.Client(
    base_url=XVP_URL,
    headers={"Authorization": f"Bearer {XVP_TOKEN}"},
    timeout=60.0,
)


@mcp.tool()
def post_tweet(text: str) -> dict:
    """Post a single tweet to X. Returns the posted tweet's URL and id."""
    r = xvp.post("/post", json={"tweets": [{"text": text}]})
    r.raise_for_status()
    return r.json()


@mcp.tool()
def post_thread(tweets: list[str]) -> dict:
    """Post a thread of tweets to X. Each item becomes one tweet, chained as replies."""
    payload = {"tweets": [{"text": t} for t in tweets]}
    r = xvp.post("/post", json=payload)
    r.raise_for_status()
    return r.json()


@mcp.tool()
def schedule_post(text: str, scheduled_at: str) -> dict:
    """Schedule a tweet for a future time. scheduled_at must be ISO 8601 (e.g. 2026-05-20T15:00:00+00:00)."""
    payload = {"tweets": [{"text": text}], "scheduled_at": scheduled_at}
    r = xvp.post("/schedule", json=payload)
    r.raise_for_status()
    return r.json()


@mcp.tool()
def get_recent_tweets(count: int = 20) -> dict:
    """Fetch the user's recent own tweets with engagement metrics."""
    r = xvp.get("/tweets", params={"count": count})
    r.raise_for_status()
    return r.json()


@mcp.tool()
def get_stats() -> dict:
    """Profile stats (followers, following, tweet count) plus aggregate metrics on recent tweets."""
    r = xvp.get("/stats")
    r.raise_for_status()
    return r.json()


@mcp.tool()
def delete_tweet(tweet_id: str) -> dict:
    """Delete a tweet by id. Useful for undoing a mistaken post."""
    r = xvp.delete(f"/tweet/{tweet_id}")
    r.raise_for_status()
    return r.json()


@mcp.tool()
def list_scheduled() -> dict:
    """List all scheduled posts (pending, posted, failed, cancelled)."""
    r = xvp.get("/queue")
    r.raise_for_status()
    return r.json()


@mcp.tool()
def cancel_scheduled(schedule_id: str) -> dict:
    """Cancel a pending scheduled post by its id."""
    r = xvp.delete(f"/queue/{schedule_id}")
    r.raise_for_status()
    return r.json()


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != MCP_TOKEN:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


app = mcp.http_app(path="/mcp")
app.add_middleware(BearerAuthMiddleware)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8143")))
