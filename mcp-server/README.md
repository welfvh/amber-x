# x-mcp

MCP wrapper around `x-vibepoastry` so consumer MCP clients (Poke / interaction.co, Claude desktop, etc.) can post to X on Welf's behalf without holding raw X API credentials.

## Architecture

```
  Poke (interaction.co)
        │  HTTPS, Authorization: Bearer X_MCP_AUTH_TOKEN
        ▼
  Caddy ────────────────────────────────────  x.mcp.welf.ai (Let's Encrypt)
        │ reverse_proxy localhost:8143
        ▼
  x-mcp (FastMCP, Streamable HTTP at /mcp) ── /opt/x-mcp on VPS, port 8143
        │  Bearer XVP_AUTH_TOKEN
        ▼
  x-vibepoastry (FastAPI) ─────────────────── /opt/x-vibepoastry on VPS, port 8142
        │  OAuth 1.0a (consumer + access tokens)
        ▼
  X API (api.x.com)
```

Why two layers: x-vibepoastry already exists and is shared with the local `amber-x` composer over an SSH tunnel. Rather than duplicate the X integration, x-mcp is a thin MCP-protocol facade that proxies REST calls to it. Future clients (other consumer AIs) drop in on the same MCP endpoint with a different token.

## Tools exposed

All return JSON. Bearer auth is required on every request.

| Tool | Args | Proxies to | Notes |
|---|---|---|---|
| `post_tweet` | `text: str` | `POST /post` | Single tweet, 280-char cap enforced upstream |
| `post_thread` | `tweets: list[str]` | `POST /post` | Threaded as replies |
| `schedule_post` | `text: str`, `scheduled_at: ISO8601` | `POST /schedule` | APScheduler queue |
| `get_recent_tweets` | `count: int = 20` | `GET /tweets` | With engagement metrics |
| `get_stats` | — | `GET /stats` | Followers, following, aggregate metrics |
| `delete_tweet` | `tweet_id: str` | `DELETE /tweet/{id}` | Undo a post |
| `list_scheduled` | — | `GET /queue` | Pending + posted + failed + cancelled |
| `cancel_scheduled` | `schedule_id: str` | `DELETE /queue/{id}` | Only `pending` can be cancelled |

Media uploads aren't currently exposed via MCP — Poke can't send binaries through the protocol cleanly. Add a tool if a client needs it.

## Endpoint

- **URL:** `https://x.mcp.welf.ai/mcp`
- **Transport:** MCP Streamable HTTP
- **Auth:** `Authorization: Bearer <X_MCP_AUTH_TOKEN>` on every request, including the initial handshake. 401 on any path without it.

The token lives in three places:

1. `/opt/x-mcp/.env` on the VPS (chmod 600) — what the server compares against
2. macOS Keychain on Welf's Mac, service `cc/x-mcp`, account `auth_token` — for reference and to paste into clients
3. Whichever client(s) you've handed it to (currently Poke)

## Repo layout

```
~/dev/amber-x/mcp-server/
  main.py            FastMCP server + Bearer middleware
  requirements.txt   fastmcp, httpx, python-dotenv, uvicorn
  x-mcp.service      systemd unit (deployed to /etc/systemd/system/)
  README.md          this file
  .gitignore
  .env               (NOT committed) XVP_URL, XVP_AUTH_TOKEN, X_MCP_AUTH_TOKEN, PORT
```

On the VPS:

```
/opt/x-mcp/
  main.py / requirements.txt / x-mcp.service   rsynced from above
  venv/                                        python3 -m venv (fastmcp 3.x)
  .env                                         chmod 600, secrets
```

## Deploy from scratch

Assumes `welf.ai` is already in Cloudflare under the token at Keychain `cc/cloudflare`, and the VPS has Caddy + `x-vibepoastry` running.

