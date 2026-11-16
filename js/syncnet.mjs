// ---------------------------------------------------------------------------
// 来源: 另一 AI 为同一用户做的 tvbox-hub 项目 (lib/syncnet.mjs, MIT).
// 用户 2026-11-13 决定把评审中值得借鉴的部分并入本项目, 本文件为原样复制
// (仅 http.mjs 去掉了证书失败时不验证重试的逻辑, 见该文件内注释)。
// 上游代码由对方 AI 编写, 本项目仅作复用, 不宣称原创。
// ---------------------------------------------------------------------------
// Blocking HTTP for @js: rules. Legado's `java.ajax(url)` is synchronous, but
// Node's network API is not, so a worker thread does the awaiting while the
// caller parks in Atomics.wait. One worker per request keeps it simple and lets
// a hung socket be abandoned without wedging the whole crawl.
import { Worker, MessageChannel, receiveMessageOnPort } from 'node:worker_threads';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { decodeBody } from './charset.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WORKER = path.join(HERE, 'syncnet-worker.mjs');

let spawnCount = 0;
export function syncNetStats() {
  return { spawned: spawnCount };
}

// Returns { ok, status, text, url } and never throws, so callers can treat a
// failed fetch as an empty body the way Legado does.
export function syncGet(url, opts = {}) {
  const { port1, port2 } = new MessageChannel();
  const sab = new SharedArrayBuffer(4);
  const signal = new Int32Array(sab);
  // port1 is a MessagePort, so it has to be listed for transfer; without that
  // Node throws DataCloneError and every java.ajax() call fails.
  const worker = new Worker(WORKER, { workerData: { port: port1, sab }, transferList: [port1] });
  spawnCount++;
  let settled = false;
  const cleanup = () => {
    if (settled) return;
    settled = true;
    try { port1.close(); } catch { /* already closed */ }
    worker.terminate().catch(() => {});
  };
  const budget = Number(opts.timeout || 20000);
  const timer = setTimeout(cleanup, budget + 2000);
  timer.unref?.();
  try {
    worker.postMessage({ url, opts });
    while (Atomics.load(signal, 0) === 0) {
      if (Atomics.wait(signal, 0, 0, budget + 2000) === 'timed-out') break;
    }
    const msg = receiveMessageOnPort(port2);
    clearTimeout(timer);
    cleanup();
    if (!msg || !msg.message) return { ok: false, status: 0, text: '', error: 'timeout' };
    const r = msg.message;
    if (!r.ok) return { ok: false, status: 0, text: '', error: r.error };
    return { ok: true, status: r.status, text: decodeBody(r.bytes, r.contentType, opts.charset), url: r.url, headers: r.headers || {} };
  } catch (e) {
    clearTimeout(timer);
    cleanup();
    return { ok: false, status: 0, text: '', error: String((e && e.message) || e) };
  }
}

// Legado's java.ajax returns the body string, or "" when the request fails.
export function javaAjax(url, opts = {}) {
  const r = syncGet(url, opts);
  return r.ok ? r.text : '';
}

// Legado's java.get(url, opts) is an HTTP request that returns a response
// object with .body() and .headers(name) — not a key/value cache read. The 自写
// sources all use it as `java.get(su).headers('Location')` to follow a redirect.
export function javaGetResponse(url, opts = {}) {
  const r = syncGet(url, opts);
  const h = r.headers || {};
  return {
    body: () => (r.ok ? r.text : ''),
    string: () => (r.ok ? r.text : ''),
    bytes: () => new Uint8Array(),
    statusCode: () => r.status || 0,
    // Legado's Response.headers(key) returns String[]; sources index it as
    // .headers('Location')[0].
    headers: (name) => {
      if (name == null) return Object.entries(h).map(([k, v]) => `${k}: ${v}`).join('\n');
      const v = h[String(name).toLowerCase()];
      if (v == null) return [];
      return Array.isArray(v) ? v : [String(v)];
    },
    header: (name) => {
      const v = h[String(name).toLowerCase()];
      if (v == null) return '';
      return Array.isArray(v) ? String(v[0]) : String(v);
    },
    url: () => r.url || url,
  };
}
