// 书源四步校验驱动 (Node).
//
// 由 Python fetcher.py 调用: `node js/validate_books.js <job.json> <out.json>`.
// job.json: { proxy, concurrency, deadlineEpochMs,
//             domains: [ { domain, mirrors: [source,...], books: [{title,author,truthChapters,proofs}] } ] }
//   - mirrors: 同一注册域的书源 (最多 3 个, searchUrl 优先, Python 侧已排好序)。
//   - books: 该域要测的书 (一般源用混合 5 本, 女频源用女频 5 本)。
// out.json: { results: [ { domain, representativeUrl, ok, blocking, reason,
//                          perBook: { title: {outcome, reason, mirror, chapterRatio,
//                                              tocLagging, contentLen, hanRatio, proofHits} },
//                          passCount, testedCount, skippedCount, contentBad } ],
//             stats: {...}, startedAt, finishedAt }
//
// 判定口径 (与 Python 侧 search_pass 语义一致):
//   - 每本书 outcome: pass (四步全过; toc-lagging 只记信号, 仍算过) /
//     fail (源站侧问题: 无结果/标题不符/无章节/正文不符等) /
//     untested (环境/规则引擎问题: 反爬/HTTP异常/无搜索URL/规则抛错等, 不判 0 分)。
//   - contentBad: 正向内容失败 (content-too-short/placeholder/not-prose/
//     mismatch/truncated) 的本书数; Python 侧 contentBad>=2 的源不进最终 150。
//   - 反爬 (reason 以 'shield:' 开头) 是站点属性: 一旦命中, 整个域停止
//     (不再换书、不再试下一镜像), 记 blocking=true。
//   - hostAlive 门: 先 GET 根路径 10 秒, 只有 502/异常算死; 死站整域未测试。
//
// 注意: 本驱动复用 js/validate_relaxed.mjs (validate.mjs 的宽松 fork)。
// 代理: job.proxy 写入 process.env.HTTPS_PROXY 后再动态 import http 依赖,
// 因为 http.mjs 在模块加载时读取代理环境变量。
import fs from 'node:fs';

const jobPath = process.argv[2];
const outPath = process.argv[3];
if (!jobPath || !outPath) {
  console.error('usage: node validate_books.js <job.json> <out.json>');
  process.exit(2);
}
const job = JSON.parse(fs.readFileSync(jobPath, 'utf8'));
if (job.proxy) {
  process.env.HTTPS_PROXY = job.proxy;
  process.env.HTTP_PROXY = job.proxy;
}
const { validateSource } = await import('./validate_relaxed.mjs');
const { request, mapLimit } = await import('./http.mjs');

const CONCURRENCY = job.concurrency || 24;
const DEADLINE = job.deadlineEpochMs || 0;
const CONTENT_FAIL_RE = /^content-(too-short|placeholder|not-prose|mismatch|truncated)$/;
const UNTESTED_REASONS = new Set([
  'no-search-url', 'search-error', 'bookList-throw',
  'detail-error', 'toc-error', 'toc-throw', 'content-error',
  'driver-throw', 'no-attempt',
]);
function isHttpFail(reason) {
  return /^(search|detail|toc|content)-http-\d+$/.test(reason || '');
}
function isShield(reason) {
  return typeof reason === 'string' && reason.startsWith('shield:');
}
function classify(reason) {
  // validateSource ok=true 时不走这里。reason 为空表示未知失败, 按 fail 计。
  if (!reason) return 'fail';
  if (isShield(reason) || reason.startsWith('content-shield:')) return 'untested';
  if (UNTESTED_REASONS.has(reason) || isHttpFail(reason)) return 'untested';
  return 'fail';
}

// 注册域 (照抄规格 §2.6): IP 直返; 多段 TLD 命中取末三段, 否则末两段。
const MULTI_PART_TLD = /^(com|net|org|gov|edu|co|ac|or|ne|go|in|me|tv|cc|info|biz)\.(cn|hk|tw|mo|uk|jp|kr|sg|us|io|me|tv)$/i;
export function registrableDomain(hostname) {
  const h = String(hostname || '').toLowerCase();
  if (/^\d+\.\d+\.\d+\.\d+$/.test(h)) return h;
  const parts = h.split('.').filter(Boolean);
  if (parts.length <= 2) return h;
  const last2 = parts.slice(-2).join('.');
  if (MULTI_PART_TLD.test(last2)) return parts.slice(-3).join('.');
  return last2;
}

async function hostAlive(url) {
  let base = '';
  try { base = String(url || '').split('##')[0]; } catch { return false; }
  let root = '';
  try {
    const u = new URL(base);
    root = `${u.protocol}//${u.host}/`;
  } catch { return false; }
  // 出口代理偶发 502/504/超时: 异常时重试一次 (间隔 1 秒), 仍失败才判死站。
  // 502 本身也重试一次 (代理层 502 常为瞬时), 两次 502 才判死。
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const res = await request(root, { timeout: 10000, maxBytes: 256 << 10 });
      if (res.status > 0 && res.status !== 502) return true;
      if (attempt === 0) await new Promise((r) => setTimeout(r, 1000));
    } catch {
      if (attempt === 0) await new Promise((r) => setTimeout(r, 1000));
      else return false;
    }
  }
  return false;
}

