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
import { randomUUID } from 'crypto';
import Database from 'better-sqlite3';

const PORT = 3131;
const __dir = dirname(fileURLToPath(import.meta.url));
const VPS_HOST = 'root@162.55.60.42';
const VPS_PORT = 8142;
const DATA_DIR = resolve(process.env.HOME, '.local/share/x-vibepoastry');
const MEDIA_DIR = resolve(DATA_DIR, 'media');
const ASSETS_DIR = resolve(DATA_DIR, 'media/assets');
const JAM_DIR = resolve(DATA_DIR, 'jam');
const DB_PATH = resolve(DATA_DIR, 'data.db');

// Auth token for VPS x-vibepoastry service — read from Keychain at startup
let VPS_AUTH_TOKEN = '';
try {
  VPS_AUTH_TOKEN = execSync('security find-generic-password -s "cc/x-vibepoastry" -a "auth_token" -w', { encoding: 'utf8' }).trim();
} catch {
  console.error('WARNING: cc/x-vibepoastry auth token not found in Keychain');
}

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
`);

// Prepared statements for performance
const stmts = {
  listDrafts: db.prepare('SELECT * FROM drafts ORDER BY updated_at DESC'),
  getDraft: db.prepare('SELECT * FROM drafts WHERE id = ?'),
  insertDraft: db.prepare('INSERT INTO drafts (id, thread_json) VALUES (?, ?)'),
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
};

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

// ── routes ──────────────────────────────────────────────────

const server = http.createServer(async (req, res) => {
  if (req.method === 'OPTIONS') { cors(res); res.writeHead(204); res.end(); return; }

  const { path, params } = parseRoute(req.url);

  try {
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

    // GET /drafts — list all drafts
    if (req.method === 'GET' && path === '/drafts') {
      const drafts = stmts.listDrafts.all().map(d => ({
        ...d, thread: JSON.parse(d.thread_json),
      }));
      return send(res, 200, { drafts });
    }

    // POST /drafts — create draft
    if (req.method === 'POST' && path === '/drafts') {
      const body = await readBody(req);
      const id = randomUUID().slice(0, 12);
      const thread = body.thread || body.tweets || [{ text: body.text || '' }];
      stmts.insertDraft.run(id, JSON.stringify(thread));
      return send(res, 201, { ok: true, id, thread });
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

          vpsPayload.push({ text: t.text || ' ', media_paths: mediaPaths.length ? mediaPaths : null });
        }

        const result = await vpsRequest('POST', '/post', { tweets: vpsPayload });

        if (draftId) {
          stmts.updateDraftPosted.run(new Date().toISOString(), result.url, draftId);
        }

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

      const jamData = {
        draft_id: draft.id,
        thread: JSON.parse(draft.thread_json),
        created_at: new Date().toISOString(),
      };

      writeFileSync(resolve(JAM_DIR, 'active.json'), JSON.stringify(jamData, null, 2));
      return send(res, 200, { ok: true, message: 'Jam ready — run /cc-x-jam in Claude Code' });
    }

    // ── 404 ──────────────────────────────────────────────

    send(res, 404, { error: 'Not found' });

  } catch (e) {
    console.error(`${req.method} ${path} error:`, e.message);
    send(res, 500, { error: e.message });
  }
});

// ── startup ─────────────────────────────────────────────────

startTunnel();

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
