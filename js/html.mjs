// ---------------------------------------------------------------------------
// 来源: 另一 AI 为同一用户做的 tvbox-hub 项目 (lib/html.mjs, MIT).
// 用户 2026-11-13 决定把评审中值得借鉴的部分并入本项目, 本文件为原样复制
// (仅 http.mjs 去掉了证书失败时不验证重试的逻辑, 见该文件内注释)。
// 上游代码由对方 AI 编写, 本项目仅作复用, 不宣称原创。
// ---------------------------------------------------------------------------
// Zero-dependency HTML parser + jsoup-like selector engine.
// Only what Legado book sources actually need: tolerant parsing of messy real
// world markup, a CSS subset, and jsoup's text()/html()/attr() accessors.

const VOID = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
  'link', 'meta', 'param', 'source', 'track', 'wbr']);

const RAW_TEXT = new Set(['script', 'style', 'textarea', 'title']);

const ENTITIES = {
  amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', shy: '­',
  copy: '©', reg: '®', trade: '™', hellip: '…', mdash: '—',
  ndash: '–', lsquo: '‘', rsquo: '’', ldquo: '“', rdquo: '”',
  middot: '·', times: '×', divide: '÷', deg: '°', laquo: '«',
  raquo: '»', bull: '•', dagger: '†', permil: '‰', euro: '€',
  pound: '£', yen: '¥', cent: '¢', sect: '§', para: '¶',
};

export function decodeEntities(s) {
  if (!s || s.indexOf('&') < 0) return s;
  return s.replace(/&(#x?[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);/g, (m, body) => {
    if (body[0] === '#') {
      const hex = body[1] === 'x' || body[1] === 'X';
      const n = parseInt(body.slice(hex ? 2 : 1), hex ? 16 : 10);
      if (!Number.isFinite(n) || n < 0 || n > 0x10ffff) return m;
      try { return String.fromCodePoint(n); } catch { return m; }
    }
    const v = ENTITIES[body];
    return v === undefined ? m : v;
  });
}

