#!/usr/bin/env node
/**
 * x-vibepoastry local server — draft CRUD, asset management, VPS proxy.
 * Port: 3131
 *
 * Architecture:
 * - SQLite (better-sqlite3) for drafts, media refs, assets, tweet cache
 * - SSH tunnel to VPS (162.55.60.42:8142) for all X API calls
 * - Static file serving for UI (index.html) and uploaded media
 * - Jam integration for CC session handoff
 */

import http from 'http';
import { readFileSync, writeFileSync, mkdirSync, existsSync, unlinkSync, createReadStream, statSync } from 'fs';
import { resolve, dirname, extname, join, basename } from 'path';
import { fileURLToPath } from 'url';
import { execSync, spawn } from 'child_process';
import { randomUUID, timingSafeEqual } from 'crypto';
import Database from 'better-sqlite3';

const PORT = parseInt(process.env.PORT || '3131', 10);
const __dir = dirname(fileURLToPath(import.meta.url));
const VPS_HOST = 'root@162.55.60.42';
const VPS_PORT = 8142;
const DATA_DIR = resolve(process.env.HOME, '.local/share/x-vibepoastry');
const MEDIA_DIR = resolve(DATA_DIR, 'media');
const ASSETS_DIR = resolve(DATA_DIR, 'media/assets');
const JAM_DIR = resolve(DATA_DIR, 'jam');
const DB_PATH = resolve(DATA_DIR, 'data.db');

// Auth token for VPS x-vibepoastry service — env first (VPS deploy), then Mac Keychain
let VPS_AUTH_TOKEN = process.env.XVP_AUTH_TOKEN || '';
if (!VPS_AUTH_TOKEN) {
  try {
    VPS_AUTH_TOKEN = execSync('security find-generic-password -s "cc/x-vibepoastry" -a "auth_token" -w', { encoding: 'utf8' }).trim();
  } catch {
    console.error('WARNING: cc/x-vibepoastry auth token not found in Keychain or XVP_AUTH_TOKEN env');
  }
}

// ── UI auth gate (public deploys only) ──────────────────────
// Active when XVP_UI_PASSWORD is set (x.welf.ai). Local Mac runs skip it.
const UI_PASSWORD = process.env.XVP_UI_PASSWORD || '';
const authFails = new Map(); // ip → { count, resetAt }
const AUTH_MAX_FAILS = 10, AUTH_WINDOW_MS = 15 * 60 * 1000;

// ── ensure directories ──────────────────────────────────────

[DATA_DIR, MEDIA_DIR, ASSETS_DIR, JAM_DIR].forEach(d => mkdirSync(d, { recursive: true }));

// ── SSH tunnel ──────────────────────────────────────────────

let tunnelProcess = null;

function startTunnel() {
  if (tunnelProcess) return;
  tunnelProcess = spawn('ssh', [
    '-N', '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
    '-o', 'ExitOnForwardFailure=yes', '-o', 'StrictHostKeyChecking=accept-new',
    '-L', `${VPS_PORT}:localhost:${VPS_PORT}`,
    VPS_HOST,
  ], { stdio: 'ignore' });

  tunnelProcess.on('exit', (code) => {
    console.log(`SSH tunnel exited (code ${code}), restarting in 5s…`);
    tunnelProcess = null;
    setTimeout(startTunnel, 5000);
  });

  console.log(`SSH tunnel → localhost:${VPS_PORT} (PID ${tunnelProcess.pid})`);
}

// ── SQLite setup ────────────────────────────────────────────

const db = new Database(DB_PATH);
db.pragma('journal_mode = WAL');
db.pragma('foreign_keys = ON');

db.exec(`
  CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    thread_json TEXT NOT NULL,
    status TEXT DEFAULT 'draft',
    scheduled_at TEXT,
    posted_at TEXT,
    tweet_url TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
  );

  CREATE TABLE IF NOT EXISTS media (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL,
    tweet_idx INTEGER DEFAULT 0,
    file_path TEXT NOT NULL,
    mime_type TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (draft_id) REFERENCES drafts(id) ON DELETE CASCADE
  );

  CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY,
    type TEXT DEFAULT 'text',
    title TEXT,
    content TEXT,
    file_path TEXT,
    tags TEXT DEFAULT '[]',
    created_at TEXT DEFAULT (datetime('now'))
  );

  CREATE TABLE IF NOT EXISTS tweets_cache (
    tweet_id TEXT PRIMARY KEY,
    text TEXT,
    metrics_json TEXT,
    created_at TEXT,
    fetched_at TEXT DEFAULT (datetime('now'))
  );

  CREATE TABLE IF NOT EXISTS feed_cache (
    feed_key TEXT PRIMARY KEY,
    json TEXT NOT NULL,
    fetched_at TEXT DEFAULT (datetime('now'))
  );

  CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
  );

  CREATE TABLE IF NOT EXISTS reply_status (
    tweet_id TEXT PRIMARY KEY,
    done_at TEXT DEFAULT (datetime('now'))
  );
`);

// Migration: drafts.kind ('draft' | 'suggestion') — AI-suggested drafts live
// in their own tab and never mix into the composer's draft list.
const draftCols = db.prepare('PRAGMA table_info(drafts)').all().map(c => c.name);
if (!draftCols.includes('kind')) {
  db.exec("ALTER TABLE drafts ADD COLUMN kind TEXT DEFAULT 'draft'");
}