async function validateDomain(dom) {
  const mirrors = dom.mirrors || [];
  const books = dom.books || [];
  const out = {
    domain: dom.domain,
    representativeUrl: mirrors[0] ? mirrors[0].bookSourceUrl : '',
    ok: false, blocking: false, reason: '',
    perBook: {}, passCount: 0, testedCount: 0, skippedCount: 0, contentBad: 0,
    mirrorsTried: 0, hostAlive: null,
  };
  if (!mirrors.length || !books.length) { out.reason = 'no-attempt'; return out; }

  out.hostAlive = await hostAlive(mirrors[0].bookSourceUrl);
  if (!out.hostAlive) {
    out.reason = 'host-dead';
    for (const b of books) {
      out.perBook[b.title] = { outcome: 'untested', reason: 'host-dead', mirror: '' };
      out.skippedCount += 1;
    }
    return out;
  }

  let lastReason = '';
  const mirrorPass = new Map(); // 镜像 URL -> 通过本数 (用于选代表源)
  for (const book of books) {
    let bookRes = null;
    for (const mirror of mirrors) {
      out.mirrorsTried += 1;
      let r;
      try {
        r = await validateSource(mirror, { book });
      } catch (e) {
        r = { ok: false, reason: 'driver-throw', detail: String((e && e.message) || e) };
      }
      if (r.ok) {
        mirrorPass.set(mirror.bookSourceUrl, (mirrorPass.get(mirror.bookSourceUrl) || 0) + 1);
        bookRes = {
          outcome: 'pass', reason: '', mirror: mirror.bookSourceUrl,
          chapterRatio: r.chapterRatio ?? null, tocLagging: !!r.tocLagging,
          contentLen: r.steps ? r.steps.content : null,
          hanRatio: r.hanRatio ?? null, proofHits: r.proofHits || [],
          chapterUsed: r.chapterUsed ?? 0,
        };
        break;
      }
      const reason = r.reason || '';
      lastReason = reason;
      if (isShield(reason)) {
        // 反爬是站点属性: 整个域停止, 不再换书、不再试下一镜像。
        out.blocking = true;
        bookRes = { outcome: 'untested', reason, mirror: mirror.bookSourceUrl };
        out.perBook[book.title] = bookRes;
        out.skippedCount += 1;
        out.reason = reason;
        return out;
      }
      if (classify(reason) === 'untested') {
        // 环境/规则问题: 换下一镜像再试同一本书。
        bookRes = { outcome: 'untested', reason, mirror: mirror.bookSourceUrl };
        continue;
      }
      // 源站侧失败 (无结果/标题不符/正文不符等): 换下一镜像再试同一本书,
      // 若镜像都失败, 取最后一次的原因。
      bookRes = {
        outcome: 'fail', reason, mirror: mirror.bookSourceUrl,
        chapterRatio: r.chapterRatio ?? null, tocLagging: !!r.tocLagging,
        contentLen: r.steps ? r.steps.content : null,
        hanRatio: r.hanRatio ?? null, proofHits: r.proofHits || [],
      };
    }
    if (!bookRes) bookRes = { outcome: 'untested', reason: 'no-attempt', mirror: '' };
    out.perBook[book.title] = bookRes;
    if (bookRes.outcome === 'pass') { out.passCount += 1; out.testedCount += 1; }
    else if (bookRes.outcome === 'fail') {
      out.testedCount += 1;
      if (CONTENT_FAIL_RE.test(bookRes.reason || '')) out.contentBad += 1;
    } else out.skippedCount += 1;
  }
  out.ok = out.passCount >= 3;
  out.reason = lastReason;
  // 代表源: 通过本数最多的镜像 (平局取镜像顺序在前的); 无一通过时取首镜像。
  if (mirrorPass.size) {
    let best = mirrors[0].bookSourceUrl, bestN = -1;
    for (const m of mirrors) {
      const n = mirrorPass.get(m.bookSourceUrl) || 0;
      if (n > bestN) { best = m.bookSourceUrl; bestN = n; }
    }
    out.representativeUrl = best;
    out.representativePasses = bestN;
  }
  return out;
}

const startedAt = new Date().toISOString();
const results = [];
const domains = job.domains || [];
// 顺序跑 mapLimit: 每个域内部串行 (同一站不并行轰), 域之间并发。
// 超时预算: 到点后不再开新域, 已跑完的如实写出。
let stoppedByDeadline = false;
await mapLimit(domains, CONCURRENCY, async (dom) => {
  if (stoppedByDeadline) {
    results.push({ domain: dom.domain, representativeUrl: (dom.mirrors[0] || {}).bookSourceUrl || '',
      ok: false, blocking: false, reason: 'skipped-deadline', perBook: {},
      passCount: 0, testedCount: 0, skippedCount: 0, contentBad: 0, mirrorsTried: 0, hostAlive: null });
    return;
  }
  if (DEADLINE && Date.now() > DEADLINE) {
    stoppedByDeadline = true;
    results.push({ domain: dom.domain, representativeUrl: (dom.mirrors[0] || {}).bookSourceUrl || '',
      ok: false, blocking: false, reason: 'skipped-deadline', perBook: {},
      passCount: 0, testedCount: 0, skippedCount: 0, contentBad: 0, mirrorsTried: 0, hostAlive: null });
    return;
  }
  const r = await validateDomain(dom);
  results.push(r);
});
// mapLimit 不保证顺序: 按输入域顺序排回, 便于 Python 侧对照。
const order = new Map(domains.map((d, i) => [d.domain, i]));
results.sort((a, b) => (order.get(a.domain) ?? 0) - (order.get(b.domain) ?? 0));
const finishedAt = new Date().toISOString();
const stats = {
  domains: domains.length,
  domainsDone: results.filter((r) => r.reason !== 'skipped-deadline').length,
  domainsOk: results.filter((r) => r.ok).length,
  blocking: results.filter((r) => r.blocking).length,
  hostDead: results.filter((r) => r.reason === 'host-dead').length,
  stoppedByDeadline,
};
fs.writeFileSync(outPath, JSON.stringify({ results, stats, startedAt, finishedAt }, null, 2));
console.log(JSON.stringify({ ...stats, startedAt, finishedAt }));
