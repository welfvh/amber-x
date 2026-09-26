# Deploy — x.welf.ai

The studio (server.js + index.html) runs on the VPS behind Caddy, password-gated.
Deployed 2026-07-06, replacing the old standalone `x-composer-site` Pages project.

```
[browser] ─HTTPS→ x.welf.ai (Caddy on VPS, LE cert, DNS-only A record)
                    └→ localhost:3131  x-studio (systemd, node server.js)
                         ├─ sqlite: /root/.local/share/x-vibepoastry/data.db
                         └→ localhost:8142  x-vibepoastry FastAPI → X API
```

## Pieces

| Thing | Where |
|---|---|
| App | `/opt/x-studio/` (server.js, index.html, node_modules) |
| Service | `systemd x-studio` — template: [`vps/x-studio.service`](../vps/x-studio.service) |
| Vhost | `/etc/caddy/conf.d/x-studio.caddy` → `reverse_proxy localhost:3131` |
| DNS | `x.welf.ai` A `162.55.60.42`, **DNS-only** (grey — Caddy owns TLS) |
| Auth | session-cookie gate in server.js, active because `XVP_UI_PASSWORD` is set; password in Mac keychain `cc/x-composer` |
| Drafts DB | copied from Mac 2026-07-06 — **VPS is canonical from then on**; the Mac's local :3131 has a separate DB |

## Update the app

```bash
scp server.js index.html root@162.55.60.42:/opt/x-studio/
ssh root@162.55.60.42 systemctl restart x-studio   # index.html-only changes need no restart
```

## Gotchas

- systemd sets no `HOME` — the unit provides `Environment=HOME=/root` (server.js resolves its data dir from it). Without it: `ERR_INVALID_ARG_TYPE` at boot.
- `NO_TUNNEL=1` — on the VPS the FastAPI is already localhost:8142.
- Jam button: pbcopy/Terminal is Mac-only; on the VPS deploy the browser-clipboard fallback handles it.
- Local Mac gate stays off because `XVP_UI_PASSWORD` is unset there.