// Seed default feed settings (only if unset)
const seedSetting = db.prepare('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)');
seedSetting.run('feed_keywords', JSON.stringify([
  'e-ink screen', 'paper-like display', 'daylight computer',
  'screen flicker PWM', 'blue light sleep', 'amber computing',
]));
seedSetting.run('arena_channels', JSON.stringify(['amber-kcuxgs11jy4']));

// Prepared statements for performance
const stmts = {
  listDrafts: db.prepare("SELECT * FROM drafts WHERE kind = ? ORDER BY updated_at DESC"),
  getDraft: db.prepare('SELECT * FROM drafts WHERE id = ?'),
  insertDraft: db.prepare("INSERT INTO drafts (id, thread_json, kind) VALUES (?, ?, ?)"),
  updateDraft: db.prepare('UPDATE drafts SET thread_json = ?, updated_at = datetime(\'now\') WHERE id = ?'),
  updateDraftStatus: db.prepare('UPDATE drafts SET status = ?, updated_at = datetime(\'now\') WHERE id = ?'),
  updateDraftPosted: db.prepare('UPDATE drafts SET status = \'posted\', posted_at = ?, tweet_url = ?, updated_at = datetime(\'now\') WHERE id = ?'),
  updateDraftScheduled: db.prepare('UPDATE drafts SET status = \'scheduled\', scheduled_at = ?, updated_at = datetime(\'now\') WHERE id = ?'),
  deleteDraft: db.prepare('DELETE FROM drafts WHERE id = ?'),

  listMedia: db.prepare('SELECT * FROM media WHERE draft_id = ? ORDER BY tweet_idx, created_at'),
  insertMedia: db.prepare('INSERT INTO media (id, draft_id, tweet_idx, file_path, mime_type) VALUES (?, ?, ?, ?, ?)'),
  deleteMedia: db.prepare('DELETE FROM media WHERE id = ? AND draft_id = ?'),
  deleteMediaByDraft: db.prepare('DELETE FROM media WHERE draft_id = ?'),

  listAssets: db.prepare('SELECT * FROM assets ORDER BY created_at DESC'),
  getAsset: db.prepare('SELECT * FROM assets WHERE id = ?'),
  insertAsset: db.prepare('INSERT INTO assets (id, type, title, content, file_path, tags) VALUES (?, ?, ?, ?, ?, ?)'),
  deleteAsset: db.prepare('DELETE FROM assets WHERE id = ?'),

  getSetting: db.prepare('SELECT value FROM settings WHERE key = ?'),
  setSetting: db.prepare(`INSERT INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now'))
    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')`),

  getFeedCache: db.prepare('SELECT json, fetched_at FROM feed_cache WHERE feed_key = ?'),
  setFeedCache: db.prepare(`INSERT INTO feed_cache (feed_key, json, fetched_at) VALUES (?, ?, datetime('now'))
    ON CONFLICT(feed_key) DO UPDATE SET json = excluded.json, fetched_at = datetime('now')`),

  markReplyDone: db.prepare('INSERT OR IGNORE INTO reply_status (tweet_id) VALUES (?)'),
  unmarkReplyDone: db.prepare('DELETE FROM reply_status WHERE tweet_id = ?'),
  listRepliesDone: db.prepare('SELECT tweet_id FROM reply_status'),

  upsertTweetCache: db.prepare(`INSERT INTO tweets_cache (tweet_id, text, metrics_json, created_at, fetched_at)
    VALUES (?, ?, ?, ?, datetime('now'))
    ON CONFLICT(tweet_id) DO UPDATE SET metrics_json = excluded.metrics_json, fetched_at = datetime('now')`),
};

// ── feed cache helper ───────────────────────────────────────
// TTL-cached fetch: protects pay-as-you-go X API credits. `refresh=1` forces.
async function cachedFeed(key, ttlMinutes, refresh, fetcher) {
  const row = stmts.getFeedCache.get(key);
  if (row && !refresh) {
    const ageMin = (Date.now() - new Date(row.fetched_at + 'Z').getTime()) / 60000;
    if (ageMin < ttlMinutes) {
      return { ...JSON.parse(row.json), cached_at: row.fetched_at, from_cache: true };
    }
  }
  try {
    const data = await fetcher();
    stmts.setFeedCache.run(key, JSON.stringify(data));
    return { ...data, from_cache: false };
  } catch (e) {
    // Fetcher failed — serve stale cache if we have it rather than nothing
    if (row) return { ...JSON.parse(row.json), cached_at: row.fetched_at, from_cache: true, stale: true, error: e.message };
    throw e;
  }
}

// Engagement score for "sorted by how well they worked"
function engagementScore(m) {
  return (m.likes || 0) + 2 * (m.retweets || 0) + (m.replies || 0) + 2 * (m.bookmarks || 0);
}

// ── VPS proxy helpers ───────────────────────────────────────

async function vpsRequest(method, path, body = null) {
  const url = `http://localhost:${VPS_PORT}${path}`;
  const opts = { method, headers: { 'Authorization': `Bearer ${VPS_AUTH_TOKEN}` } };
  if (body) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  const data = await res.json();
  if (!res.ok && !data.ok) {
    throw new Error(data.detail || data.error || `VPS ${res.status}`);
  }
  return data;
}

