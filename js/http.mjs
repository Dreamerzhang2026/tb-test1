// ---------------------------------------------------------------------------
// 来源: 另一 AI 为同一用户做的 tvbox-hub 项目 (lib/http.mjs, MIT).
// 用户 2026-11-13 决定把评审中值得借鉴的部分并入本项目, 本文件为原样复制
// (仅 http.mjs 去掉了证书失败时不验证重试的逻辑, 见该文件内注释)。
// 上游代码由对方 AI 编写, 本项目仅作复用, 不宣称原创。
// ---------------------------------------------------------------------------
// Zero-dependency HTTP client: direct, or through an HTTP CONNECT proxy when
// HTTPS_PROXY / HTTP_PROXY is set. Honours NO_PROXY.
import http from 'node:http';
import https from 'node:https';
import tls from 'node:tls';
import zlib from 'node:zlib';

const PROXY =
  process.env.HTTPS_PROXY || process.env.https_proxy ||
  process.env.HTTP_PROXY || process.env.http_proxy || '';
const NO_PROXY = (process.env.NO_PROXY || process.env.no_proxy || '')
  .split(',').map((s) => s.trim().toLowerCase()).filter(Boolean);

function bypassProxy(host) {
  const h = host.toLowerCase();
  return NO_PROXY.some((p) => p === '*' || h === p || h.endsWith('.' + p.replace(/^\./, '')));
}

function proxyAuthHeader(p) {
  if (!p.username) return null;
  const cred = `${decodeURIComponent(p.username)}:${decodeURIComponent(p.password)}`;
  return 'Basic ' + Buffer.from(cred).toString('base64');
}

function connectTunnel(host, port, timeout) {
  return new Promise((resolve, reject) => {
    const p = new URL(PROXY);
    const auth = proxyAuthHeader(p);
    const req = http.request({
      host: p.hostname,
      port: p.port || 80,
      method: 'CONNECT',
      path: `${host}:${port}`,
      headers: { Host: `${host}:${port}`, ...(auth ? { 'Proxy-Authorization': auth } : {}) },
      timeout,
    });
    req.on('connect', (res, socket) => {
      if (res.statusCode !== 200) { socket.destroy(); reject(new Error(`proxy CONNECT ${res.statusCode}`)); return; }
      resolve(socket);
    });
    req.on('timeout', () => req.destroy(new Error('proxy connect timeout')));
    req.on('error', reject);
    req.end();
  });
}

// An agent that hands out one pre-established socket (e.g. a TLS socket on top
// of a CONNECT tunnel). Node's built-in proxy support only covers http.request's
// absolute-form, so this is how https-over-proxy gets done without dependencies.
class HttpsOneSocketAgent extends https.Agent {
  constructor(socket) {
    super({ keepAlive: false, maxSockets: 1 });
    this._socket = socket;
  }
  createConnection() { return this._socket; }
}

function decode(buf, enc) {
  const e = String(enc || '').toLowerCase();
  try {
    if (e === 'gzip' || e === 'x-gzip') return zlib.gunzipSync(buf);
    if (e === 'deflate') return zlib.inflateSync(buf);
    if (e === 'br') return zlib.brotliDecompressSync(buf);
  } catch { /* fall through to raw bytes */ }
  return buf;
}