export function escapeText(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function el(name) {
  return { type: 'element', name, attrs: Object.create(null), children: [], parent: null };
}

// Attribute values in the wild are unquoted, single quoted, or carry stray
// quotes inside them; parse leniently rather than failing the whole document.
const ATTR_RE = /([^\s"'>/=]+)(?:\s*=\s*("[^"]*"|'[^']*'|[^\s"'>`]+))?/g;

function parseAttrs(src) {
  const attrs = Object.create(null);
  ATTR_RE.lastIndex = 0;
  let m;
  while ((m = ATTR_RE.exec(src))) {
    const name = m[1].toLowerCase();
    let val = m[2];
    if (val === undefined) val = '';
    else if ((val[0] === '"' && val[val.length - 1] === '"') || (val[0] === "'" && val[val.length - 1] === "'")) {
      val = val.slice(1, -1);
    }
    if (!(name in attrs)) attrs[name] = decodeEntities(val);
  }
  return attrs;
}

// Parse an HTML string into a tree. Never throws: malformed markup degrades to
// something selectable rather than aborting a crawl.
export function parseHtml(input) {
  const html = String(input == null ? '' : input);
  const root = { type: 'root', name: '#root', attrs: Object.create(null), children: [], parent: null };
  const stack = [root];
  let i = 0;
  const top = () => stack[stack.length - 1];

  const pushText = (text) => {
    if (!text) return;
    const parent = top();
    const last = parent.children[parent.children.length - 1];
    // Merge with the previous text node so selectors see whole runs.
    if (last && last.type === 'text') { last.text += decodeEntities(text); return; }
    parent.children.push({ type: 'text', text: decodeEntities(text), parent });
  };

  while (i < html.length) {
    const lt = html.indexOf('<', i);
    if (lt < 0) { pushText(html.slice(i)); break; }
    if (lt > i) pushText(html.slice(i, lt));

    if (html.startsWith('<!--', lt)) {
      const end = html.indexOf('-->', lt + 4);
      i = end < 0 ? html.length : end + 3;
      continue;
    }
    if (html.startsWith('<!', lt) || html.startsWith('<?', lt)) {
      const end = html.indexOf('>', lt);
      i = end < 0 ? html.length : end + 1;
      continue;
    }
    if (html.startsWith('</', lt)) {
      const end = html.indexOf('>', lt);
      const name = html.slice(lt + 2, end < 0 ? html.length : end).trim().toLowerCase();
      i = end < 0 ? html.length : end + 1;
      // Unwind to the matching open tag; ignore stray closers.
      for (let d = stack.length - 1; d > 0; d--) {
        if (stack[d].name === name) { stack.length = d; break; }
      }
      continue;
    }

    // Opening tag: find the '>' that is not inside a quoted attribute value.
    let j = lt + 1, quote = null;
    while (j < html.length) {
      const c = html[j];
      if (quote) { if (c === quote) quote = null; }
      else if (c === '"' || c === "'") quote = c;
      else if (c === '>') break;
      j++;
    }
    if (j >= html.length) { pushText(html.slice(lt)); break; }

    let inner = html.slice(lt + 1, j);
    const selfClose = inner.endsWith('/');
    if (selfClose) inner = inner.slice(0, -1);
    const nameMatch = /^([a-zA-Z][^\s/>]*)/.exec(inner);
    i = j + 1;
    if (!nameMatch) continue;
    const name = nameMatch[1].toLowerCase();
    const node = el(name);
    node.attrs = parseAttrs(inner.slice(nameMatch[1].length));

    const parent = top();
    node.parent = parent;
    parent.children.push(node);

    if (VOID.has(name) || selfClose) continue;

    if (RAW_TEXT.has(name)) {
      // Consume to the real closing tag; inner markup is text, not elements.
      const close = new RegExp(`</${name}\\s*>`, 'i');
      const rest = html.slice(i);
      const m = close.exec(rest);
      const raw = m ? rest.slice(0, m.index) : rest;
      if (raw) node.children.push({ type: 'text', text: raw, parent: node });
      i += m ? m.index + m[0].length : rest.length;
      continue;
    }
    stack.push(node);
  }
  return root;
}

// jsoup text(): descendant text with runs of whitespace collapsed.
export function text(node) {
  if (!node) return '';
  if (node.type === 'text') return node.text;
  const parts = [];
  const walk = (n) => {
    for (const c of n.children || []) {
      if (c.type === 'text') parts.push(c.text);
      else walk(c);
    }
  };
  walk(node);
  return parts.join(' ').replace(/\s+/g, ' ').trim();
}

export function ownText(node) {
  if (!node) return '';
  if (node.type === 'text') return node.text;
  return (node.children || []).filter((c) => c.type === 'text').map((c) => c.text).join('').replace(/\s+/g, ' ').trim();
}

export function textNodes(node) {
  if (!node) return '';
  if (node.type === 'text') return node.text;
  const parts = [];
  const walk = (n) => {
    for (const c of n.children || []) {
      if (c.type === 'text') parts.push(c.text);
      else walk(c);
    }
  };
  walk(node);
  return parts.join('').replace(/\s+/g, ' ').trim();
}

export function attr(node, name) {
  if (!node || node.type !== 'element') return '';
  const v = node.attrs[String(name).toLowerCase()];
  return v === undefined ? '' : v;
}

export function hasAttr(node, name) {
  return !!(node && node.type === 'element' && String(name).toLowerCase() in node.attrs);
}

export function serialize(node) {
  if (!node) return '';
  if (node.type === 'text') return escapeText(node.text);
  if (node.type === 'root') return node.children.map(serialize).join('');
  const attrs = Object.entries(node.attrs)
    .map(([k, v]) => (v === '' ? ` ${k}` : ` ${k}="${String(v).replace(/"/g, '&quot;')}"`))
    .join('');
  const inner = node.children.map(serialize).join('');
  if (VOID.has(node.name)) return `<${node.name}${attrs}>`;
  return `<${node.name}${attrs}>${inner}</${node.name}>`;
}

export function innerHtml(node) {
  if (!node || node.type !== 'element') return '';
  return node.children.map(serialize).join('');
}

// ---------------------------------------------------------------------------
// Selectors

