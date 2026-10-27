// Reserve attempts before asynchronous body reads to bound concurrent logins.
export class LoginLimiter {
  constructor(now = Date.now) {
    this.now = now;
    this.attempts = new Map();
    this.global = [];
  }

  reserve(ip) {
    const now = this.now();
    const cutoff = now - 60_000;
    this.global = this.global.filter(time => time > cutoff);
    for (const [key, times] of this.attempts) {
      const active = times.filter(time => time > cutoff);
      if (active.length) this.attempts.set(key, active);
      else this.attempts.delete(key);
    }
    const times = this.attempts.get(ip) || [];
    const blockedUntil = Math.max(
      times.length >= 5 ? times[0] + 60_000 : 0,
      this.global.length >= 50 ? this.global[0] + 60_000 : 0,
    );
    if (blockedUntil > now) return Math.ceil((blockedUntil - now) / 1000);
    this.attempts.set(ip, [...times, now]);
    this.global.push(now);
    return 0;
  }

  reset(ip) {
    this.attempts.delete(ip);
  }
}

export function readLoginPassword(req, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    let settled = false;
    const timer = setTimeout(() => finish(new Error('invalid_request')), timeoutMs);
    function finish(error, password) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      req.off('data', onData);
      req.off('end', onEnd);
      req.off('error', onError);
      req.off('aborted', onError);
      if (error) {
        req.resume();
        reject(error);
      } else resolve(password);
    }
    function onError() { finish(new Error('invalid_request')); }
    function onData(chunk) {
      size += chunk.length;
      if (size > 4096) return finish(new Error('invalid_request'));
      chunks.push(chunk);
    }
    function onEnd() {
      try {
        const raw = new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks));
        const body = JSON.parse(raw);
        if (!body || Array.isArray(body) || typeof body.password !== 'string' ||
            !body.password.length || Buffer.byteLength(body.password) > 1024) {
          return finish(new Error('invalid_request'));
        }
        finish(null, body.password);
      } catch { finish(new Error('invalid_request')); }
    }
    req.on('error', onError);
    req.on('aborted', onError);
    req.on('data', onData);
    req.on('end', onEnd);
    const type = req.headers['content-type'] || '';
    const length = req.headers['content-length'];
    if (!/^application\/json(?:\s*;.*)?$/i.test(type) ||
        (length !== undefined && (!/^\d+$/.test(length) || Number(length) > 4096))) {
      finish(new Error('invalid_request'));
    }
  });
}
