// ---------------------------------------------------------------------------
// 来源: 另一 AI 为同一用户做的 tvbox-hub 项目 (lib/validate.mjs, MIT).
// 本文件是 validate.mjs 的 FORK (宽松版, 供本项目四步校验驱动使用)。
// 与原版的三处不同 (用户定: 借方法不借标准):
//   (1) toc-lagging 只记信号 (out.tocLagging=true, out.chapterRatio), 不淘汰;
//   (2) 正文检查首章失败时 (太短/占位/非散文/内容不符/截断) 再试第 2 章一次
//       (首章常为序言/楔子, 易误判), 两章都失败才判内容不符;
//   (3) proofs 除主角名外也含书名本身 (正文含书名即算内容对得上)。
// 其余流程、阈值 (正文>=300字、汉字比>=0.5、章节比 0.8) 与原版一致。
// 上游代码由对方 AI 编写, 本项目仅作复用, 不宣称原创。
// ---------------------------------------------------------------------------
// Live validation of a book source.
//
// This is the piece the previous version was missing entirely: previously a
// source passed if its rule *fields* existed. Here the source has to actually
// run — search a real title, open the detail page, read the chapter list, and
// pull a chapter body — before it is kept.
//
// The four assertions the user asked for map onto the steps below:
//   1 搜得到     — search returns a book whose title matches
//   2 能打开     — detail + toc + one chapter fetch real content
//   3 跟进及时   — the source's chapter count is within tolerance of the truth
//   4 内容正确   — the chapter text contains this book's protagonist or title
import { request } from './http.mjs';
import { decodeBody } from './charset.mjs';
import { evalRuleString, evalRuleList, expandTemplate } from './rules.mjs';
import { detectShield, looksLikePlaceholder, hasNextPageHint, hanRatio } from './filters.mjs';

// A TOC holding under this share of the real book's chapters is stale.
const MIN_CHAPTER_RATIO = 0.8;

const UA_FALLBACK = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