function parseSelector(sel) {
  const groups = [];
  for (const part of splitTop(sel, ',')) {
    const seq = [];
    let buf = '';
    let combinator = ' ';
    const flush = (next) => {
      if (!buf.trim()) { combinator = next; return; }
      seq.push({ compound: parseCompound(buf.trim()), combinator });
      buf = '';
      combinator = next;
    };
    for (let k = 0; k < part.length; k++) {
      const c = part[k];
      if (c === '>' || c === '+' || c === '~') { flush(c); continue; }
      if (/\s/.test(c)) { if (buf.trim()) flush(' '); continue; }
      buf += c;
    }
    flush(' ');
    if (seq.length) groups.push(seq);
  }
  return groups;
}

// Split on a separator that is not inside brackets or parentheses.
function splitTop(s, sep) {
  const out = [];
  let depth = 0, buf = '';
  let quote = null;
  for (const c of s) {
    if (quote) { buf += c; if (c === quote) quote = null; continue; }
    if (c === '"' || c === "'") { quote = c; buf += c; continue; }
    if (c === '[' || c === '(') depth++;
    if (c === ']' || c === ')') depth--;
    if (c === sep && depth === 0) { out.push(buf); buf = ''; continue; }
    buf += c;
  }
  out.push(buf);
  return out;
}

