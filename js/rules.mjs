// ---------------------------------------------------------------------------
// 来源: 另一 AI 为同一用户做的 tvbox-hub 项目 (lib/rules.mjs, MIT).
// 用户 2026-11-13 决定把评审中值得借鉴的部分并入本项目, 本文件为原样复制
// (仅 http.mjs 去掉了证书失败时不验证重试的逻辑, 见该文件内注释)。
// 上游代码由对方 AI 编写, 本项目仅作复用, 不宣称原创。
// ---------------------------------------------------------------------------
// Legado rule evaluator.
//
// A rule is a `@`-separated pipeline. Each step either narrows a selection
// (CSS-ish selector), pulls a value out (attribute accessor, JSONPath, XPath),
// or transforms the running value (@js:, {{}} template, ##regex##replacement).
// This implements the subset the published sources actually use — measured over
// the current 500-source pool, `class.`/`id.`/`tag.`/`.N`/`@attr`/`$.`/`@js:`
// cover all but a handful.
import { parseHtml, select, text as htmlText, innerHtml, attr, ownText, textNodes, serialize } from './html.mjs';
import { runJsRule } from './jsrule.mjs';

const HTML_TAGS = new Set(['a', 'abbr', 'address', 'area', 'article', 'aside', 'audio', 'b', 'base',
  'bdi', 'bdo', 'blockquote', 'body', 'br', 'button', 'canvas', 'caption', 'cite', 'code', 'col',
  'colgroup', 'data', 'dd', 'del', 'details', 'dfn', 'dialog', 'div', 'dl', 'dt', 'em', 'embed',
  'fieldset', 'figcaption', 'figure', 'footer', 'form', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'head',
  'header', 'hgroup', 'hr', 'html', 'i', 'iframe', 'img', 'input', 'ins', 'kbd', 'label', 'legend',
  'li', 'link', 'main', 'map', 'mark', 'menu', 'meta', 'meter', 'nav', 'noscript', 'object', 'ol',
  'optgroup', 'option', 'output', 'p', 'param', 'picture', 'pre', 'progress', 'q', 'rp', 'rt',
  'ruby', 's', 'samp', 'script', 'section', 'select', 'slot', 'small', 'source', 'span', 'strong',
  'style', 'sub', 'summary', 'sup', 'table', 'tbody', 'td', 'template', 'textarea', 'tfoot', 'th',
  'thead', 'time', 'title', 'tr', 'track', 'u', 'ul', 'var', 'video', 'wbr']);

const ACCESSORS = new Set(['text', 'textNodes', 'ownText', 'html', 'all', 'outerHtml', 'content']);

// ---------------------------------------------------------------------------
// Splitting helpers. These all have to respect {{...}}, [...] and quoted
// strings, because rules embed regexes and templates that contain separators.

function splitTop(s, sep) {
  const out = [];
  let depth = 0, brace = 0, buf = '', quote = null, i = 0;
  while (i < s.length) {
    const c = s[i];
    if (quote) { buf += c; if (c === quote && s[i - 1] !== '\\') quote = null; i++; continue; }
    if (c === '"' || c === "'") { quote = c; buf += c; i++; continue; }
    if (c === '{' && s[i + 1] === '{') { brace++; buf += '{{'; i += 2; continue; }
    if (c === '}' && s[i + 1] === '}' && brace > 0) { brace--; buf += '}}'; i += 2; continue; }
    if (c === '[') depth++;
    if (c === ']') depth--;
    if (c === '(') depth++;
    if (c === ')') depth--;
    if (c === sep && depth <= 0 && brace === 0) { out.push(buf); buf = ''; i++; continue; }
    buf += c; i++;
  }
  out.push(buf);
  return out;
}

