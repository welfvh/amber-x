#!/usr/bin/env node
// amber-x server — local X/Twitter composer backend
// Credentials stored in .env, never leave your machine.
// Port: 3131

import 'dotenv/config';
import { writeFileSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath }    from 'url';
import http         from 'http';
import { TwitterApi } from 'twitter-api-v2';

const PORT     = 3131;
const ENV_PATH = resolve(dirname(fileURLToPath(import.meta.url)), '.env');

// ── credential helpers ─────────────────────────────────────

function loadCreds() {
  const { X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET } = process.env;
  if (!X_API_KEY || !X_API_SECRET || !X_ACCESS_TOKEN || !X_ACCESS_TOKEN_SECRET) return null;
  return { apiKey: X_API_KEY, apiSecret: X_API_SECRET, accessToken: X_ACCESS_TOKEN, accessSecret: X_ACCESS_TOKEN_SECRET };
}

function saveCreds({ apiKey, apiSecret, accessToken, accessSecret }) {
  const content = [
    `X_API_KEY=${apiKey}`,
    `X_API_SECRET=${apiSecret}`,
    `X_ACCESS_TOKEN=${accessToken}`,
    `X_ACCESS_TOKEN_SECRET=${accessSecret}`,
  ].join('\n') + '\n';
  writeFileSync(ENV_PATH, content, { mode: 0o600 }); // owner-read only
  // reload into current process
  process.env.X_API_KEY             = apiKey;
  process.env.X_API_SECRET          = apiSecret;
  process.env.X_ACCESS_TOKEN        = accessToken;
  process.env.X_ACCESS_TOKEN_SECRET = accessSecret;
}

function makeClient(creds) {
  return new TwitterApi({
    appKey:       creds.apiKey,
    appSecret:    creds.apiSecret,
    accessToken:  creds.accessToken,
    accessSecret: creds.accessSecret,
  }).readWrite;
}

// ── media upload ───────────────────────────────────────────
async function uploadAllMedia(client, tweets) {
  return Promise.all(tweets.map(async tweet => {
    if (!(tweet.media || []).length) return [];
    return Promise.all(tweet.media.map(img =>
      client.v1.uploadMedia(Buffer.from(img.data, 'base64'), { mimeType: img.mimeType })
    ));
  }));
}

// ── post thread ────────────────────────────────────────────
async function postThread(client, tweets) {
  const mediaIdSets = await uploadAllMedia(client, tweets);
  let previousId = null;
  let firstId    = null;

  for (let i = 0; i < tweets.length; i++) {
    const payload = { text: tweets[i].text || ' ' };
    if (mediaIdSets[i].length) payload.media = { media_ids: mediaIdSets[i] };
    if (previousId)            payload.reply  = { in_reply_to_tweet_id: previousId };

    const result = await client.v2.tweet(payload);
    previousId   = result.data.id;
    if (!firstId) firstId = previousId;
  }

  return firstId;
}

// ── http helpers ───────────────────────────────────────────
function cors(res) {
  res.setHeader('Access-Control-Allow-Origin',  '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,OPTIONS');
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
    req.on('data',  c => chunks.push(c));
    req.on('end',   () => { try { resolve(JSON.parse(Buffer.concat(chunks).toString() || '{}')); } catch { reject(new Error('Bad JSON')); } });
    req.on('error', reject);
  });
}

// ── routes ─────────────────────────────────────────────────
const server = http.createServer(async (req, res) => {
  if (req.method === 'OPTIONS') { cors(res); res.writeHead(204); res.end(); return; }

  // GET /status
  if (req.method === 'GET' && req.url === '/status') {
    const creds = loadCreds();
    if (!creds) return send(res, 200, { ok: false, configured: false });
    try {
      const me = await makeClient(creds).v2.me();
      return send(res, 200, { ok: true, configured: true, username: me.data.username });
    } catch (e) {
      return send(res, 200, { ok: false, configured: true, error: 'Credentials invalid — check your keys' });
    }
  }

  // POST /setup — verify then save credentials to .env
  if (req.method === 'POST' && req.url === '/setup') {
    try {
      const { apiKey, apiSecret, accessToken, accessSecret } = await readBody(req);
      if (!apiKey || !apiSecret || !accessToken || !accessSecret)
        return send(res, 400, { error: 'All four keys are required' });

      const client = new TwitterApi({ appKey: apiKey, appSecret: apiSecret, accessToken, accessSecret }).readWrite;
      const me     = await client.v2.me();
      saveCreds({ apiKey, apiSecret, accessToken, accessSecret });
      console.log(`✓ Credentials saved for @${me.data.username}`);
      return send(res, 200, { ok: true, username: me.data.username });
    } catch (e) {
      const msg = e.data?.detail || e.message || 'Unknown error';
      return send(res, 400, { error: `Could not verify: ${msg}` });
    }
  }

  // POST /post
  if (req.method === 'POST' && req.url === '/post') {
    const creds = loadCreds();
    if (!creds) return send(res, 401, { error: 'Not configured — add your API keys first' });

    try {
      const body   = await readBody(req);
      const tweets = Array.isArray(body.tweets) ? body.tweets
                   : body.text                  ? [{ text: body.text, media: [] }]
                   : null;

      if (!tweets?.length) return send(res, 400, { error: 'No tweets provided' });
      for (const t of tweets) {
        if ((t.text || '').length > 280) return send(res, 400, { error: 'Tweet over 280 characters' });
        if ((t.media || []).length > 4)  return send(res, 400, { error: 'Max 4 images per tweet' });
      }

      const total = tweets.reduce((n, t) => n + (t.media || []).length, 0);
      console.log(`Posting ${tweets.length} tweet(s), ${total} image(s)…`);

      const client  = makeClient(creds);
      const firstId = await postThread(client, tweets);
      const me      = await client.v2.me();
      const url     = `https://x.com/${me.data.username}/status/${firstId}`;

      console.log(`✓ ${url}`);
      return send(res, 200, { ok: true, url, id: firstId });
    } catch (e) {
      const msg = e.data?.detail || e.data?.title || e.message || 'Unknown error';
      console.error('Post error:', msg);
      return send(res, 500, { error: msg });
    }
  }

  send(res, 404, { error: 'Not found' });
});

server.listen(PORT, () => {
  const creds = loadCreds();
  console.log(`amber-x → http://localhost:${PORT}`);
  console.log(creds ? '✓ Credentials loaded from .env' : '⚠  No credentials — open the app to set up');
});