async function uploadMediaToVPS(filePath) {
  const url = `http://localhost:${VPS_PORT}/upload`;
  const fileData = readFileSync(filePath);
  const filename = basename(filePath);

  // Use FormData via fetch
  const boundary = `----xvp${Date.now()}`;
  const ext = extname(filename).toLowerCase();
  const mimeMap = { '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp' };
  const mime = mimeMap[ext] || 'application/octet-stream';

  const header = `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="${filename}"\r\nContent-Type: ${mime}\r\n\r\n`;
  const footer = `\r\n--${boundary}--\r\n`;

  const body = Buffer.concat([Buffer.from(header), fileData, Buffer.from(footer)]);

  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': `multipart/form-data; boundary=${boundary}`, 'Authorization': `Bearer ${VPS_AUTH_TOKEN}` },
    body,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || 'Upload failed');
  return data.path;
}

// ── HTTP helpers ────────────────────────────────────────────

function cors(res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
}

function send(res, code, body) {
  cors(res);
  res.writeHead(code, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(body));
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', c => chunks.push(c));
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString();
      if (!raw) return resolve({});
      try { resolve(JSON.parse(raw)); }
      catch { reject(new Error('Bad JSON')); }
    });
    req.on('error', reject);
  });
}

function readMultipart(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', c => chunks.push(c));
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

function parseRoute(url) {
  const [path, qs] = url.split('?');
  const params = Object.fromEntries(new URLSearchParams(qs || ''));
  return { path, params };
}

const MIME_TYPES = {
  '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.json': 'application/json', '.png': 'image/png', '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp',
  '.svg': 'image/svg+xml', '.ico': 'image/x-icon',
};

// In-flight dedup for raw (non-draft) posts
const postInFlight = new Set();

// ── UI auth gate helpers ────────────────────────────────────

db.exec(`CREATE TABLE IF NOT EXISTS ui_sessions (
  token TEXT PRIMARY KEY,
  created_at TEXT DEFAULT (datetime('now'))
)`);
const sessStmts = {
  get: db.prepare('SELECT token FROM ui_sessions WHERE token = ?'),
  put: db.prepare('INSERT INTO ui_sessions (token) VALUES (?)'),
  del: db.prepare('DELETE FROM ui_sessions WHERE token = ?'),
};

function clientIp(req) {
  return (req.headers['cf-connecting-ip'] || req.headers['x-forwarded-for'] || req.socket.remoteAddress || 'unknown')
    .toString().split(',')[0].trim();
}

function isAuthed(req) {
  const cookie = req.headers.cookie || '';
  const m = cookie.match(/(?:^|;\s*)xvp_session=([a-f0-9-]+)/);
  return !!(m && sessStmts.get.get(m[1]));
}

function passwordMatches(submitted) {
  const a = Buffer.from(submitted || '');
  const b = Buffer.from(UI_PASSWORD);
  return a.length === b.length && timingSafeEqual(a, b);
}

const LOGIN_HTML = `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>x.welf.ai — sign in</title><style>
*{margin:0;padding:0;box-sizing:border-box}html,body{height:100%}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;background:#f7f9f9;color:#0f1419;display:grid;place-items:center;padding:1rem}
@media (prefers-color-scheme:dark){body{background:#000;color:#e7e9ea}.card{background:#000!important;border:1px solid #2f3336}}
.card{background:#fff;border-radius:16px;padding:2rem;width:100%;max-width:360px;box-shadow:0 1px 3px rgba(0,0,0,.04),0 8px 32px rgba(0,0,0,.04)}
h1{font-size:1.25rem;font-weight:700;margin-bottom:.25rem}.sub{font-size:.875rem;color:#536471;margin-bottom:1.5rem}
input{width:100%;padding:.75rem 1rem;font-size:1rem;border:1px solid #cfd9de;border-radius:8px;margin-bottom:1rem;background:transparent;color:inherit}
input:focus{outline:2px solid #1d9bf0;border-color:transparent}
button{width:100%;padding:.75rem;font-size:1rem;font-weight:700;color:#fff;background:#0f1419;border:none;border-radius:9999px;cursor:pointer}
@media (prefers-color-scheme:dark){button{background:#eff3f4;color:#0f1419}}
.err{color:#f4212e;font-size:.875rem;margin-bottom:1rem;display:none}</style></head><body>
<div class="card"><h1>𝕏 x.welf.ai</h1><div class="sub">Content studio — sign in</div>
<div class="err" id="err">Wrong password</div>
<form id="f"><input type="password" id="pw" placeholder="Password" autofocus autocomplete="current-password">
<button type="submit">Sign in</button></form></div>
<script>
document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const r = await fetch('/login', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('pw').value})});
  if (r.ok) location.href = '/'; else document.getElementById('err').style.display = 'block';
});
</script></body></html>`;

// ── routes ──────────────────────────────────────────────────

const server = http.createServer(async (req, res) => {
  if (req.method === 'OPTIONS') { cors(res); res.writeHead(204); res.end(); return; }

  const { path, params } = parseRoute(req.url);

  try {
    // ── auth gate (only when XVP_UI_PASSWORD is set) ──────
    if (UI_PASSWORD) {
      if (path === '/login' && req.method === 'GET') {
        res.writeHead(200, { 'Content-Type': 'text/html' });
        res.end(LOGIN_HTML);
        return;
      }
      if (path === '/login' && req.method === 'POST') {
        const ip = clientIp(req);
        const fails = authFails.get(ip);
        if (fails && fails.count >= AUTH_MAX_FAILS && Date.now() < fails.resetAt) {
          return send(res, 429, { error: 'too_many_attempts' });
        }
        const body = await readBody(req);
        await new Promise(r => setTimeout(r, 250)); // slow brute force
        if (!passwordMatches(body.password)) {
          const f = authFails.get(ip) || { count: 0, resetAt: Date.now() + AUTH_WINDOW_MS };
          f.count += 1; if (Date.now() > f.resetAt) { f.count = 1; f.resetAt = Date.now() + AUTH_WINDOW_MS; }
          authFails.set(ip, f);
          return send(res, 401, { error: 'invalid_credentials' });
        }
        authFails.delete(ip);
        const token = randomUUID().replace(/-/g, '') + randomUUID().replace(/-/g, '');
        sessStmts.put.run(token);
        cors(res);
        res.writeHead(200, {
          'Content-Type': 'application/json',
          'Set-Cookie': `xvp_session=${token}; HttpOnly; Secure; SameSite=Lax; Max-Age=2592000; Path=/`,
        });
        res.end(JSON.stringify({ ok: true }));
        return;
      }
      if (path === '/logout' && req.method === 'POST') {
        const m = (req.headers.cookie || '').match(/(?:^|;\s*)xvp_session=([a-f0-9-]+)/);
        if (m) sessStmts.del.run(m[1]);
        cors(res);
        res.writeHead(200, { 'Content-Type': 'application/json', 'Set-Cookie': 'xvp_session=; Max-Age=0; Path=/' });
        res.end(JSON.stringify({ ok: true }));
        return;
      }
      if (!isAuthed(req)) {
        if (req.method === 'GET' && (path === '/' || path === '/index.html')) {
          res.writeHead(302, { Location: '/login' });
          res.end();
          return;
        }
        return send(res, 401, { error: 'unauthorized' });
      }
    }

    // ── static files ──────────────────────────────────────
    if (req.method === 'GET' && (path === '/' || path === '/index.html')) {
      cors(res);
      res.writeHead(200, { 'Content-Type': 'text/html' });
      res.end(readFileSync(resolve(__dir, 'index.html')));
      return;
    }

    // Serve local media files
    if (req.method === 'GET' && path.startsWith('/media/')) {
      const relPath = path.slice(7); // remove /media/
      const filePath = resolve(MEDIA_DIR, relPath);
      if (!filePath.startsWith(MEDIA_DIR) || !existsSync(filePath)) {
        return send(res, 404, { error: 'Not found' });
      }
      const ext = extname(filePath).toLowerCase();
      cors(res);
      res.writeHead(200, { 'Content-Type': MIME_TYPES[ext] || 'application/octet-stream' });
      createReadStream(filePath).pipe(res);
      return;
    }

    // ── draft CRUD ────────────────────────────────────────

    // GET /drafts — list drafts (?kind=suggestion for the AI tab; default: real drafts)
    if (req.method === 'GET' && path === '/drafts') {
      const kind = params.kind === 'suggestion' ? 'suggestion' : 'draft';
      const drafts = stmts.listDrafts.all(kind).map(d => ({
        ...d, thread: JSON.parse(d.thread_json),
      }));
      return send(res, 200, { drafts });
    }

    // POST /drafts — create draft (kind: 'draft' | 'suggestion')
    if (req.method === 'POST' && path === '/drafts') {
      const body = await readBody(req);
      const id = randomUUID().slice(0, 12);
      const thread = body.thread || body.tweets || [{ text: body.text || '' }];
      const kind = body.kind === 'suggestion' ? 'suggestion' : 'draft';
      stmts.insertDraft.run(id, JSON.stringify(thread), kind);
      return send(res, 201, { ok: true, id, thread, kind });
    }

    // POST /drafts/:id/promote — suggestion → real draft (Welf pulls it into the composer)
    const promoteMatch = path.match(/^\/drafts\/([a-z0-9-]+)\/promote$/);
    if (req.method === 'POST' && promoteMatch) {
      const draft = stmts.getDraft.get(promoteMatch[1]);
      if (!draft) return send(res, 404, { error: 'Not found' });
      db.prepare("UPDATE drafts SET kind = 'draft', updated_at = datetime('now') WHERE id = ?").run(draft.id);
      return send(res, 200, { ok: true, id: draft.id });
    }

    // GET /drafts/:id
    const draftGetMatch = path.match(/^\/drafts\/([a-z0-9-]+)$/);
    if (req.method === 'GET' && draftGetMatch) {
      const draft = stmts.getDraft.get(draftGetMatch[1]);
      if (!draft) return send(res, 404, { error: 'Not found' });
      const media = stmts.listMedia.all(draft.id);
      return send(res, 200, { ...draft, thread: JSON.parse(draft.thread_json), media });
    }

    // PUT /drafts/:id
    const draftPutMatch = path.match(/^\/drafts\/([a-z0-9-]+)$/);
    if (req.method === 'PUT' && draftPutMatch) {
      const draft = stmts.getDraft.get(draftPutMatch[1]);
      if (!draft) return send(res, 404, { error: 'Not found' });
      const body = await readBody(req);
      const thread = body.thread || body.tweets || JSON.parse(draft.thread_json);
      stmts.updateDraft.run(JSON.stringify(thread), draft.id);
      return send(res, 200, { ok: true, id: draft.id, thread });
    }

    // DELETE /drafts/:id
    const draftDelMatch = path.match(/^\/drafts\/([a-z0-9-]+)$/);
    if (req.method === 'DELETE' && draftDelMatch) {
      const draft = stmts.getDraft.get(draftDelMatch[1]);
      if (!draft) return send(res, 404, { error: 'Not found' });
      stmts.deleteMediaByDraft.run(draft.id);
      stmts.deleteDraft.run(draft.id);
      return send(res, 200, { ok: true });
    }

    // ── media ─────────────────────────────────────────────

    // POST /drafts/:id/media — attach media to a draft
    const mediaPostMatch = path.match(/^\/drafts\/([a-z0-9-]+)\/media$/);
    if (req.method === 'POST' && mediaPostMatch) {
      const draft = stmts.getDraft.get(mediaPostMatch[1]);
      if (!draft) return send(res, 404, { error: 'Draft not found' });

      const buf = await readMultipart(req);
      const ct = req.headers['content-type'] || '';
      const boundaryMatch = ct.match(/boundary=(.+)/);
      if (!boundaryMatch) return send(res, 400, { error: 'Missing boundary' });

      // Simple multipart parser for single file
      const boundary = boundaryMatch[1];
      const parts = buf.toString('binary').split(`--${boundary}`);
      for (const part of parts) {
        const headerEnd = part.indexOf('\r\n\r\n');
        if (headerEnd === -1) continue;
        const headers = part.slice(0, headerEnd);
        const filenameMatch = headers.match(/filename="(.+?)"/);
        if (!filenameMatch) continue;

        const filename = filenameMatch[1];
        const ext = extname(filename).toLowerCase();
        const mediaId = randomUUID().slice(0, 12);
        const savedName = `${mediaId}${ext}`;
        const savedPath = resolve(MEDIA_DIR, savedName);

        const bodyData = part.slice(headerEnd + 4, part.lastIndexOf('\r\n'));
        writeFileSync(savedPath, bodyData, 'binary');

        const mimeMatch = headers.match(/Content-Type:\s*(.+)/i);
        const mime = mimeMatch ? mimeMatch[1].trim() : MIME_TYPES[ext] || 'application/octet-stream';

        const tweetIdx = parseInt(params.tweet_idx || '0', 10);
        stmts.insertMedia.run(mediaId, draft.id, tweetIdx, savedPath, mime);

        return send(res, 201, { ok: true, id: mediaId, path: `/media/${savedName}`, file_path: savedPath });
      }

      return send(res, 400, { error: 'No file found in upload' });
    }

    // DELETE /drafts/:id/media/:mid
    const mediaDelMatch = path.match(/^\/drafts\/([a-z0-9-]+)\/media\/([a-z0-9-]+)$/);
    if (req.method === 'DELETE' && mediaDelMatch) {
      stmts.deleteMedia.run(mediaDelMatch[2], mediaDelMatch[1]);
      return send(res, 200, { ok: true });
    }

    // ── posting ───────────────────────────────────────────

    // POST /post — post a draft or raw tweets
    // Duplicate protection: in-flight lock + draft status check
    if (req.method === 'POST' && path === '/post') {
      const body = await readBody(req);
      let tweets;
      let draftId = body.draft_id;

      if (draftId) {
        const draft = stmts.getDraft.get(draftId);
        if (!draft) return send(res, 404, { error: 'Draft not found' });
        // Block if already posted or currently being posted
        if (draft.status === 'posted') return send(res, 409, { error: 'Already posted', url: draft.tweet_url });
        if (draft.status === 'posting') return send(res, 409, { error: 'Post already in flight — wait for it to complete' });
        // Mark as posting to prevent concurrent attempts
        stmts.updateDraftStatus.run('posting', draftId);
        tweets = JSON.parse(draft.thread_json);
      } else {
        // For raw posts (no draft), check content-based dedup
        const textKey = (body.tweets || [{ text: body.text }]).map(t => t.text).join('||');
        if (postInFlight.has(textKey)) return send(res, 409, { error: 'Duplicate post in flight' });
        postInFlight.add(textKey);
        // Auto-clear after 30s as safety valve
        setTimeout(() => postInFlight.delete(textKey), 30000);
        tweets = body.tweets || [{ text: body.text }];
      }

      try {
        // Upload any local media to VPS first
        const vpsPayload = [];
        for (let i = 0; i < tweets.length; i++) {
          const t = tweets[i];
          const mediaPaths = [];

          if (draftId) {
            const mediaRows = stmts.listMedia.all(draftId).filter(m => m.tweet_idx === i);
            for (const m of mediaRows) {
              if (existsSync(m.file_path)) {
                const vpsPath = await uploadMediaToVPS(m.file_path);
                mediaPaths.push(vpsPath);
              }
            }
          }

          // Also handle inline base64 media (from UI)
          if (t.media && Array.isArray(t.media)) {
            for (const img of t.media) {
              if (img.data) {
                const ext = (img.mimeType || 'image/png').split('/')[1] || 'png';
                const tmpPath = resolve(MEDIA_DIR, `tmp_${randomUUID().slice(0,8)}.${ext}`);
                writeFileSync(tmpPath, Buffer.from(img.data, 'base64'));
                const vpsPath = await uploadMediaToVPS(tmpPath);
                mediaPaths.push(vpsPath);
                unlinkSync(tmpPath);
              }
            }
          }

          vpsPayload.push({
            text: t.text || ' ',
            media_paths: mediaPaths.length ? mediaPaths : null,
            in_reply_to_tweet_id: i === 0 ? (t.in_reply_to_tweet_id || body.in_reply_to_tweet_id || null) : null,
            quote_tweet_id: i === 0 ? (t.quote_tweet_id || body.quote_tweet_id || null) : null,
          });
        }

        const result = await vpsRequest('POST', '/post', { tweets: vpsPayload });

        if (draftId) {
          stmts.updateDraftPosted.run(new Date().toISOString(), result.url, draftId);
        }

        // The freshly posted tweet must show up on next feed visit — drop the
        // mine cache so the next load fetches live.
        db.prepare("DELETE FROM feed_cache WHERE feed_key = 'mine'").run();

        return send(res, 200, { ok: true, url: result.url, id: result.id });
      } catch (postErr) {
        // Reset draft status on failure so user can retry
        if (draftId) stmts.updateDraftStatus.run('draft', draftId);
        throw postErr;
      }
    }

    // ── scheduling ────────────────────────────────────────

    // POST /schedule
    if (req.method === 'POST' && path === '/schedule') {
      const body = await readBody(req);
      const tweets = body.tweets || [{ text: body.text }];
      const scheduledAt = body.scheduled_at;

      if (!scheduledAt) return send(res, 400, { error: 'scheduled_at required' });

      const result = await vpsRequest('POST', '/schedule', {
        tweets: tweets.map(t => ({ text: t.text, media_paths: t.media_paths || null })),
        scheduled_at: scheduledAt,
      });

      if (body.draft_id) {
        stmts.updateDraftScheduled.run(scheduledAt, body.draft_id);
      }

      return send(res, 200, result);
    }

    // GET /queue
    if (req.method === 'GET' && path === '/queue') {
      const data = await vpsRequest('GET', '/queue');
      return send(res, 200, data);
    }

    // DELETE /queue/:id
    const queueDelMatch = path.match(/^\/queue\/([a-z0-9]+)$/);
    if (req.method === 'DELETE' && queueDelMatch) {
      const data = await vpsRequest('DELETE', `/queue/${queueDelMatch[1]}`);
      return send(res, 200, data);
    }

    // POST/DELETE /tweet/:id/like — like / unlike
    const likeMatch = path.match(/^\/tweet\/(\d+)\/like$/);
    if (likeMatch && (req.method === 'POST' || req.method === 'DELETE')) {
      const data = await vpsRequest(req.method, `/tweet/${likeMatch[1]}/like`);
      return send(res, 200, data);
    }

    // POST/DELETE /tweet/:id/retweet — repost / undo repost
    const rtMatch = path.match(/^\/tweet\/(\d+)\/retweet$/);
    if (rtMatch && (req.method === 'POST' || req.method === 'DELETE')) {
      const data = await vpsRequest(req.method, `/tweet/${rtMatch[1]}/retweet`);
      return send(res, 200, data);
    }

    // DELETE /tweet/:id — delete a tweet
    const tweetDelMatch = path.match(/^\/tweet\/(\d+)$/);
    if (req.method === 'DELETE' && tweetDelMatch) {
      const data = await vpsRequest('DELETE', `/tweet/${tweetDelMatch[1]}`);
      return send(res, 200, data);
    }

    // ── feed + stats (proxy to VPS) ──────────────────────

    if (req.method === 'GET' && path === '/tweets') {
      const count = params.count || 20;
      const data = await vpsRequest('GET', `/tweets?count=${count}`);
      return send(res, 200, data);
    }

    if (req.method === 'GET' && path === '/activity') {
      const count = params.count || 20;
      const data = await vpsRequest('GET', `/activity?count=${count}`);
      return send(res, 200, data);
    }

    if (req.method === 'GET' && path === '/stats') {
      const data = await vpsRequest('GET', '/stats');
      return send(res, 200, data);
    }

    if (req.method === 'GET' && path === '/status') {
      try {
        const data = await vpsRequest('GET', '/status');
        return send(res, 200, { ...data, configured: true });
      } catch {
        return send(res, 200, { ok: false, configured: false, error: 'VPS unreachable' });
      }
    }

    // ── multi-feed system ─────────────────────────────────
    // All X-backed feeds are TTL-cached (feed_cache) to protect credits.
    // ?refresh=1 forces a live fetch.

    // GET /feeds/mine?sort=top|recent — own tweets, cached 15 min
    if (req.method === 'GET' && path === '/feeds/mine') {
      const data = await cachedFeed('mine', 15, params.refresh, async () => {
        const d = await vpsRequest('GET', '/tweets?count=100');
        // Persist into tweets_cache for analytics history
        for (const t of d.tweets || []) {
          stmts.upsertTweetCache.run(String(t.id), t.text, JSON.stringify(t.metrics), t.created_at);
        }
        return d;
      });
      let tweets = data.tweets || [];
      if (params.sort === 'top') {
        tweets = [...tweets].sort((a, b) => engagementScore(b.metrics) - engagementScore(a.metrics));
      }
      return send(res, 200, { ...data, tweets, sort: params.sort || 'recent' });
    }

    // GET /feeds/replies — mentions inbox with local done-toggle, cached 10 min
    if (req.method === 'GET' && path === '/feeds/replies') {
      const data = await cachedFeed('replies', 10, params.refresh, () => vpsRequest('GET', '/activity?count=50'));
      const done = new Set(stmts.listRepliesDone.all().map(r => r.tweet_id));
      const mentions = (data.mentions || []).map(m => ({ ...m, done: done.has(String(m.id)) }));
      return send(res, 200, { ...data, mentions });
    }

    // POST /feeds/replies/:id/done {done:bool} — toggle replied-to state
    const replyDoneMatch = path.match(/^\/feeds\/replies\/(\d+)\/done$/);
    if (req.method === 'POST' && replyDoneMatch) {
      const body = await readBody(req);
      if (body.done === false) stmts.unmarkReplyDone.run(replyDoneMatch[1]);
      else stmts.markReplyDone.run(replyDoneMatch[1]);
      return send(res, 200, { ok: true });
    }

    // GET /feeds/arena — blocks from configured Are.na channels, cached 30 min
    if (req.method === 'GET' && path === '/feeds/arena') {
      const data = await cachedFeed('arena', 30, params.refresh, async () => {
        const channels = JSON.parse(stmts.getSetting.get('arena_channels')?.value || '[]');
        const blocks = [];
        for (const slug of channels) {
          const r = await fetch(`https://api.are.na/v2/channels/${encodeURIComponent(slug)}?per=50`, {
            headers: { 'User-Agent': 'amber-x/2.0' },
          });
          if (!r.ok) continue;
          const ch = await r.json();
          for (const b of ch.contents || []) {
            blocks.push({
              id: b.id,
              class: b.class,
              title: b.title || b.generated_title || '',
              content: b.content || '',
              image: b.image?.display?.url || b.image?.thumb?.url || null,
              source_url: b.source?.url || null,
              channel: ch.title,
              channel_slug: slug,
              connected_at: b.connected_at,
            });
          }
        }
        blocks.sort((a, b) => new Date(b.connected_at) - new Date(a.connected_at));
        return { blocks, channels };
      });
      return send(res, 200, data);
    }

    // GET /feeds/topical — keyword-curated X search, cached 60 min (credit-hungry)
    if (req.method === 'GET' && path === '/feeds/topical') {
      const data = await cachedFeed('topical', 60, params.refresh, async () => {
        const keywords = JSON.parse(stmts.getSetting.get('feed_keywords')?.value || '[]');
        const seen = new Map();
        const errors = [];
        for (const kw of keywords) {
          try {
            const q = encodeURIComponent(`${kw} -is:retweet`);
            const d = await vpsRequest('GET', `/search?q=${q}&count=25`);
            for (const t of d.tweets || []) {
              if (!seen.has(String(t.id))) seen.set(String(t.id), { ...t, matched_keyword: kw });
            }
          } catch (e) {
            errors.push({ keyword: kw, error: e.message });
          }
        }
        const tweets = [...seen.values()].sort((a, b) =>
          engagementScore(b.metrics) - engagementScore(a.metrics) ||
          new Date(b.created_at) - new Date(a.created_at));
        return { tweets, keywords, errors: errors.length ? errors : undefined };
      });
      return send(res, 200, data);
    }

    // ── settings (keyword/algorithm editor backend) ──────
    const settingMatch = path.match(/^\/settings\/([a-z_]+)$/);
    if (req.method === 'GET' && settingMatch) {
      const row = stmts.getSetting.get(settingMatch[1]);
      if (!row) return send(res, 404, { error: 'Not found' });
      return send(res, 200, { key: settingMatch[1], value: JSON.parse(row.value) });
    }
    if (req.method === 'PUT' && settingMatch) {
      const body = await readBody(req);
      stmts.setSetting.run(settingMatch[1], JSON.stringify(body.value));
      // Invalidate feeds that depend on settings
      if (settingMatch[1] === 'feed_keywords') db.prepare("DELETE FROM feed_cache WHERE feed_key = 'topical'").run();
      if (settingMatch[1] === 'arena_channels') db.prepare("DELETE FROM feed_cache WHERE feed_key = 'arena'").run();
      return send(res, 200, { ok: true, key: settingMatch[1], value: body.value });
    }

    // ── analytics (computed from cache, zero credits) ─────
    if (req.method === 'GET' && path === '/analytics') {
      const row = stmts.getFeedCache.get('mine');
      if (!row) return send(res, 200, { ok: false, error: 'No tweet data cached yet — open the Mine feed first' });
      const tweets = (JSON.parse(row.json).tweets || [])
        .slice()
        .sort((a, b) => new Date(b.created_at) - new Date(a.created_at));

      const now = Date.now();
      const days = d => tweets.filter(t => now - new Date(t.created_at).getTime() < d * 86400000).length;
      const windowAvg = n => {
        const w = tweets.slice(0, n);
        if (!w.length) return null;
        const sum = k => w.reduce((s, t) => s + (t.metrics[k] || 0), 0);
        return {
          n: w.length,
          avg_likes: +(sum('likes') / w.length).toFixed(1),
          avg_retweets: +(sum('retweets') / w.length).toFixed(1),
          avg_replies: +(sum('replies') / w.length).toFixed(1),
          avg_impressions: Math.round(sum('impressions') / w.length),
          avg_engagement: +(w.reduce((s, t) => s + engagementScore(t.metrics), 0) / w.length).toFixed(1),
        };
      };
      const replies = tweets.filter(t => t.text.startsWith('@')).length;

      return send(res, 200, {
        ok: true,
        sample: tweets.length,
        cached_at: row.fetched_at,
        counts: { last_7d: days(7), last_30d: days(30), last_90d: days(90) },
        windows: { last_10: windowAvg(10), last_20: windowAvg(20), last_50: windowAvg(50), last_100: windowAvg(100) },
        replies_vs_posts: { replies, posts: tweets.length - replies },
      });
    }

    // ── assets ────────────────────────────────────────────

    // GET /assets
    if (req.method === 'GET' && path === '/assets') {
      const assets = stmts.listAssets.all().map(a => ({
        ...a, tags: JSON.parse(a.tags || '[]'),
      }));
      return send(res, 200, { assets });
    }

    // POST /assets — create text asset or upload image
    if (req.method === 'POST' && path === '/assets') {
      const ct = req.headers['content-type'] || '';

      if (ct.includes('multipart/form-data')) {
        // File upload
        const buf = await readMultipart(req);
        const boundaryMatch = ct.match(/boundary=(.+)/);
        if (!boundaryMatch) return send(res, 400, { error: 'Missing boundary' });

        const boundary = boundaryMatch[1];
        const parts = buf.toString('binary').split(`--${boundary}`);
        for (const part of parts) {
          const headerEnd = part.indexOf('\r\n\r\n');
          if (headerEnd === -1) continue;
          const headers = part.slice(0, headerEnd);
          const filenameMatch = headers.match(/filename="(.+?)"/);
          if (!filenameMatch) continue;

          const filename = filenameMatch[1];
          const ext = extname(filename).toLowerCase();
          const assetId = randomUUID().slice(0, 12);
          const savedName = `${assetId}${ext}`;
          const savedPath = resolve(ASSETS_DIR, savedName);

          const bodyData = part.slice(headerEnd + 4, part.lastIndexOf('\r\n'));
          writeFileSync(savedPath, bodyData, 'binary');

          stmts.insertAsset.run(assetId, 'image', filename, null, savedPath, '[]');
          return send(res, 201, { ok: true, id: assetId, path: `/media/assets/${savedName}` });
        }
        return send(res, 400, { error: 'No file in upload' });
      }

      // Text/note asset
      const body = await readBody(req);
      const id = randomUUID().slice(0, 12);
      stmts.insertAsset.run(id, body.type || 'text', body.title || null, body.content || '', null, JSON.stringify(body.tags || []));
      return send(res, 201, { ok: true, id });
    }

    // DELETE /assets/:id
    const assetDelMatch = path.match(/^\/assets\/([a-z0-9-]+)$/);
    if (req.method === 'DELETE' && assetDelMatch) {
      const asset = stmts.getAsset.get(assetDelMatch[1]);
      if (!asset) return send(res, 404, { error: 'Not found' });
      if (asset.file_path && existsSync(asset.file_path)) {
        unlinkSync(asset.file_path);
      }
      stmts.deleteAsset.run(asset.id);
      return send(res, 200, { ok: true });
    }

    // ── jam (CC integration) ─────────────────────────────

    // POST /drafts/:id/jam — prepare draft for CC session
    const jamMatch = path.match(/^\/drafts\/([a-z0-9-]+)\/jam$/);
    if (req.method === 'POST' && jamMatch) {
      const draft = stmts.getDraft.get(jamMatch[1]);
      if (!draft) return send(res, 404, { error: 'Draft not found' });

      const thread = JSON.parse(draft.thread_json);
      const jamData = {
        draft_id: draft.id,
        thread,
        created_at: new Date().toISOString(),
      };

      writeFileSync(resolve(JAM_DIR, 'active.json'), JSON.stringify(jamData, null, 2));

      // Ready-to-paste prompt: invokes /cc-x-jam and carries the draft text
      // (the skill reads jam/active.json; the inline text is context + fallback).
      const draftText = thread
        .map((t, i) => (thread.length > 1 ? `${i + 1}. ${t.text}` : t.text))
        .join('\n\n');
      const prompt = `/cc-x-jam\n\nDraft (id: ${draft.id}) — also saved to jam/active.json:\n${draftText}`;

      // Server runs on the Mac, so pbcopy targets the same clipboard the CC
      // session pastes from. Also bring Terminal forward — jam is one paste away.
      try {
        if (process.platform !== 'darwin') throw new Error('clipboard/Terminal only on Mac — browser fallback handles it');
        const pb = spawn('pbcopy');
        pb.stdin.end(prompt);
        spawn('open', ['-a', 'Terminal']);
      } catch (e) {
        console.error('jam clipboard/terminal failed:', e.message);
      }

      return send(res, 200, { ok: true, prompt, message: 'Copied /cc-x-jam prompt — paste in Terminal' });
    }

    // ── 404 ──────────────────────────────────────────────

    send(res, 404, { error: 'Not found' });

  } catch (e) {
    console.error(`${req.method} ${path} error:`, e.message);
    send(res, 500, { error: e.message });
  }
});

// ── startup ─────────────────────────────────────────────────

if (process.env.NO_TUNNEL !== '1') startTunnel(); // NO_TUNNEL=1 for dev instances (prod already tunnels :8142)

server.listen(PORT, () => {
  console.log(`x-vibepoastry → http://localhost:${PORT}`);
  console.log(`Data: ${DATA_DIR}`);
});

// ── cleanup ─────────────────────────────────────────────────

process.on('SIGINT', () => {
  if (tunnelProcess) tunnelProcess.kill();
  db.close();
  process.exit(0);
});

process.on('SIGTERM', () => {
  if (tunnelProcess) tunnelProcess.kill();
  db.close();
  process.exit(0);
});