function normalizeUrl(raw, base) {
  const s = String(raw || '').trim();
  if (!s) return '';
  if (/^https?:\/\//i.test(s)) return s;
  try { return new URL(s, base).toString(); } catch { return ''; }
}

// Legado (Gson) accepts single-quoted keys, bare keys and trailing commas, so
// many published sources carry `{method:'post','Cookie':'x'}`. JSON.parse throws
// on all three and the whole rule then leaks into the URL; normalise first.
function lenientJson(src) {
  const s = String(src);
  const n = s.length;
  let out = '';
  let i = 0;
  while (i < n) {
    const c = s[i];
    if (c === '"' || c === "'") {
      const q = c;
      let j = i + 1;
      let buf = '';
      while (j < n) {
        const d = s[j];
        if (d === '\\') { buf += d + (s[j + 1] ?? ''); j += 2; continue; }
        if (d === q) break;
        if (d === '"') { buf += '\\"'; j++; continue; }
        buf += d; j++;
      }
      out += '"' + buf + '"';
      i = j + 1;
      continue;
    }
    if (c === '{' || c === ',') {
      out += c;
      let j = i + 1;
      while (j < n && /\s/.test(s[j])) j++;
      const m = /^[A-Za-z_$][\w$-]*/.exec(s.slice(j));
      if (m) {
        let k = j + m[0].length;
        while (k < n && /\s/.test(s[k])) k++;
        if (s[k] === ':') {
          if (c === ',') out += ' ';
          out += '"' + m[0] + '"';
          i = j + m[0].length;
          continue;
        }
      }
      i = j;
      continue;
    }
    if (c === '}') {
      out = out.replace(/,\s*$/, '');
      out += c; i++; continue;
    }
    out += c; i++;
  }
  return out;
}

function parseJsonLoose(txt) {
  const t = String(txt || '').trim();
  if (!t) return null;
  try { return JSON.parse(t); } catch { /* try lenient */ }
  try { return JSON.parse(lenientJson(t)); } catch { return null; }
}

// `url,{json}` is Legado's request-options syntax; the JSON carries method,
// body, charset and headers.
function parseUrlRule(raw, base, key, page) {
  let url = String(raw == null ? '' : raw);
  let opts = {};
  const mComma = /,\s*\{/.exec(url);
  const comma = mComma ? mComma.index : -1;
  if (comma > 0) {
    const o = parseJsonLoose(url.slice(comma + 1));
    if (o && typeof o === 'object') { opts = o; url = url.slice(0, comma); }
  }
  const encKey = encodeURIComponent(key);
  url = expandTemplate(url, { key: encKey, page, result: '' })
    .replace(/\{\{key\}\}/g, encKey)
    .replace(/\{\{searchKey\}\}/g, encKey)
    .replace(/\{\{page\}\}/g, String(page));
  url = url.trim();
  if (opts.body) {
    opts.body = String(opts.body)
      .replace(/\{\{key\}\}/g, key)
      .replace(/\{\{searchKey\}\}/g, key)
      .replace(/\{\{page\}\}/g, String(page));
  }
  return { url: normalizeUrl(url, base), opts };
}

async function fetchPage(url, { method = 'GET', body, headers = {}, charset = '', timeout = 20000 } = {}) {
  const res = await request(url, { timeout, maxBytes: 6 << 20, method, body, headers });
  const bytes = res.body;
  const text = bytes && bytes.length
    ? decodeBody(new Uint8Array(bytes), res.headers['content-type'] || '', charset)
    : '';
  return { status: res.status, text, finalUrl: res.url, headers: res.headers };
}

// Legado's searchUrl can itself be an @js: rule that resolves to a URL; the
// simple case is a literal with {{key}} substituted.
function resolveSearchUrl(src, base, key, page) {
  const raw = String(src.searchUrl || '');
  if (/^\s*@js:|^\s*<js>/.test(raw)) {
    const r = evalRuleString(raw, { baseUrl: base, key, page, result: '', bookSourceName: src.bookSourceName });
    // The script usually builds the URL itself, but may still hand back
    // `{{key}}`/`{{page}}` placeholders or a `url,{opts}` suffix.
    const filled = String(r || '')
      .replace(/\{\{\s*key\s*\}\}/g, encodeURIComponent(key))
      .replace(/\{\{\s*searchKey\s*\}\}/g, encodeURIComponent(key))
      .replace(/\{\{\s*page\s*\}\}/g, String(page));
    return parseUrlRule(filled, base, key, page);
  }
  return parseUrlRule(raw, base, key, page);
}

// Exposed for diagnostics: does this source even resolve a request URL for a key?
export function resolveSearchUrlProbe(src, base, key, page = 1) {
  return resolveSearchUrl(src, base, key, page);
}

function titleMatches(bookName, key) {
  const a = String(bookName || '').replace(/\s+/g, '');
  const b = String(key || '').replace(/\s+/g, '');
  if (!a || !b) return false;
  if (a.includes(b) || b.includes(a)) return true;
  // Sources often decorate titles (【完结】, 作者后缀); compare the longest
  // common run of characters instead of requiring a prefix.
  let best = 0;
  for (let i = 0; i < b.length; i++) {
    for (let j = i + 2; j <= b.length; j++) {
      if (a.includes(b.slice(i, j))) best = Math.max(best, j - i);
    }
  }
  return best >= Math.max(2, Math.ceil(b.length * 0.6));
}

// A book name that is really site chrome ("首页", "登录") means the rule matched
// the wrong node.
const CHROME_RE = /^(首页|主页|登录|注册|搜索|目录|书架|全部|排行|分类|完本|更多|下一页|上一页|返回)$/;

export async function validateSource(src, { book, concurrencyHint = 0 } = {}) {
  const base = String(src.bookSourceUrl || '').split('##')[0];
  const key = book.title;
  const out = {
    name: src.bookSourceName,
    host: (() => { try { return new URL(base).hostname; } catch { return base; } })(),
    steps: {},
    ok: false,
    reason: '',
  };
  const baseCtx = {
    baseUrl: base, key, page: 1,
    bookSourceUrl: src.bookSourceUrl,
    bookSourceName: src.bookSourceName,
    bookSourceGroup: src.bookSourceGroup,
    bookSourceComment: src.bookSourceComment,
  };

  // ---- 1. search -----------------------------------------------------------
  let html;
  try {
    const { url, opts } = resolveSearchUrl(src, base, key, 1);
    if (!url) { out.reason = 'no-search-url'; return out; }
    const r = await fetchPage(url, { method: (opts.method || 'GET').toUpperCase(), body: opts.body, headers: opts.headers || {}, charset: opts.charset || '' });
    const shield = detectShield({ status: r.status, headers: r.headers, body: r.text });
    if (shield.shielded) { out.reason = 'shield:' + shield.kind; return out; }
    if (r.status !== 200) { out.reason = 'search-http-' + r.status; return out; }
    html = r.text;
  } catch (e) { out.reason = 'search-error'; out.detail = String(e.message || e); return out; }
  out.steps.search = html.length;

  const sctx = { ...baseCtx, input: html };
  let list;
  try { list = evalRuleList(src.ruleSearch.bookList, sctx); } catch { out.reason = 'bookList-throw'; return out; }
  out.steps.bookList = list.length;
  if (!list.length) { out.reason = 'no-results'; return out; }

  // Pick the entry whose title actually matches the searched book.
  let hit = null;
  for (const node of list.slice(0, 30)) {
    const ictx = { ...sctx, input: node };
    let name = '';
    try { name = evalRuleString(src.ruleSearch.name, ictx); } catch { continue; }
    if (!name || CHROME_RE.test(name.trim())) continue;
    if (titleMatches(name, key)) {
      hit = {
        node,
        name,
        author: safeEval(src.ruleSearch.author, ictx),
        bookUrl: safeEval(src.ruleSearch.bookUrl, ictx),
      };
      break;
    }
  }
  if (!hit) { out.reason = 'title-mismatch'; out.sampleName = safeEval(src.ruleSearch.name, { ...sctx, input: list[0] }); return out; }
  out.book = { name: hit.name, author: hit.author };
  if (!hit.bookUrl) { out.reason = 'no-bookUrl'; return out; }

  const bookUrl = normalizeUrl(hit.bookUrl, base);
  if (!bookUrl) { out.reason = 'bad-bookUrl'; return out; }
  out.bookUrl = bookUrl;

  // ---- 2. detail + toc -----------------------------------------------------
  let detail;
  try {
    const r = await fetchPage(bookUrl);
    if (r.status !== 200) { out.reason = 'detail-http-' + r.status; return out; }
    detail = r.text;
  } catch { out.reason = 'detail-error'; return out; }
  out.steps.detail = detail.length;

  const dctx = { ...baseCtx, input: detail, result: detail, book: out.book };
  let tocUrl = bookUrl;
  const tocRule = src.ruleBookInfo && (src.ruleBookInfo.tocUrl || src.ruleBookInfo.toc_url);
  if (tocRule) {
    const t = safeEval(tocRule, dctx);
    const abs = normalizeUrl(t, bookUrl);
    if (abs) tocUrl = abs;
  }

  let tocHtml = detail;
  if (tocUrl !== bookUrl) {
    try {
      const r = await fetchPage(tocUrl);
      if (r.status !== 200) { out.reason = 'toc-http-' + r.status; return out; }
      tocHtml = r.text;
    } catch { out.reason = 'toc-error'; return out; }
  }

  const tctx = { ...dctx, input: tocHtml, result: tocHtml };
  let chapters;
  try { chapters = evalRuleList(src.ruleToc.chapterList, tctx); } catch { out.reason = 'toc-throw'; return out; }
  out.steps.chapters = chapters.length;
  if (!chapters.length) { out.reason = 'no-chapters'; return out; }

  // ---- 3. chapter follow-up ------------------------------------------------
  // The user's third complaint is a TOC that lags the real book: the source
  // opens fine and serves the right text, but stops 100 chapters short. A
  // source is only usable if its TOC is close to the truth. Serials drift, so
  // this is a tolerance band, not equality. A source that declares its own
  // nextTocUrl is exempt: we read one TOC page, it would paginate on its own.
  const paginatesToc = !!(src.ruleBookInfo && (src.ruleBookInfo.nextTocUrl || src.ruleBookInfo.nextTocUrl));
  if (book.truthChapters && chapters.length) {
    const ratio = chapters.length / book.truthChapters;
    out.chapterRatio = Number(ratio.toFixed(2));
    if (!paginatesToc && book.truthChapters >= 20 && ratio < MIN_CHAPTER_RATIO) {
      // FORK: 只记信号, 不淘汰 (借方法不借标准: 对方 0.8 线太严, 且真值章节数
      // 为约数, 误杀代价高)。继续跑正文检查, 由调用方决定是否采纳。
      out.tocLagging = true;
    }
  }

  // ---- 4. content + correctness -------------------------------------------
  // FORK: 正文检查封装成 tryContentChapter, 首章失败时再试第 2 章一次。
  // 只有正文质量类失败 (太短/占位/非散文/内容不符/截断) 才触发二次机会;
  // 站点级失败 (反爬/HTTP/异常) 不重试, 直接返回。
  const CONTENT_FAILS = new Set(['content-too-short', 'content-placeholder',
    'content-not-prose', 'content-mismatch', 'content-truncated']);
  const proofs = [...(book.proofs || []), book.title].filter(Boolean);
  async function tryContentChapter(node, idx) {
    const cctx = { ...tctx, input: node };
    const chName = safeEval(src.ruleToc.chapterName, cctx);
    const chUrl = normalizeUrl(safeEval(src.ruleToc.chapterUrl, cctx), tocUrl);
    if (!chUrl) return { ok: false, reason: 'no-chapterUrl' };
    let content;
    try {
      const r = await fetchPage(chUrl);
      const shield = detectShield({ status: r.status, headers: r.headers, body: r.text });
      if (shield.shielded) return { ok: false, reason: 'content-shield:' + shield.kind };
      if (r.status !== 200) return { ok: false, reason: 'content-http-' + r.status };
      const cctx2 = { ...baseCtx, input: r.text, result: r.text, book: out.book, chapter: { title: chName, url: chUrl } };
      content = safeEval(src.ruleContent.content, cctx2);
    } catch { return { ok: false, reason: 'content-error' }; }
    // Some sources return HTML in the content rule; the text is what matters.
    const plain = String(content || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
    const hr = Number(hanRatio(plain).toFixed(2));
    if (!plain || plain.length < 300) return { ok: false, reason: 'content-too-short', plain, hanRatio: hr, chName };
    if (looksLikePlaceholder(plain)) return { ok: false, reason: 'content-placeholder', plain, hanRatio: hr, chName };
    if (hr < 0.5) return { ok: false, reason: 'content-not-prose', plain, hanRatio: hr, chName };
    // This is assertion 4: the text must be *this* book, not a swapped one.
    // FORK: proofs 除主角名外也含书名本身。
    const found = proofs.filter((p) => p && plain.includes(p));
    if (proofs.length && !found.length) return { ok: false, reason: 'content-mismatch', plain, hanRatio: hr, proofHits: found, chName };
    // A chapter that says "next page" but has no rule to follow it is the
    // "content incomplete" complaint.
    if (src.ruleContent.nextContentUrl) return { ok: true, plain, hanRatio: hr, proofHits: found, chName, hasNextRule: true };
    if (hasNextPageHint(plain) && plain.length < 1500) return { ok: false, reason: 'content-truncated', plain, hanRatio: hr, proofHits: found, chName };
    return { ok: true, plain, hanRatio: hr, proofHits: found, chName };
  }

  let attempt = await tryContentChapter(chapters[0], 0);
  out.chapterUsed = 0;
  if (!attempt.ok && CONTENT_FAILS.has(attempt.reason) && chapters.length > 1) {
    const second = await tryContentChapter(chapters[1], 1);
    out.secondChapterReason = attempt.reason;
    if (second.ok || !CONTENT_FAILS.has(second.reason)) attempt = second;
    else { attempt = second; out.chapterUsed = 1; }
    if (attempt.ok) out.chapterUsed = 1;
  }
  if (!attempt.ok) { out.reason = attempt.reason; return out; }
  out.firstChapter = attempt.chName;
  out.steps.content = attempt.plain.length;
  out.hanRatio = attempt.hanRatio;
  out.proofHits = attempt.proofHits || [];
  if (attempt.hasNextRule) out.hasNextRule = true;

  out.ok = true;
  return out;
}

function safeEval(rule, ctx) {
  if (!rule) return '';
  try { return evalRuleString(rule, ctx); } catch { return ''; }
}
