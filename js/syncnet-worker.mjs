// ---------------------------------------------------------------------------
// 来源: 另一 AI 为同一用户做的 tvbox-hub 项目 (lib/syncnet-worker.mjs, MIT).
// 用户 2026-11-13 决定把评审中值得借鉴的部分并入本项目, 本文件为原样复制
// (仅 http.mjs 去掉了证书失败时不验证重试的逻辑, 见该文件内注释)。
// 上游代码由对方 AI 编写, 本项目仅作复用, 不宣称原创。
// ---------------------------------------------------------------------------
// Worker side of the synchronous fetch bridge. Runs on its own thread so the
// main thread can block in Atomics.wait while this awaits the network.
//
// Returns raw bytes rather than a decoded string: a lot of the sites these
// sources target are GBK, and the decoder has to run after the charset is
// sniffed from the headers or the meta tag.
import { parentPort, workerData } from 'node:worker_threads';
import { request } from './http.mjs';

const { port, sab } = workerData;
const signal = new Int32Array(sab);

parentPort.on('message', async (msg) => {
  let out;
  try {
    const res = await request(msg.url, msg.opts || {});
    const body = res.body || Buffer.alloc(0);
    // Copy out of the Buffer's pool: the pool is not transferable and a
    // detached view here would corrupt neighbouring reads.
    out = {
      ok: true,
      status: res.status,
      contentType: res.headers['content-type'] || '',
      // java.get(url).headers('Location') reads response headers, so they
      // have to survive the trip back to the main thread.
      headers: res.headers,
      bytes: new Uint8Array(body.buffer.slice(body.byteOffset, body.byteOffset + body.byteLength)),
      url: res.url,
    };
  } catch (e) {
    out = { ok: false, error: String((e && e.message) || e) };
  }
  // Hand the payload over the port, then wake the blocked main thread. The
  // order matters: the message must be queued before the notify.
  port.postMessage(out);
  Atomics.store(signal, 0, 1);
  Atomics.notify(signal, 0);
});
