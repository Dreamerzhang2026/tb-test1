// ---------------------------------------------------------------------------
// 来源: 另一 AI 为同一用户做的 tvbox-hub 项目 (lib/jsrule.mjs, MIT).
// 用户 2026-11-13 决定把评审中值得借鉴的部分并入本项目, 本文件为原样复制
// (仅 http.mjs 去掉了证书失败时不验证重试的逻辑, 见该文件内注释)。
// 上游代码由对方 AI 编写, 本项目仅作复用, 不宣称原创。
// ---------------------------------------------------------------------------
// Sandbox for Legado `@js:` rules.
//
// Legado runs these under Rhino with a set of host bindings; source authors rely
// on `result` (the previous step's value), `baseUrl`, and a `java.*`/`source.*`
// API surface. Node has no Rhino, so the script body is wrapped in a function
// whose last parameter is `result`, seeded from the previous step and returned
// when the body does not return anything itself. That covers both idioms seen in
// the wild: `result = x` followed by falling off the end, and an explicit
// `return x`.
import vm from 'node:vm';
import crypto from 'node:crypto';
import { javaAjax, javaGetResponse } from './syncnet.mjs';
import { parseHtml, text as htmlText, innerHtml, attr, select, selectFirst, serialize, serialize as outerHtml } from './html.mjs';
import { splitTop } from './rules.mjs';

const MAX_SCRIPT_MS = 8000;
const MAX_AJAX_PER_SCRIPT = 12;

function b64encode(s) { return Buffer.from(String(s == null ? '' : s), 'utf8').toString('base64'); }
function b64decode(s) {
  try { return Buffer.from(String(s), 'base64').toString('utf8'); } catch { return ''; }
}

// Legado's t2s/s2t need a conversion table; without one these return the input
// unchanged, which keeps scripts that only call them for cosmetics working.
const identity = (s) => String(s == null ? '' : s);

function md5(s) { return crypto.createHash('md5').update(String(s), 'utf8').digest('hex'); }
function sha1(s) { return crypto.createHash('sha1').update(String(s), 'utf8').digest('hex'); }
function sha256(s) { return crypto.createHash('sha256').update(String(s), 'utf8').digest('hex'); }

function timeFormat(ts, fmt) {
  const d = new Date(Number(ts) || Date.now());
  const p = (n) => String(n).padStart(2, '0');
  return String(fmt || 'yyyy-MM-dd HH:mm')
    .replace(/yyyy/g, d.getFullYear())
    .replace(/MM/g, p(d.getMonth() + 1))
    .replace(/dd/g, p(d.getDate()))
    .replace(/HH/g, p(d.getHours()))
    .replace(/mm/g, p(d.getMinutes()))
    .replace(/ss/g, p(d.getSeconds()));
}

const CN_NUM = { 零: 0, 一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 十: 10, 百: 100, 千: 1000, 万: 10000 };

// Legado's toNumChapter turns "第123章" / "番外" into a sortable number.
function toNumChapter(s) {
  const str = String(s == null ? '' : s);
  const m = /(\d+)/.exec(str);
  if (m) return Number(m[1]);
  let total = 0, section = 0, num = 0;
  for (const ch of str) {
    if (CN_NUM[ch] === undefined) continue;
    if (CN_NUM[ch] >= 10) { section = (section || 1) * CN_NUM[ch]; total += section; section = 0; }
    else num = CN_NUM[ch];
  }
  return total + section + num;
}

