import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import http from 'node:http';

function login(body, type = 'application/json') {
  return new Promise((resolve, reject) => {
    const req = http.request('http://127.0.0.1:13931/login', {
      method: 'POST', headers: { 'Content-Type': type },
    }, res => {
      const chunks = [];
      res.on('data', chunk => chunks.push(chunk));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks).toString() }));
    });
    req.on('error', reject);
    req.end(body);
  });
}

test('real server rejects bad input, persists a session, and limits a parallel burst', async () => {
  const server = spawn(process.execPath, ['/source/server.js'], {
    env: {
      PATH: '/usr/local/bin:/usr/bin:/bin', HOME: '/scratch/server', TMPDIR: '/scratch',
      PORT: '13931', NO_TUNNEL: '1', XVP_AUTH_TOKEN: 'synthetic-vps-token', XVP_UI_PASSWORD: 'synthetic-password',
    }, stdio: ['ignore', 'pipe', 'pipe'],
  });
  let diagnostics = '';
  server.stderr.on('data', chunk => { diagnostics += chunk.toString(); });
  let startupTimer;
  try {
    await Promise.race([
      new Promise(resolve => server.stdout.on('data', chunk => { if (chunk.toString().includes('http://localhost:13931')) resolve(); })),
      once(server, 'exit').then(() => { throw new Error('Server failed: ' + diagnostics); }),
      new Promise((_, reject) => { startupTimer = setTimeout(() => reject(new Error('Server startup timed out')), 5000); }),
    ]);
    clearTimeout(startupTimer);
    assert.equal((await fetch('http://127.0.0.1:13931/drafts')).status, 401);
    assert.equal((await login('{')).status, 400);
    assert.equal((await login('password=oops', 'application/x-www-form-urlencoded')).status, 400);
    const success = await login('{"password":"synthetic-password"}');
    assert.equal(success.status, 200);
    assert.match(success.headers['set-cookie'][0], /HttpOnly; Secure; SameSite=Lax/);
    assert.equal(success.headers['cache-control'], 'no-store');
    const cookie = success.headers['set-cookie'][0].split(';')[0];
    const privateRead = await fetch('http://127.0.0.1:13931/drafts', { headers: { Cookie: cookie } });
    assert.equal(privateRead.status, 200);
    const burst = await Promise.all(Array.from({ length: 10 }, () => login('{"password":"wrong"}')));
    assert.equal(burst.filter(result => result.status === 401).length, 5);
    assert.equal(burst.filter(result => result.status === 429).length, 5);
    for (const result of burst.filter(result => result.status === 429)) {
      assert.ok(Number(result.headers['retry-after']) > 0);
      assert.equal(result.body, '{"error":"too_many_attempts"}');
    }
  } finally {
    clearTimeout(startupTimer);
    if (server.exitCode === null && server.signalCode === null && server.pid !== undefined) {
      const exited = once(server, 'exit');
      server.kill('SIGTERM');
      await exited;
    }
  }
});
