// ---------------------------------------------------------------------------
// 来源: 另一 AI 为同一用户做的 tvbox-hub 项目 (lib/charset.mjs, MIT).
// 用户 2026-11-13 决定把评审中值得借鉴的部分并入本项目, 本文件为原样复制
// (仅 http.mjs 去掉了证书失败时不验证重试的逻辑, 见该文件内注释)。
// 上游代码由对方 AI 编写, 本项目仅作复用, 不宣称原创。
// ---------------------------------------------------------------------------
// Charset detection and decoding. Chinese sites still ship GBK/GB2312 bodies
// with a Content-Type that claims nothing useful, so the bytes are kept raw
// until the encoding is known.

const ALIASES = {
  gb2312: 'gbk', gb18030: 'gb18030', 'gb-2312': 'gbk', gbk: 'gbk',
  'x-gbk': 'gbk', big5: 'big5', 'big5-hkscs': 'big5',
  'utf8': 'utf-8', 'utf-8': 'utf-8', 'unicode-1-1-utf-8': 'utf-8',
  'iso-8859-1': 'windows-1252', latin1: 'windows-1252', 'us-ascii': 'windows-1252',
  'shift_jis': 'shift_jis', sjis: 'shift_jis', 'euc-jp': 'euc-jp', 'euc-kr': 'euc-kr',
};

function canonical(name) {
  const n = String(name || '').trim().toLowerCase().replace(/["';]/g, '');
  return ALIASES[n] || n;
}

// Decode a body that the caller already knows is UTF-8 safe, scanning the head
// for a meta charset declaration when the transport did not state one.
export function sniffCharset(bytes, contentType = '') {
  const fromHeader = /charset\s*=\s*([\w-]+)/i.exec(contentType || '');
  if (fromHeader) {
    const c = canonical(fromHeader[1]);
    if (c) return c;
  }
  // Meta tags live in the first couple of KB at most; scan a UTF-8 decode of
  // the head, and fall back to latin1 so byte offsets stay meaningful.
  const head = new Uint8Array(bytes.buffer, bytes.byteOffset, Math.min(bytes.length, 4096));
  let s = '';
  for (let i = 0; i < head.length; i++) s += String.fromCharCode(head[i]);
  const m = /<meta[^>]+charset\s*=\s*["']?\s*([\w-]+)/i.exec(s);
  if (m) return canonical(m[1]);
  const m2 = /<meta[^>]+content\s*=\s*["'][^"']*charset\s*=\s*([\w-]+)/i.exec(s);
  if (m2) return canonical(m2[1]);
  return 'utf-8';
}

export function decodeBytes(bytes, charset) {
  const enc = canonical(charset) || 'utf-8';
  try {
    return new TextDecoder(enc).decode(bytes);
  } catch {
    try { return new TextDecoder('utf-8').decode(bytes); }
    catch { return Buffer.from(bytes).toString('latin1'); }
  }
}

export function decodeBody(bytes, contentType = '', hint = '') {
  if (!bytes || !bytes.length) return '';
  // An explicit charset from the rule / source wins over sniffing.
  const charset = hint ? canonical(hint) : sniffCharset(bytes, contentType);
  let text = decodeBytes(bytes, charset);
  // A wrong guess shows up as replacement characters; retry the other common
  // Chinese encodings before giving up on it.
  if (replacementRatio(text) > 0.004) {
    for (const alt of ['gb18030', 'utf-8', 'big5']) {
      if (alt === charset) continue;
      const t = decodeBytes(bytes, alt);
      if (replacementRatio(t) < replacementRatio(text)) text = t;
    }
  }
  return text;
}

function replacementRatio(s) {
  if (!s) return 0;
  let n = 0;
  for (let i = 0; i < s.length; i++) if (s.charCodeAt(i) === 0xfffd) n++;
  return n / s.length;
}

export function encodeToCharset(text, charset) {
  // Node cannot encode to GBK natively; sources that POST Chinese keywords send
  // percent-encoded bytes, which this covers. Raw GBK bodies are rare and fall
  // back to UTF-8.
  return Buffer.from(String(text), 'utf8');
}