function makeJava(ctx, budget) {
  const jar = ctx.cookieJar;
  const store = ctx.store;
  // java.post returns a response object, not a string: sources call
  // java.post(url, body).headers('Location') to follow a form redirect.
  const post = (url, body, opts) => {
    if (budget.ajax++ > MAX_AJAX_PER_SCRIPT) return javaGetResponse('', {});
    const o = { method: 'POST', body: String(body == null ? '' : body), ...(opts && typeof opts === 'object' ? opts : {}) };
    try { return javaGetResponse(String(url), o); } catch { return javaGetResponse('', {}); }
  };
  const ajax = (url, opts) => {
    if (budget.ajax++ > MAX_AJAX_PER_SCRIPT) return '';
    try { return javaAjax(String(url), opts && typeof opts === 'object' ? opts : {}); }
    catch { return ''; }
  };
  return {
    ajax,
    connect: ajax,
    post,
    // A simple per-source string cache, distinct from the response-object get().
    put: (key, val) => { store.set(String(key), val); return ''; },
    // java.get is overloaded in the wild: java.get(someUrl, {}) is an HTTP
    // request returning a response object, while java.get('key') is a cache read
    // paired with java.put (476 of the pooled sources do the latter). A second
    // argument, or a URL-looking first argument, selects the HTTP form.
    get: (a, opts) => {
      const isUrl = (opts !== undefined && opts !== null) || /^(https?:|\/\/)/i.test(String(a || ''));
      if (isUrl) {
        if (budget.ajax++ > MAX_AJAX_PER_SCRIPT) return javaGetResponse('', {});
        try { return javaGetResponse(String(a), opts && typeof opts === 'object' ? opts : {}); }
        catch { return javaGetResponse('', {}); }
      }
      return store.get(String(a)) ?? '';
    },
    getVariable: () => (store.get('variable') ?? ''),
    putVariable: (v) => { store.set('variable', v); return ''; },
    log: () => '',
    toast: () => '',
    longToast: () => '',
    base64Encode: b64encode,
    base64Decode: b64decode,
    md5Encode: md5,
    md5: md5,
    sha1Encode: sha1,
    sha256Encode: sha256,
    hexDecodeToString: (hex) => {
      try { return Buffer.from(String(hex).replace(/[^0-9a-fA-F]/g, ''), 'hex').toString('utf8'); } catch { return ''; }
    },
    hexEncodeToString: (str) => Buffer.from(String(str), 'utf8').toString('hex'),
    encodeURI: (s) => encodeURI(String(s)),
    decodeURI: (s) => { try { return decodeURI(String(s)); } catch { return String(s); } },
    t2s: identity,
    s2t: identity,
    timeFormat,
    toNumChapter,
    androidId: () => '0000000000000000',
    getCookie: (url) => jar.get(String(url)) || '',
    // Browser/webview APIs cannot be honoured headlessly. Returning an empty
    // body keeps scripts that only probe for availability from crashing.
    startBrowserAwait: () => '',
    startBrowser: () => '',
    webView: () => '',
    refreshTocUrl: () => '',
    getString: (_rule, content) => String(content == null ? '' : content),
    setContent: () => '',
    // Legado's java.getElement returns an Element (with .text()/.html()/.attr()),
    // not a string; scripts chain on it. Reuse the jsoup wrapper shape.
    getElement: (html, rule) => {
      let src = html;
      let sel = rule;
      // One-argument form: `java.getElement("id.oneboolt@tag.tr.-2@span")`
      if (rule === undefined) { src = ctx.content || ''; sel = html; }
      const doc = parseHtml(String(src == null ? '' : src));
      let nodes;
      if (sel === undefined || sel === null || sel === '') nodes = [doc];
      else {
        // `@@a@b` skips the implicit first step; `@` chains selectors.
        const clean = String(sel).replace(/^@@?/, '');
        nodes = applyRuleToDoc(doc, clean);
      }
      return makeElement(nodes);
    },
    getElements: (html, rule) => {
      const doc = parseHtml(String(html == null ? '' : html));
      return select(doc, String(rule || '*')).map(htmlText);
    },
    createSymmetricCrypto: () => ({
      encryptHex: (s) => String(s),
      decryptHex: (s) => String(s),
      encryptBase64: (s) => b64encode(s),
      decryptBase64: (s) => b64decode(s),
    }),
    isSupported: () => false,
  };
}

// `$` in Legado is a JSONPath-ish view over the previous result. A proxy over
// the object covers the common `$.field` / `$.a.b` reads.
// Legado scripts treat the incoming value as a Java List<Element>: they call
// result.toArray(), .size(), .get(i), .isEmpty(). Arrays give none of those, so
// expose them. Non-array results pass through untouched.
function wrapJavaList(v) {
  if (!Array.isArray(v)) return v;
  const methods = {
    toArray: () => v.map((x) => makeElement([x])),
    size: () => v.length,
    get: (i) => makeElement([v[i]]),
    isEmpty: () => v.length === 0,
    add: (x) => { v.push(x); return true; },
    first: () => makeElement([v[0]]),
    last: () => makeElement([v[v.length - 1]]),
  };
  return new Proxy(v, {
    get(t, k) {
      if (k in methods) return methods[k];
      if (k === 'length') return t.length;
      if (typeof k === 'string' && /^\d+$/.test(k)) return t[Number(k)];
      return t[k];
    },
  });
}

