# x-mcp

MCP wrapper around `x-vibepoastry` so consumer MCP clients (e.g. Poke / interaction.co) can post to X on Welf's behalf.

## Architecture

```
Poke client → HTTPS → Caddy (x.mcp.welf.ai) → :8143 x-mcp (FastMCP)
                                                  ↓ Bearer XVP_AUTH_TOKEN
                                              :8142 x-vibepoastry (FastAPI)
                                                  ↓ tweepy
                                              X API
```

## Tools exposed

- `post_tweet(text)` — single tweet
- `post_thread(tweets)` — list of strings, chained as replies
- `schedule_post(text, scheduled_at)` — ISO 8601 datetime
- `get_recent_tweets(count=20)`
- `get_stats()`
- `delete_tweet(tweet_id)`
- `list_scheduled()` / `cancel_scheduled(schedule_id)`

## Auth

`Authorization: Bearer <X_MCP_AUTH_TOKEN>` on every request (any path).
Stored in `/opt/x-mcp/.env` on the VPS, also stashed in macOS Keychain locally under `cc/x-mcp` for reference.

## Deploy

See `../vps/` neighbour service for the pattern. Files:

- `main.py` — MCP server
- `requirements.txt` — deps
- `x-mcp.service` — systemd unit installed at `/etc/systemd/system/`
- `.env` (NOT committed) — `XVP_AUTH_TOKEN`, `X_MCP_AUTH_TOKEN`
