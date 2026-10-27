import test from 'node:test';
import assert from 'node:assert/strict';
import { PassThrough, Readable } from 'node:stream';
import { LoginLimiter, readLoginPassword } from '../login.js';

function request(body, type = 'application/json') {
  const req = Readable.from([Buffer.from(body)]);
  req.headers = { 'content-type': type };
  return req;
}

test('login parser accepts a string password and rejects malformed input', async () => {
  assert.equal(await readLoginPassword(request('{"password":"secret"}')), 'secret');
  for (const body of ['{', 'null', '[]', '{}', '{"password":4}', '{"password":""}', JSON.stringify({ password: 'a'.repeat(1025) })]) {
    await assert.rejects(readLoginPassword(request(body)), /invalid_request/);
  }
  await assert.rejects(readLoginPassword(request('password=secret', 'application/x-www-form-urlencoded')), /invalid_request/);
  await assert.rejects(readLoginPassword(request('a'.repeat(4097))), /invalid_request/);
});

test('login parser bounds a streaming body and a stalled request', async () => {
  const req = new PassThrough();
  req.headers = { 'content-type': 'application/json' };
  const parsed = readLoginPassword(req, 100);
  req.write(Buffer.alloc(4096, 97));
  req.write(Buffer.from('a'));
  await assert.rejects(parsed, /invalid_request/);
  assert.equal(req.listenerCount('data'), 0);
  const stalled = new PassThrough();
  stalled.headers = { 'content-type': 'application/json' };
  await assert.rejects(readLoginPassword(stalled, 10), /invalid_request/);
  assert.equal(stalled.listenerCount('data'), 0);
});

test('attempts reserve synchronously and recover after the rolling window', () => {
  let now = 100_000;
  const limiter = new LoginLimiter(() => now);
  assert.deepEqual(Array.from({ length: 6 }, () => limiter.reserve('one')), [0, 0, 0, 0, 0, 60]);
  now += 1000;
  assert.equal(limiter.reserve('one'), 59);
  now += 59_000;
  assert.equal(limiter.reserve('one'), 0);
  limiter.reset('one');
  assert.equal(limiter.reserve('one'), 0);
});

test('global admission bounds rotating IPs and limiter memory', () => {
  let now = 100_000;
  const limiter = new LoginLimiter(() => now);
  for (let i = 0; i < 50; i++) assert.equal(limiter.reserve(String(i)), 0);
  for (let i = 50; i < 500; i++) assert.equal(limiter.reserve(String(i)), 60);
  assert.equal(limiter.attempts.size, 50);
  now += 60_000;
  assert.equal(limiter.reserve('new'), 0);
  assert.equal(limiter.attempts.size, 1);
});