function makeDollar(result, jsonPath) {
  const wrap = (v) => {
    if (v === null || v === undefined) return '';
    if (typeof v !== 'object') return v;
    return new Proxy(v, {
      get(t, k) {
        if (k === Symbol.toPrimitive) return () => JSON.stringify(t);
        if (k === 'toString') return () => JSON.stringify(t);
        return wrap(t[k]);
      },
    });
  };
  const target = wrap(result);
  if (typeof target === 'object' && target !== null) return target;
  // A string result still supports $.  (jsonpath calls); route those to the
  // JSONPath helper rather than failing.
  return new Proxy({}, { get: (_t, k) => (k === 'toString' ? () => String(result) : () => jsonPath(String(k), result)) });
}


// `cookie.*` and `org.jsoup.*` are Rhino host objects the 自写 sources call.
// removeCookie is a no-op here (no persistent jar), but it must exist or the
// whole search rule throws.
function makeCookie(ctx) {
  return {
    removeCookie: () => '',
    setCookie: () => '',
    getCookie: () => '',
    replaceCookie: () => '',
  };
}

// A node list in the shape Legado scripts expect from java.getElement /
// jsoup select results: .text(), .html(), .attr(), .select(), .size(), .get().
function makeElement(nodes) {
  const list = Array.isArray(nodes) ? nodes : (nodes ? [nodes] : []);
  return {
    select: (sel) => makeElement(list.flatMap((n) => select(n, String(sel)))),
    attr: (name) => (list[0] ? String(attr(list[0], String(name)) || '') : ''),
    text: () => (list[0] ? htmlText(list[0]) : ''),
    ownText: () => (list[0] ? htmlText(list[0]) : ''),
    html: () => (list[0] ? innerHtml(list[0]) : ''),
    outerHtml: () => (list[0] ? outerHtml(list[0]) : ''),
    first: () => makeElement(list.slice(0, 1)),
    last: () => makeElement(list.slice(-1)),
    get: (i) => makeElement(list.slice(i, i + 1)),
    size: () => list.length,
    isEmpty: () => list.length === 0,
    toString: () => (list[0] ? outerHtml(list[0]) : ''),
    // jsoup's Element.toString() is the outer HTML, so `String(el).match(/href/)`
    // (a common way sources pull an id out) sees the attributes.
    [Symbol.toPrimitive]: () => (list[0] ? outerHtml(list[0]) : ''),
  };
}

// Evaluate a Legado selector chain against an already-parsed document.
function applyRuleToDoc(doc, rule) {
  const steps = splitTop(String(rule), '@').map((x) => x.trim()).filter(Boolean);
  let nodes = [doc];
  for (const st of steps) {
    const m = /^(class|id|tag|text)\.(.*)$/s.exec(st);
    let sel = st;
    if (m) sel = m[1] === 'class' ? '.' + m[2] : m[1] === 'id' ? '#' + m[2] : m[2];
    if (st === 'text' || /^text\./.test(st)) { nodes = nodes; continue; }
    let idx = null;
    const im = /\.(-?\d+)\s*$/.exec(sel);
    if (im) { idx = Number(im[1]); sel = sel.slice(0, im.index); }
    let next = [];
    for (const n of nodes) next.push(...select(n, sel));
    nodes = idx == null ? next : (idx < 0 ? next.slice(idx) : next.slice(idx, idx + 1));
  }
  return nodes;
}

function makeJsoup() {
  const parse = (html) => {
    const doc = parseHtml(String(html == null ? '' : html));
    const wrap = (nodes) => ({
      select: (sel) => wrap(nodes.flatMap((n) => select(n, String(sel)))),
      attr: (name) => (nodes[0] ? String(attr(nodes[0], String(name)) || '') : ''),
      text: () => (nodes[0] ? htmlText(nodes[0]) : ''),
      html: () => (nodes[0] ? innerHtml(nodes[0]) : ''),
      first: () => wrap(nodes.slice(0, 1)),
      get: (i) => wrap(nodes.slice(i, i + 1)),
      size: () => nodes.length,
      toString: () => (nodes[0] ? outerHtml(nodes[0]) : ''),
    });
    return wrap([doc]);
  };
  return { Jsoup: { parse, connect: () => ({ get: () => ({}) }) } };
}

const FORBIDDEN = /\b(process|require|globalThis|module|exports|import|eval|Function|setTimeout|setInterval|setImmediate|queueMicrotask)\b/;

function unwrap(script, marker) {
  return String(script == null ? '' : script)
    .replace(new RegExp(`^@${marker}:\\s*`), '')
    .replace(/^<js>/, '')
    .replace(/<\/js>\s*$/, '');
}