// `##regex##replacement` (or `##regex` for removal) is appended to a rule and
// applies to that rule's final value.
// `rule##regex##replacement`. `##` also occurs inside @js:/<js> bodies (e.g.
// /^##/ matches a "##title" search convention), so script blocks are masked
// before scanning and the separator is only honoured outside them.
function splitReplace(rule) {
  const s = String(rule);
  const mask = new Array(s.length).fill(false);
  const maskRange = (a, b) => { for (let i = a; i < b && i < s.length; i++) mask[i] = true; };

  // `<js>...</js>` is opaque: nothing inside it is a replacement separator.
  for (const m of s.matchAll(/<js>[\s\S]*?<\/js>/g)) maskRange(m.index, m.index + m[0].length);

  // Inside an @js: body `##` can legitimately appear in a regex literal
  // (/^##/) or a string ("##title"). Only those occurrences are masked; a
  // trailing `##regex##replacement` after the script stays a separator.
  for (const m of s.matchAll(/@js:/g)) {
    const from = m.index + 4;
    let q = '';
    for (let k = from; k + 1 < s.length; k++) {
      const c = s[k];
      if (q) { if (c === '\\') { k++; continue; } if (c === q) q = ''; continue; }
      if (c === '"' || c === "'" || c === '`') { q = c; continue; }
      if (c === '#' && s[k + 1] === '#') {
        const prev = s[k - 1], next = s[k + 2];
        if (prev === '/' || next === '/' || (q && c !== q)) { mask[k] = mask[k + 1] = true; }
      }
    }
  }

  const nextSep = (from) => {
    for (let i = from; i + 1 < s.length; i++) {
      if (s[i] === '#' && s[i + 1] === '#' && !mask[i] && !mask[i + 1]) return i;
    }
    return -1;
  };
  const at = nextSep(0);
  if (at < 0) return { body: s, regex: null, repl: null };
  const at2 = nextSep(at + 2);
  const body = s.slice(0, at);
  if (at2 < 0) return { body, regex: s.slice(at + 2), repl: '' };
  const regex = s.slice(at + 2, at2);
  const repl = s.slice(at2 + 2);
  if (!regex) return { body, regex: null, repl: null };
  return { body, regex, repl };
}

function applyReplace(value, regex, repl) {
  if (regex == null) return value;
  const map = (s) => {
    let re;
    try { re = new RegExp(regex, 'g'); } catch { return s; }
    const str = String(s);
    if (repl === undefined || repl === null || repl === '') return str.replace(re, '');
    // Legado writes the replacement for the whole match in $0 style groups;
    // JS uses $& for that, so rewrite a bare $0.
    const r = String(repl).replace(/\$0/g, '$&');
    return str.replace(re, r);
  };
  return Array.isArray(value) ? value.map(map) : map(value);
}

// ---------------------------------------------------------------------------
// Minimal XPath: //tag, /tag, *, [@attr], [@attr="v"], [n], @attr, text().