```bash
# 1. Push source to VPS
ssh root@162.55.60.42 'mkdir -p /opt/x-mcp'
rsync -av --exclude='.env' --exclude='venv' --exclude='__pycache__' \
  ~/dev/amber-x/mcp-server/ root@162.55.60.42:/opt/x-mcp/

# 2. Write .env on the VPS (token shared with x-vibepoastry + a fresh MCP token)
XVP_TOKEN=$(ssh root@162.55.60.42 'grep XVP_AUTH_TOKEN /opt/x-vibepoastry/.env | cut -d= -f2')
MCP_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
ssh root@162.55.60.42 "cat > /opt/x-mcp/.env <<EOF
XVP_URL=http://localhost:8142
XVP_AUTH_TOKEN=${XVP_TOKEN}
X_MCP_AUTH_TOKEN=${MCP_TOKEN}
PORT=8143
EOF
chmod 600 /opt/x-mcp/.env"

# Stash MCP token locally for reference
security add-generic-password -U -s "cc/x-mcp" -a "auth_token" -w "$MCP_TOKEN"

# 3. Install Python deps
ssh root@162.55.60.42 'cd /opt/x-mcp && python3 -m venv venv && ./venv/bin/pip install -r requirements.txt'

# 4. Install + start systemd unit
ssh root@162.55.60.42 'cp /opt/x-mcp/x-mcp.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now x-mcp'

# 5. Cloudflare DNS — A record, DNS-only (NOT proxied; Caddy needs direct TLS for ACME)
CF_TOKEN=$(security find-generic-password -s "cc/cloudflare" -w)
ZONE=$(curl -s "https://api.cloudflare.com/client/v4/zones?name=welf.ai" \
  -H "Authorization: Bearer $CF_TOKEN" | jq -r '.result[0].id')
curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$ZONE/dns_records" \
  -H "Authorization: Bearer $CF_TOKEN" -H "Content-Type: application/json" \
  -d '{"type":"A","name":"x.mcp","content":"162.55.60.42","ttl":1,"proxied":false}'

# 6. Caddy reverse proxy (drop a snippet, reload)
ssh root@162.55.60.42 "cat > /etc/caddy/conf.d/x-mcp.caddy <<'EOF'
x.mcp.welf.ai {
	reverse_proxy localhost:8143
}
EOF
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && systemctl reload caddy"

# 7. Smoke test
curl -s -o /dev/null -w "%{http_code}\n" https://x.mcp.welf.ai/mcp    # → 401
curl -s -X POST https://x.mcp.welf.ai/mcp \
  -H "Authorization: Bearer $MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}'
# → event: message / data: {... serverInfo: x-mcp ...}
```

## Operations

### Push a code change

```bash
rsync -av --exclude='.env' --exclude='venv' --exclude='__pycache__' \
  ~/dev/amber-x/mcp-server/ root@162.55.60.42:/opt/x-mcp/
ssh root@162.55.60.42 'systemctl restart x-mcp'
```

If `requirements.txt` changed: also `./venv/bin/pip install -r requirements.txt` on the VPS before restart.

### Logs

```bash
ssh root@162.55.60.42 'journalctl -u x-mcp -n 50 --no-pager'
ssh root@162.55.60.42 'journalctl -u x-mcp -f'              # follow
```

### Rotate the MCP token

```bash
NEW=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
ssh root@162.55.60.42 "sed -i 's/^X_MCP_AUTH_TOKEN=.*/X_MCP_AUTH_TOKEN=${NEW}/' /opt/x-mcp/.env && systemctl restart x-mcp"
security add-generic-password -U -s "cc/x-mcp" -a "auth_token" -w "$NEW"
# Then update Poke (and any other client) with the new token.
```

Rotating the upstream `XVP_AUTH_TOKEN` is handled in `x-vibepoastry`; if it changes there, mirror it into `/opt/x-mcp/.env` and restart x-mcp.

## Troubleshooting

### Cert not provisioning

Symptom: `curl https://x.mcp.welf.ai/mcp` returns connection error or wrong cert.

Check Caddy: `ssh root@162.55.60.42 'journalctl -u caddy -n 30 --no-pager | grep -i x.mcp'`

Common cause: DNS hadn't propagated when Caddy first tried; it backs off to a 10-min retry and may escalate to Let's Encrypt staging after repeated failures. Fix: confirm `dig +short x.mcp.welf.ai @1.1.1.1` returns `162.55.60.42`, then `systemctl restart caddy` to force an immediate retry against prod LE.

### 401 from authed client

Confirm the client is sending `Authorization: Bearer <token>` (not a custom header, not `?api_key=`). Compare the token to `/opt/x-mcp/.env`. Poke's "API Key" field maps to the Bearer header.

### 402 Payment Required

Comes from the X API itself, not our stack. The X developer account is out of credits for the endpoint that was called (reads especially — `/tweets`, `/bookmarks`). Top up at https://developer.x.com/en/portal/products. Posting is on a different quota and may still work when reads don't.

### Tool returns 5xx

Likely an x-vibepoastry / X-API issue, not the MCP layer. Check both:

```bash
ssh root@162.55.60.42 'journalctl -u x-mcp -n 20; echo ---; journalctl -u x-vibepoastry -n 20'
```

## Adding a new client (e.g. another consumer AI)

1. Generate a separate token if you want per-client revocation, OR reuse the existing one for simplicity. Current setup is a single shared token — if you need per-client scopes/audit, replace the middleware with a token→client map.
2. Paste `https://x.mcp.welf.ai/mcp` + token into the client's MCP integration screen.
3. The client will hit `initialize` then `tools/list`, then call tools as the user prompts.