// Runs one @js: rule. Returns { ok, value } — value is whatever the script
// produced (a string, array, object, or undefined).
export function runJsRule(script, ctx = {}) {
  const src = unwrap(script, 'js');
  if (FORBIDDEN.test(src)) return { ok: false, error: 'forbidden-api' };

  const budget = { ajax: 0 };
  const sandbox = Object.create(null);
  const bindings = {
    baseUrl: ctx.baseUrl || '',
    baseURI: ctx.baseUrl || '',
    cookie: makeCookie(ctx),
    getUrl: () => ctx.bookSourceUrl || ctx.baseUrl || '',
    org: { jsoup: makeJsoup() },
    key: ctx.key || '',
    page: ctx.page ?? 1,
    java: makeJava(ctx, budget),
    source: makeSource(ctx),
    book: makeBook(ctx),
    chapter: ctx.chapter || {},
    cache: ctx.cache || {},
    JSON, Math, Date, String, Number, Boolean, Array, Object, RegExp, Error,
    parseInt, parseFloat, isNaN, isFinite, encodeURIComponent, decodeURIComponent,
    encodeURI, decodeURI, unescape, escape,
    console: { log: () => {} },
  };
  Object.assign(sandbox, bindings);
  sandbox.$ = makeDollar(ctx.result, ctx.jsonPath || (() => ''));
  const names = Object.keys(bindings);
  // `result` starts as the incoming value and is read back afterwards; a script
  // that assigns it wins, otherwise Rhino's "value of the last expression"
  // semantics come from the script's completion value.
  const incoming = wrapJavaList(ctx.result);
  sandbox.result = incoming;

  const wrapFn = () => vm.runInNewContext(
    `(function(${names.join(',')}, result){\n${src}\nreturn result;\n})`,
    sandbox, { timeout: MAX_SCRIPT_MS, displayErrors: false });

  let out;
  try {
    // Running the body as a script (not a function) makes runInNewContext return
    // the completion value of the final expression statement, which is how the
    // 自写 search templates end (`...JSON.stringify({method:"GET"})`).
    const completion = vm.runInNewContext(src, sandbox, { timeout: MAX_SCRIPT_MS, displayErrors: false });
    // The script's own value wins only if it actually assigned `result`; a bare
    // final expression is Rhino's implicit return, so the completion value runs.
    const assigned = sandbox.result !== incoming && sandbox.result !== undefined;
    out = assigned ? sandbox.result : completion;
    if (out === undefined) out = sandbox.result;
  } catch (e) {
    if (/Illegal return statement|return.*not.*(function|top level)/i.test(e.message || '')) {
      // A top-level `return` needs the function wrapper.
      try {
        const fn = wrapFn();
        out = fn(...names.map((n) => sandbox[n]), ctx.result);
      } catch (e2) {
        return { ok: false, error: `runtime: ${e2.message}` };
      }
    } else if (/SyntaxError|compile/i.test(e.name + e.message)) {
      return { ok: false, error: `compile: ${e.message}` };
    } else {
      return { ok: false, error: `runtime: ${e.message}` };
    }
  }
  return { ok: true, value: out };
}

function makeSource(ctx) {
  const vars = ctx.vars || new Map();
  return {
    bookSourceUrl: ctx.bookSourceUrl || '',
    bookSourceName: ctx.bookSourceName || '',
    bookSourceGroup: ctx.bookSourceGroup || '',
    bookSourceComment: ctx.bookSourceComment || '',
    bookSourceType: 0,
    key: ctx.key || '',
    // Legado's source.getKey() is the book source URL, not the search keyword;
    // the 自写 template uses java.get(source.getKey()).headers('Location') to
    // discover the real host behind a redirect.
    getKey: () => ctx.bookSourceUrl || ctx.key || '',
    getVariable: (k) => (vars.get(k) ?? ''),
    setVariable: (k, v) => { vars.set(k, v); return ''; },
    getVariableMap: () => Object.fromEntries(vars),
    getLoginInfoMap: () => ({}),
    getLoginHeaderMap: () => ({}),
    putLoginInfo: () => '',
    loginUrl: ctx.loginUrl || '',
    header: ctx.header || '',
    searchUrl: ctx.searchUrl || '',
    getTag: () => ctx.bookSourceGroup || '',
  };
}

// Book/chapter objects: unknown properties read as "" rather than undefined so
// rules comparing them do not explode.
function makeBook(ctx) {
  const b = ctx.book || {};
  return new Proxy(b, {
    get(t, k) {
      if (k in t) return t[k];
      if (k === 'durChapterIndex') return 0;
      if (k === 'type') return 0;
      if (k === 'getVariable' || k === 'setVariable') return () => '';
      return '';
    },
  });
}

export { htmlText, innerHtml, attr, select, selectFirst, serialize, parseHtml };