function xpathSelect(root, expr) {
  let e = String(expr).trim();
  let attrSel = null;
  // A trailing `.N` is Legado's element index, not part of a tag name.
  let index = null;
  let idxM;
  if ((idxM = /\.(-?\d+)\s*$/.exec(e))) { index = Number(idxM[1]); e = e.slice(0, idxM.index); }
  const attrTail = /\/@([\w-]+)$/.exec(e);
  if (attrTail) { attrSel = attrTail[1]; e = e.slice(0, attrTail.index); }
  const textTail = /\/text\(\)$/.test(e);
  if (textTail) e = e.replace(/\/text\(\)$/, '');

  const steps = e.split('/').filter((s) => s !== '');
  // `//a/b` = any-depth `a`, then child `b`; `/a/b` = body child `a`, then child `b`.
  let current = [root];
  const deepFirst = e.startsWith('//');

  const siblingsOf = (cand, tag) => {
    const par = cand.parent;
    if (!par || !par.children) return [cand];
    return par.children.filter((x) => x.type === 'element' && (!tag || x.name === tag));
  };
  const predOk = (cand, pred, tag) => {
    for (const sub of pred.split(/\s+and\s+/i)) {
      const t = sub.trim();
      if (!t) continue;
      const mAttr = /^@([\w-]+)\s*(?:=\s*["']([^"']*)["'])?$/.exec(t);
      if (mAttr) {
        const v = cand.attrs[mAttr[1]];
        if (v === undefined) return false;
        if (mAttr[2] !== undefined && v !== mAttr[2]) return false;
        continue;
      }
      const mPos = /^position\(\)\s*(>=|<=|>|<|=)\s*(\d+)$/.exec(t);
      if (mPos) {
        const idx = siblingsOf(cand, tag).indexOf(cand) + 1;
        const n = Number(mPos[2]);
        const ok = mPos[1] === '>' ? idx > n : mPos[1] === '<' ? idx < n
          : mPos[1] === '>=' ? idx >= n : mPos[1] === '<=' ? idx <= n : idx === n;
        if (!ok) return false;
        continue;
      }
      const mLast = /^last\(\)\s*(?:-\s*(\d+))?$/.exec(t);
      if (mLast) {
        const sibs = siblingsOf(cand, tag);
        const want = sibs.length - (mLast[1] ? Number(mLast[1]) : 0);
        if (sibs.indexOf(cand) + 1 !== want) return false;
        continue;
      }
      if (/^\d+$/.test(t)) {
        if (siblingsOf(cand, tag).indexOf(cand) + 1 !== Number(t)) return false;
        continue;
      }
      if (/^text\(\)\s*=\s*["']/.test(t)) {
        const want = t.replace(/^text\(\)\s*=\s*["']/, '').replace(/["']$/, '');
        if (htmlText(cand).trim() !== want) return false;
        continue;
      }
      return false;
    }
    return true;
  };

  for (let i = 0; i < steps.length; i++) {
    let step = steps[i];
    const preds = [];
    step = step.replace(/\[([^\]]*)\]/g, (_m, p) => { preds.push(p); return ''; });
    const star = step === '*' || step === '';
    const tag = star ? null : step.toLowerCase().replace(/^@/, '');
    const deep = deepFirst && i === 0;
    const next = [];
    for (const node of current) {
      const pool = [];
      const collect = (n) => {
        for (const c of n.children || []) {
          if (c.type !== 'element') continue;
          pool.push(c);
          if (deep) collect(c);
        }
      };
      collect(node);
      for (const cand of pool) {
        if (tag && cand.name !== tag) continue;
        let ok = true;
        for (const p of preds) { if (!predOk(cand, p, tag)) { ok = false; break; } }
        if (ok) next.push(cand);
      }
    }
    current = [...new Set(next)];
  }
  let result = current;
  if (index != null) result = index < 0 ? current.slice(index) : current.slice(index, index + 1);
  if (attrSel) return result.map((n) => attr(n, attrSel));
  if (textTail) return result.map(htmlText);
  return result;
}

// ---------------------------------------------------------------------------
// JSONPath subset: $.a.b, $.a[0], $..name, .[?(@.x)], plain `a.b`.

function jsonPathGet(data, path) {
  let p = String(path).trim();
  // evalPipeline splits on '@', so by here `@json:` has become `json:`.
  p = p.replace(/^@?json:/i, '').trim();
  p = p.replace(/^\$/, '');
  if (!p || p === '.') return data;
  // Recursive descent: $..key collects every match at any depth.
  const recursive = p.startsWith('..');
  if (recursive) {
    const key = p.slice(2).split(/[.[]/)[0];
    const found = [];
    const walk = (v) => {
      if (Array.isArray(v)) { for (const x of v) walk(x); return; }
      if (v && typeof v === 'object') {
        for (const [k, val] of Object.entries(v)) {
          if (k === key) found.push(val);
          walk(val);
        }
      }
    };
    walk(data);
    return found;
  }

  const tokens = [];
  const re = /\.([^.[\]]+)|\[(\d+)\]|\[['"]([^'"]+)['"]\]|\[\?\(([^)]*)\)\]|\[([^\]]*)\]/g;
  let m;
  let consumed = 0;
  while ((m = re.exec(p))) {
    if (m.index > consumed) {
      const lead = p.slice(consumed, m.index).replace(/^\./, '');
      if (lead) tokens.push({ key: lead });
    }
    consumed = m.index + m[0].length;
    if (m[1] !== undefined) tokens.push({ key: m[1] });
    else if (m[2] !== undefined) tokens.push({ idx: Number(m[2]) });
    else if (m[3] !== undefined) tokens.push({ key: m[3] });
    else if (m[4] !== undefined) tokens.push({ filter: m[4] });
    else if (m[5] === '*') tokens.push({ wild: true });
    else if (m[5] !== undefined) tokens.push({ key: m[5] });
  }
  if (consumed < p.length) {
    const tail = p.slice(consumed).replace(/^\./, '');
    if (tail) tokens.push({ key: tail });
  }

  let cur = data;
  for (const t of tokens) {
    if (cur === null || cur === undefined) return '';
    if (t.key !== undefined) {
      const parts = String(t.key).split('.');
      for (const part of parts) {
        if (cur === null || cur === undefined) return '';
        if (Array.isArray(cur)) {
          // A key after [*] maps over the exploded elements and drops misses,
          // which is what Legado's `$.list[*].name` is expected to yield.
          const mapped = cur.map((x) => (x && typeof x === 'object' ? x[part] : undefined)).filter((v) => v !== undefined);
          cur = mapped.length ? mapped : cur[part];
        } else {
          cur = cur[part];
        }
      }
    } else if (t.wild) {
      // `[*]` explodes to every element; later tokens then map over them.
      cur = Array.isArray(cur) ? cur.slice() : (cur && typeof cur === 'object' ? Object.values(cur) : []);
    } else if (t.idx !== undefined) {
      if (Array.isArray(cur)) {
        // After a [*] explosion `cur` is a list of nodes, so an index selects a
        // field from each rather than an element of the list.
        cur = (cur.length && cur.every((x) => x && typeof x === 'object' && !Array.isArray(x)) && cur.some((x) => t.idx in x))
          ? cur.map((x) => x[t.idx])
          : cur[t.idx];
      } else {
        cur = cur && typeof cur === 'object' ? Object.values(cur)[t.idx] : undefined;
      }
    } else if (t.filter !== undefined) {
      const fm = /@\.([\w.]+)\s*(==|!=|>=|<=|>|<|=~)?\s*(.*)/.exec(t.filter.trim());
      const list = Array.isArray(cur) ? cur : [cur];
      cur = list.filter((it) => {
        if (!fm) return false;
        const keys = fm[1].split('.');
        let v = it;
        for (const k of keys) { if (v == null) return false; v = v[k]; }
        const want = fm[3].replace(/^['"]|['"]$/g, '');
        switch (fm[2]) {
          case '==': case '=': return String(v) === want;
          case '!=': return String(v) !== want;
          case '>': return Number(v) > Number(want);
          case '<': return Number(v) < Number(want);
          case '>=': return Number(v) >= Number(want);
          case '<=': return Number(v) <= Number(want);
          case '=~': try { return new RegExp(want).test(String(v)); } catch { return false; }
          default: return v !== undefined;
        }
      });
      // Legado keeps the array when a filter is the last token and it is empty,
      // so callers can tell "no match" from "no data" — mirrors that here.
    }
  }
  return cur;
}

// ---------------------------------------------------------------------------
// Selector step parsing.

// Splits a step into its base, a trailing element index (`.-1`, `!0`, `[0]`) and
// a `text.` substring match. The index comes off first: `text.-1` is the text
// accessor at index -1, not a `text.` match on "-1".
function parseStep(step) {
  let s = String(step).trim();
  let index = null, exclude = null, textMatch = null, range = null;
  let m;
  // Legado's range form `[1:20]` (1-based, end-exclusive) and `[:5]` / `[2:]`.
  if ((m = /\[(-?\d*):(-?\d*)\]\s*$/.exec(s))) {
    const a = m[1] === '' ? 0 : Number(m[1]);
    const b = m[2] === '' ? null : Number(m[2]);
    range = [a, b];
    s = s.slice(0, m.index);
  }
  else if ((m = /!(\d+)\s*$/.exec(s))) { exclude = Number(m[1]); s = s.slice(0, m.index); }
  else if ((m = /\[(-?\d+)\]\s*$/.exec(s))) { index = Number(m[1]); s = s.slice(0, m.index); }
  else if ((m = /\.(-?\d+)\s*$/.exec(s))) { index = Number(m[1]); s = s.slice(0, m.index); }

  if (s === '') s = '*';

  // A bare name that is not an element is an accessor.
  if (looksLikeAttributeStep(s)) return { kind: 'accessor', name: s, index, exclude };

  const prefix = /^(class|id|tag|text)\.(.*)$/s.exec(s);
  if (prefix) {
    const [, kind, rest] = prefix;
    if (kind === 'class') s = '.' + rest;
    else if (kind === 'id') s = '#' + rest;
    else if (kind === 'tag') s = rest;
    else if (kind === 'text') { textMatch = rest; s = '*'; }
  }
  return { kind: 'selector', selector: s || '*', index, exclude, textMatch, range };
}

function looksLikeAttributeStep(step) {
  const s = String(step).trim();
  if (!/^[a-zA-Z_][\w-]*$/.test(s)) return false;
  if (ACCESSORS.has(s)) return true;
  return !HTML_TAGS.has(s.toLowerCase());
}

// ---------------------------------------------------------------------------
// Value plumbing

function toArray(v) {
  if (v === null || v === undefined) return [];
  return Array.isArray(v) ? v : [v];
}

function asDoc(input) {
  if (!input) return null;
  if (input.type === 'root' || input.type === 'element') return input;
  return parseHtml(String(input));
}

function flattenElements(list) {
  const out = [];
  for (const v of list) {
    if (v && (v.type === 'element' || v.type === 'root')) out.push(v);
  }
  return out;
}

// ---------------------------------------------------------------------------
// Main evaluator

export function evalRule(rule, ctx = {}) {
  const { body, regex, repl } = splitReplace(rule);
  let value = evalPipeline(body, ctx);
  value = applyReplace(value, regex, repl);
  return value;
}

function evalPipeline(body, ctx) {
  // `selector<js>...</js>`: the script consumes the selector's result. Handle it
  // before the operator split, whose else-branch parsers would swallow the tag.
  const jsAt = String(body).indexOf('<js>');
  if (jsAt >= 0) {
    const head = String(body).slice(0, jsAt).trim();
    const tail = String(body).slice(jsAt);
    const base = head ? evalPipeline(head, ctx) : ctx.input;
    const r = runJsRule(tail, { ...ctx, result: base, page: ctx.page });
    if (r.ok) return r.value;
    // A failing transform is not a hard error: keep what the head produced.
    if (head) return base;
  }

  // `||` picks the first non-empty branch, `&&` concatenates, `%%` merges all.
  for (const [sep, kind] of [['||', 'first'], ['&&', 'concat'], ['%%', 'merge']]) {
    const parts = splitTop(body, sep[0] === '|' ? '|' : sep[0]);
    if (parts.length > 1) {
      const branches = [];
      for (let i = 0; i < parts.length; i += 1) {
        const raw = parts[i];
        // A doubled separator (`||`, `&&`, `%%`) is one operator; the split above
        // is per-character, so skip the empty halves it produces.
        if (raw === '') continue;
        branches.push(raw);
      }
      if (kind === 'first') {
        for (const b of branches) {
          const v = evalPipeline(b, ctx);
          if (!isEmpty(v)) return v;
        }
        return '';
      }
      const vals = branches.map((b) => evalPipeline(b, ctx)).filter((v) => !isEmpty(v));
      if (!vals.length) return '';
      if (kind === 'concat') return vals.map(stringify).join('');
      const merged = [];
      for (const v of vals) merged.push(...toArray(v));
      return merged;
    }
  }

  const steps = splitTop(body, '@').map((s) => s.trim());
  let value = ctx.input;
  let started = false;

  for (const rawStep of steps) {
    const step = rawStep;
    if (!step) continue;

    // Mode-prefixed steps.
    if (/^css:/i.test(step)) {
      const doc = asDoc(started ? firstElementOrString(value) : value);
      value = select(doc || parseHtml(''), step.slice(4).trim());
      started = true; continue;
    }
    if (/^XPath:/i.test(step) || step.startsWith('//')) {
      const doc = asDoc(started ? firstElementOrString(value) : value);
      const expr = step.replace(/^XPath:/i, '').trim();
      const raw = xpathSelect(doc || parseHtml(''), expr);
      // A tail selecting @attr or text() yields strings; anything else yields
      // nodes that later steps can keep narrowing.
      value = /\/@[\w-]+$|\/text\(\)$/.test(expr) ? raw : raw.filter((n) => isElement(n));
      started = true; continue;
    }
    if (/^json:/i.test(step) || step.startsWith('$') || step.startsWith('@json:')) {
      const base = started ? value : ctx.input;
      value = jsonPathGet(normalizeJson(base), step);
      started = true; continue;
    }
    if (/^js:/i.test(step) || /^<js>/.test(step) || /^@js:/.test(step)) {
      const base = started ? value : ctx.input;
      const r = runJsRule(step, { ...ctx, result: base, page: ctx.page });
      value = r.ok ? r.value : '';
      started = true; continue;
    }
    if (/^get:\{.*\}$/.test(step) || /^put:\{.*\}$/.test(step)) {
      value = cacheStep(step, ctx, started ? value : ctx.input);
      started = true; continue;
    }
    if (step.startsWith('{{')) {
      value = expandTemplate(step, { ...ctx, result: started ? value : ctx.input });
      started = true; continue;
    }

    // Selector or accessor step.
    const st = parseStep(step);
    if (st.kind === 'accessor') {
      let src = started ? flattenElements(toArray(value)) : [];
      if (!src.length && isElement(ctx.input)) src = [ctx.input];
      value = applyIndex(src.map((n) => readAccessor(n, st.name)), st.index, st.exclude);
      started = true; continue;
    }
    const { selector, index, exclude, textMatch, range } = st;
    const baseNodes = started ? flattenElements(toArray(value)) : [];
    let docs;
    if (!started) {
      docs = [asDoc(ctx.input) || parseHtml('')];
    } else if (baseNodes.length) {
      docs = baseNodes;
    } else {
      // Previous step produced a string (a fetched page or an @js: body):
      // re-parse it as HTML, which is how `@js:...` chains into a selector.
      const s = typeof value === 'string' ? value : stringify(value);
      docs = [asDoc(s) || parseHtml('')];
    }

    let picked = [];
    for (const d of docs) {
      if (!d) continue;
      let found;
      if (textMatch != null) {
        const needle = textMatch.trim();
        found = allDesc(d).filter((el) => htmlText(el).includes(needle));
      } else if (selector === '*') {
        found = d.type === 'element' ? [d, ...allDesc(d)] : allDesc(d);
      } else {
        found = select(d, selector);
      }
      picked.push(...found);
    }
    value = applyIndex(picked, index, exclude, range);
    started = true;
  }

  return value;
}

function applyIndex(list, index, exclude, range) {
  let out = list;
  if (exclude != null) out = out.filter((_, i) => i !== exclude);
  if (range) {
    // Legado's `[a:b]` is 0-based and end-exclusive: [1:20] skips item 0.
    const end = range[1] == null ? undefined : range[1];
    const start = range[0] < 0 ? Math.max(0, out.length + range[0]) : range[0];
    out = out.slice(start, end);
  }
  if (index != null) out = index < 0 ? out.slice(index) : out.slice(index, index + 1);
  return out;
}

function allDesc(node) {
  const out = [];
  const walk = (n) => { for (const c of n.children || []) { if (c.type === 'element') { out.push(c); walk(c); } } };
  walk(node);
  return out;
}

function isElement(v) { return !!v && (v.type === 'element' || v.type === 'root'); }

function firstElementOrString(v) {
  const arr = toArray(v);
  for (const x of arr) if (isElement(x)) return x;
  for (const x of arr) if (typeof x === 'string' && x.trim()) return x;
  return v;
}

function normalizeJson(v) {
  const arr = toArray(v);
  const first = arr.length ? arr[0] : v;
  if (typeof first === 'string') {
    const t = first.trim();
    if (t.startsWith('{') || t.startsWith('[')) {
      try { return JSON.parse(t); } catch { return first; }
    }
  }
  if (isElement(first)) return htmlText(first);
  return first;
}

function readAccessor(node, name) {
  switch (name) {
    case 'text': return htmlText(node);
    case 'textNodes': return textNodes(node);
    case 'ownText': return ownText(node);
    case 'html': return innerHtml(node);
    case 'outerHtml':
    case 'all': return serialize(node);
    default: return attr(node, name);
  }
}

function cacheStep(step, ctx, input) {
  const store = ctx.store || new Map();
  const get = /^get:\{([^}]*)\}$/.exec(step);
  if (get) {
    const key = get[1].trim().replace(/^["']|["']$/g, '');
    return store.get(key) ?? '';
  }
  const put = /^put:\{([^}]*)\}$/.exec(step);
  if (put) {
    const body = put[1];
    const colon = body.indexOf(':');
    if (colon > 0) {
      const key = body.slice(0, colon).trim().replace(/^["']|["']$/g, '');
      const rule = body.slice(colon + 1).trim();
      const v = evalRule(rule, { ...ctx, input });
      store.set(key, v);
      return input;
    }
    store.set(body.trim(), input);
    return input;
  }
  return input;
}

export function isEmpty(v) {
  if (v === null || v === undefined) return true;
  if (typeof v === 'string') return v.trim() === '';
  if (Array.isArray(v)) return v.length === 0;
  return false;
}

// Best-effort string form of a rule result; arrays collapse to their first
// non-empty entry, which matches Legado's getString().
export function stringify(v) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'string') return v;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  if (isElement(v)) return htmlText(v);
  if (Array.isArray(v)) {
    for (const x of v) { const s = stringify(x); if (s.trim()) return s; }
    return '';
  }
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

export function evalRuleString(rule, ctx = {}) {
  if (rule == null || rule === '') return '';
  return stringify(evalRule(rule, ctx)).trim();
}

export function evalRuleList(rule, ctx = {}) {
  if (rule == null || rule === '') return [];
  const v = evalRule(rule, ctx);
  return toArray(v);
}

// {{...}} templates: each brace group is a rule evaluated against the context.
// `{{key}}` / `{{page}}` resolve from context vars, `{{$.x}}` from the JSON.
export function expandTemplate(tpl, ctx = {}) {
  return String(tpl).replace(/\{\{([\s\S]*?)\}\}/g, (_m, expr) => {
    const e = expr.trim();
    if (e === 'key') return ctx.key ?? '';
    if (e === 'page') return String(ctx.page ?? 1);
    if (e === '') return '';
    try {
      return stringify(evalRule(e, { ...ctx, input: ctx.result }));
    } catch {
      return '';
    }
  });
}

export { jsonPathGet, xpathSelect, splitTop, splitReplace, applyReplace };