function send(opts) {
  const { rawUrl, isHttps, host, port, path, method, hdrs, body, timeout, maxBytes, agent, stopAfter } = opts;
  return new Promise((resolve, reject) => {
    let settled = false;
    let req = null;
    // Node's `timeout` is an idle timer, so a server that drips bytes holds it
    // open forever. Enforce a wall-clock deadline on top of it.
    const hardTimer = setTimeout(() => {
      if (settled) return;
      settled = true;
      try { req && req.destroy(); } catch { /* already gone */ }
      reject(new Error('timeout'));
    }, timeout);
    hardTimer.unref?.();
    const fail = (err) => { if (!settled) { settled = true; clearTimeout(hardTimer); reject(err); } };
    const lib = isHttps ? https : http;
    // Throughput probing wants to abort once it has enough bytes to compute a
    // rate, rather than pulling the whole segment.
    let winTimer = null;
    if (stopAfter && stopAfter.ms) {
      winTimer = setTimeout(() => {
        if (settled) return;
        settled = true;
        clearTimeout(hardTimer);
        try { req && req.destroy(); } catch { /* already gone */ }
        resolve({ status: 200, headers: {}, url: rawUrl, bytes: byteCount, truncated: true });
      }, stopAfter.ms);
      winTimer.unref?.();
    }
    let byteCount = 0;
    req = lib.request({
      host, port, path, method, headers: hdrs, timeout,
      ...(agent ? { agent } : { agent: false }),
    }, (res) => {
      const chunks = [];
      let size = 0;
      res.on('data', (c) => {
        size += c.length;
        byteCount = size;
        if (stopAfter && stopAfter.bytes && size >= stopAfter.bytes) {
          if (settled) return;
          settled = true;
          clearTimeout(hardTimer); if (winTimer) clearTimeout(winTimer);
          try { req.destroy(); } catch { /* already gone */ }
          resolve({ status: res.statusCode, headers: res.headers, url: rawUrl, bytes: size, truncated: true });
          return;
        }
        if (size > maxBytes) { req.destroy(new Error('response too large')); return; }
        chunks.push(c);
      });
      res.on('end', () => {
        if (settled) return;
        settled = true;
        clearTimeout(hardTimer); if (winTimer) clearTimeout(winTimer);
        const raw = Buffer.concat(chunks);
        resolve({ status: res.statusCode, headers: res.headers, url: rawUrl, bytes: raw.length, body: decode(raw, res.headers['content-encoding']) });
      });
      res.on('error', fail);
    });
    req.on('timeout', () => req.destroy(new Error('timeout')));
    req.on('error', fail);
    if (body) req.write(body);
    req.end();
  });
}

async function once(rawUrl, opts) {
  const { timeout = 15000, headers = {}, method = 'GET', maxBytes = 8 << 20, body = null, insecure = false, stopAfter = null } = opts;
  const u = new URL(rawUrl);
  const isHttps = u.protocol === 'https:';
  const host = u.hostname;
  const port = u.port ? Number(u.port) : (isHttps ? 443 : 80);
  const path = (u.pathname || '/') + (u.search || '');
  const hdrs = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Accept': '*/*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate',
    ...headers,
  };
  if (body) hdrs['Content-Length'] = Buffer.byteLength(body);
  const useProxy = PROXY && !bypassProxy(host);
  const base = { rawUrl, isHttps, host, port, path, method, hdrs, body, timeout, maxBytes, stopAfter };

  if (isHttps && useProxy) {
    const raw = await connectTunnel(host, port, timeout);
    const socket = tls.connect({ socket: raw, servername: host, ALPNProtocols: ['http/1.1'], rejectUnauthorized: !insecure });
    await new Promise((res, rej) => {
      const t = setTimeout(() => { socket.destroy(); rej(new Error('tls timeout')); }, timeout);
      t.unref?.();
      socket.once('secureConnect', () => { clearTimeout(t); res(); });
      socket.once('error', (e) => { clearTimeout(t); rej(e); });
    });
    return send({ ...base, agent: new HttpsOneSocketAgent(socket) });
  }

  if (!isHttps && useProxy) {
    // Plain HTTP through the proxy: absolute-form request line, no tunnel needed.
    const p = new URL(PROXY);
    const auth = proxyAuthHeader(p);
    return new Promise((resolve, reject) => {
      let settled = false;
      let req = null;
      const hardTimer = setTimeout(() => {
        if (settled) return;
        settled = true;
        try { req && req.destroy(); } catch { /* already gone */ }
        reject(new Error('timeout'));
      }, timeout);
      hardTimer.unref?.();
      const fail = (err) => { if (!settled) { settled = true; clearTimeout(hardTimer); reject(err); } };
      let byteCount = 0;
      req = http.request({
        host: p.hostname, port: p.port || 80, method,
        path: rawUrl,
        headers: { ...hdrs, Host: u.host, ...(auth ? { 'Proxy-Authorization': auth } : {}) },
        timeout,
      }, (res) => {
        const chunks = []; let size = 0;
        res.on('data', (c) => {
          size += c.length; byteCount = size;
          if (stopAfter && stopAfter.bytes && size >= stopAfter.bytes) {
            if (settled) return;
            settled = true; clearTimeout(hardTimer);
            try { req.destroy(); } catch { /* already gone */ }
            resolve({ status: res.statusCode, headers: res.headers, url: rawUrl, bytes: size, truncated: true });
            return;
          }
          if (size > maxBytes) { req.destroy(new Error('response too large')); return; }
          chunks.push(c);
        });
        res.on('end', () => {
          if (settled) return; settled = true;
          clearTimeout(hardTimer);
          const raw = Buffer.concat(chunks);
          resolve({ status: res.statusCode, headers: res.headers, url: rawUrl, bytes: raw.length, body: decode(raw, res.headers['content-encoding']) });
        });
        res.on('error', fail);
      });
      req.on('timeout', () => req.destroy(new Error('timeout')));
      req.on('error', fail);
      if (body) req.write(body);
      req.end();
    });
  }

  return send(base);
}