function parseCompound(s) {
  const c = { tag: null, id: null, classes: [], attrs: [], pseudos: [] };
  let rest = s;
  const tagM = /^(\*|[a-zA-Z][\w-]*)/.exec(rest);
  if (tagM) { c.tag = tagM[1] === '*' ? null : tagM[1].toLowerCase(); rest = rest.slice(tagM[1].length); }
  const re = /([.#])([^\s.#\[\]:>+~]+)|\[((?:[^\]"']|"[^"]*"|'[^']*')*)\]|(:[a-zA-Z-]+(?:\((?:[^()]|\([^()]*\))*\))?)/g;
  let m;
  while ((m = re.exec(rest))) {
    if (m[1] === '.') c.classes.push(m[2]);
    else if (m[1] === '#') c.id = m[2];
    else if (m[3] !== undefined) c.attrs.push(parseAttrSel(m[3]));
    else if (m[4]) {
      const p = /^:([a-zA-Z-]+)(?:\(([\s\S]*)\))?$/.exec(m[4]);
      if (p) c.pseudos.push({ name: p[1].toLowerCase(), arg: p[2] === undefined ? null : p[2] });
    }
  }
  return c;
}

function parseAttrSel(s) {
  const m = /^\s*([^\s~^$*|!=\]]+)\s*(?:([~^$*|!]?=)\s*([\s\S]*?)\s*)?$/.exec(s);
  if (!m) return { name: String(s).trim().toLowerCase(), op: 'exists', val: null };
  let val = m[3];
  if (val && ((val[0] === '"' && val.endsWith('"')) || (val[0] === "'" && val.endsWith("'")))) val = val.slice(1, -1);
  return { name: (m[1] || '').toLowerCase(), op: m[2] || 'exists', val: val === undefined ? null : decodeEntities(val) };
}

function matchAttr(node, a) {
  const v = node.attrs[a.name];
  if (a.op === 'exists') return v !== undefined;
  if (v === undefined) return false;
  switch (a.op) {
    case '=': return v === a.val;
    case '!=': return v !== a.val;
    case '^=': return v.startsWith(a.val);
    case '$=': return v.endsWith(a.val);
    case '*=': return v.includes(a.val);
    case '~=': return v.split(/\s+/).includes(a.val);
    case '|=': return v === a.val || v.startsWith(a.val + '-');
    default: return false;
  }
}

function elementSiblings(node) {
  const p = node.parent;
  if (!p) return [node];
  return p.children.filter((c) => c.type === 'element');
}

function nthIndex(node) {
  return elementSiblings(node).indexOf(node) + 1;
}

function matchPseudo(node, p) {
  switch (p.name) {
    case 'first-child': return nthIndex(node) === 1;
    case 'last-child': return nthIndex(node) === elementSiblings(node).length;
    case 'first-of-type': return node.parent.children.filter((c) => c.type === 'element' && c.name === node.name).indexOf(node) === 0;
    case 'last-of-type': {
      const s = node.parent.children.filter((c) => c.type === 'element' && c.name === node.name);
      return s.indexOf(node) === s.length - 1;
    }
    case 'only-child': return elementSiblings(node).length === 1;
    case 'empty': return !node.children.some((c) => c.type === 'element' || c.text.trim());
    case 'root': return !node.parent || node.parent.type === 'root';
    case 'not': return !matchCompound(node, parseCompound(String(p.arg || '').trim()));
    case 'has': return select(node, String(p.arg || '').trim()).length > 0;
    case 'contains': return text(node).includes(String(p.arg || '').replace(/^["']|["']$/g, ''));
    case 'eq': return nthIndex(node) === Number(p.arg) + 1;
    case 'nth-child': return matchNth(nthIndex(node), p.arg);
    case 'nth-of-type': {
      const s = node.parent.children.filter((c) => c.type === 'element' && c.name === node.name);
      return matchNth(s.indexOf(node) + 1, p.arg);
    }
    case 'first': return nthIndex(node) === 1;
    case 'last': return nthIndex(node) === elementSiblings(node).length;
    default: return true;   // unknown pseudo: ignore rather than kill the rule
  }
}

function matchNth(n, arg) {
  const a = String(arg == null ? 'n' : arg).trim().toLowerCase();
  if (a === 'odd') return n % 2 === 1;
  if (a === 'even') return n % 2 === 0;
  if (/^[+-]?\d+$/.test(a)) return n === Number(a);
  const m = /^([+-]?\d*)n\s*(?:([+-])\s*(\d+))?$/.exec(a);
  if (!m) return false;
  const mult = m[1] === '' || m[1] === '+' ? 1 : m[1] === '-' ? -1 : Number(m[1]);
  const off = m[3] ? (m[2] === '-' ? -Number(m[3]) : Number(m[3])) : 0;
  if (mult === 0) return n === off;
  const d = n - off;
  return d % mult === 0 && d / mult >= 0;
}

function matchCompound(node, c) {
  if (!node || node.type !== 'element') return false;
  if (c.tag && node.name !== c.tag) return false;
  if (c.id && node.attrs.id !== c.id) return false;
  for (const cls of c.classes) {
    const list = (node.attrs.class || '').split(/\s+/);
    if (!list.includes(cls)) return false;
  }
  for (const a of c.attrs) if (!matchAttr(node, a)) return false;
  for (const p of c.pseudos) if (!matchPseudo(node, p)) return false;
  return true;
}

function descendants(node, out = []) {
  for (const c of node.children || []) {
    if (c.type === 'element') { out.push(c); descendants(c, out); }
  }
  return out;
}

function allElements(root) {
  return root.type === 'element' ? [root, ...descendants(root)] : descendants(root);
}

function matchSeq(seq, root) {
  // Evaluate left to right, keeping a working set at each step.
  let current = allElements(root).filter((n) => matchCompound(n, seq[0].compound));
  for (let k = 1; k < seq.length; k++) {
    const step = seq[k];
    const out = [];
    const seen = new Set();
    for (const node of current) {
      let cands = [];
      if (step.combinator === '>') cands = (node.children || []).filter((c) => c.type === 'element');
      else if (step.combinator === '+') {
        const sibs = elementSiblings(node);
        const idx = sibs.indexOf(node);
        cands = idx >= 0 && idx + 1 < sibs.length ? [sibs[idx + 1]] : [];
      } else if (step.combinator === '~') {
        const sibs = elementSiblings(node);
        cands = sibs.slice(sibs.indexOf(node) + 1);
      } else cands = descendants(node);
      for (const cand of cands) if (!seen.has(cand) && matchCompound(cand, step.compound)) { seen.add(cand); out.push(cand); }
    }
    current = out;
  }
  return current;
}

export function select(root, selector) {
  if (!selector || !root) return [];
  const groups = parseSelector(String(selector));
  const out = [];
  const seen = new Set();
  const all = allElements(root);
  const order = new Map(all.map((n, i) => [n, i]));
  for (const seq of groups) {
    for (const node of matchSeq(seq, root)) {
      if (!seen.has(node)) { seen.add(node); out.push(node); }
    }
  }
  out.sort((a, b) => (order.get(a) ?? 0) - (order.get(b) ?? 0));
  return out;
}

export function selectFirst(root, selector) {
  return select(root, selector)[0] || null;
}