export async function request(rawUrl, opts = {}) {
  const maxRedirects = opts.redirect ?? 5;
  let url = rawUrl;
  for (let i = 0; i <= maxRedirects; i++) {
    let res;
    try {
      res = await once(url, opts);
    } catch (e) {
      // No insecure retry on certificate errors: a cert failure is reported
      // as-is (callers record it as a failure reason). Deliberately NOT
      // retrying with rejectUnauthorized=false (the upstream code did that;
      // we removed it on review: silently disabling TLS verification to
      // "make it work" hides MITM / mis-issued certs).
      throw e;
    }
    if ([301, 302, 303, 307, 308].includes(res.status) && res.headers.location) {
      url = new URL(res.headers.location, url).toString();
      continue;
    }
    return res;
  }
  throw new Error('too many redirects');
}

export async function getText(url, opts = {}) {
  const res = await request(url, opts);
  return { status: res.status, text: res.body ? res.body.toString('utf8') : '', headers: res.headers, url: res.url, bytes: res.bytes };
}

export async function getJson(url, opts = {}) {
  const { status, text } = await getText(url, opts);
  if (status < 200 || status >= 300) throw new Error(`HTTP ${status}`);
  return JSON.parse(stripJsonNoise(text));
}

// Config files in the wild carry BOMs, // and /* */ comments, and trailing commas.
export function stripJsonNoise(text) {
  let t = text.replace(/^﻿/, '').trim();
  if (t.includes('//') || t.includes('/*')) {
    let out = '', inStr = false, esc = false;
    for (let i = 0; i < t.length; i++) {
      const c = t[i], n = t[i + 1];
      if (inStr) {
        out += c;
        if (esc) esc = false;
        else if (c === '\\') esc = true;
        else if (c === '"') inStr = false;
        continue;
      }
      if (c === '"') { inStr = true; out += c; continue; }
      if (c === '/' && n === '/') { while (i < t.length && t[i] !== '\n') i++; out += '\n'; continue; }
      if (c === '/' && n === '*') { i += 2; while (i < t.length && !(t[i] === '*' && t[i + 1] === '/')) i++; i++; continue; }
      out += c;
    }
    t = out;
  }
  t = t.replace(/,(\s*[}\]])/g, '$1');
  return t;
}

export async function mapLimit(items, limit, fn) {
  const results = new Array(items.length);
  let idx = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (true) {
      const i = idx++;
      if (i >= items.length) return;
      try { results[i] = await fn(items[i], i); }
      catch (e) { results[i] = { __error: e.message }; }
    }
  });
  await Promise.all(workers);
  return results;
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));