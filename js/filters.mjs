// ---------------------------------------------------------------------------
// 来源: 另一 AI 为同一用户做的 tvbox-hub 项目 (lib/filters.mjs, MIT).
// 用户 2026-11-13 决定把评审中值得借鉴的部分并入本项目, 本文件为原样复制
// (仅 http.mjs 去掉了证书失败时不验证重试的逻辑, 见该文件内注释)。
// 上游代码由对方 AI 编写, 本项目仅作复用, 不宣称原创。
// ---------------------------------------------------------------------------
// Shared content filters: adult-source detection, shield/captcha detection, and
// placeholder-text detection. Extracted so TVBox and book-source crawls agree on
// what counts as adult rather than each keeping its own drifting list.
//
// The `av` lesson: a bare keyword matched `navi资源` / `save资源`, so Latin
// keywords use letter boundaries.

// TVBox line names: adult/pirate video labels.
export const TVBOX_ADULT_RE = /偷拍|裙底|黑料|大奶|杏吧|麻豆|色猫|奶香|色情|情趣|探花|楼凤|野模|禁漫|下海|爆乳|成人|番号|里番|无码|有码|加勒比|一本道|东京热|色逼|老色|色欲|福利姬|约啪|援交|裸聊|情色|三级片|avbt|(?<![a-z])av(?![a-z])|souav|heiliao|danaizi|hlzy|xiuren|meitui/i;

// Book sources: the 🔞 marker plus the vocabulary the yck2026 listings use for
// 18禁 sources. Kept narrower than the TVBox list because book-source names
// legitimately contain 言情/耽美, which are not themselves adult markers.
export const NOVEL_ADULT_RE = new RegExp([
  '🔞',
  '海棠', 'po18', 'p18', 'popo', 'popo原创', '御宅屋', '肉文', '肉宅', '肉屋', '肉肉',
  '禁忌书屋', '第一版主', '版主', '禁忌', '色情', '黄色', '成人', '里番', '本子',
  '18禁', '十八禁', 'h文', '高h', 'r18', 'r-18', 'ノベル', '绅士', '福利',
  '艳情', '情色', '情欲小说', '色欲', '色中色', '色书屋', '色窝', '涩涩', '污文', '小黄文', '黄书',
  'ntr', '绿帽', '乱伦', '调教', '凌辱', '兽交', '幼交', '痴汉', '母狗', '淫',
  'xanimu', 'hanime', 'avmoo', 'sehuatang', 'souav', '98tang', 'jiuse', 'tt1069',
  // Latin keywords need boundaries so 'av' does not hit 'save'/'navi'.
  '(?<![a-z])av(?![a-z])', '(?<![a-z])porn(?![a-z])', '(?<![a-z])hentai(?![a-z])',
  '(?<![a-z])adult(?![a-z])', '(?<![a-z])xxx(?![a-z])',
].join('|'), 'i');

// Fields a source can hide an adult marker in.
export function adultHaystack(src) {
  return [
    src.bookSourceName, src.bookSourceGroup, src.bookSourceComment, src.bookSourceUrl,
    exploreTitles(src),
  ].filter(Boolean).join(' ').toLowerCase();
}

// exploreUrl holds a JSON-ish array of {title,url}; the titles are the closest
// thing the platform has to genre labels, so they belong in the haystack.
export function exploreTitles(src) {
  const raw = src && src.exploreUrl;
  if (!raw) return '';
  const s = String(raw);
  const titles = [];
  for (const m of s.matchAll(/['"]title['"]\s*:\s*['"]([^'"]+)['"]/g)) titles.push(m[1]);
  // Unquoted-key variant: {title:'女频',url:'...'}
  for (const m of s.matchAll(/title\s*:\s*['"]([^'"]+)['"]/g)) titles.push(m[1]);
  return [...new Set(titles)].join(' ');
}

export function isAdultSource(src) {
  return NOVEL_ADULT_RE.test(adultHaystack(src));
}

// ---------------------------------------------------------------------------
// Shield / captcha detection. The user's complaint was that sources behind
// Cloudflare or a captcha were never actually tested, so a search that returns a
// challenge page looked healthy.

const SHIELD_BODY_RE = /Just a moment|cf-challenge|__cf_chl|Attention Required|Checking your browser|Enable JavaScript and cookies to continue|challenge-platform|cf_chl_opt|请开启\s*JavaScript|安全验证|人机验证|滑动验证|滑块验证|请完成验证/i;

const CAPTCHA_BODY_RE = /验证码|captcha|recaptcha|geetest|hcaptcha|turnstile|verify yourself|请输入验证码|点击验证|拖动滑块|行为验证/i;

// A challenge page is small and contains no book-like content; a real page that
// merely mentions 验证码 in a comment should not be rejected, so a body match
// only counts when the page is also nearly empty.
export function detectShield({ status, headers = {}, body = '' }) {
  const server = String(headers.server || '').toLowerCase();
  const text = String(body || '');
  const len = text.length;
  const hasChallenge = SHIELD_BODY_RE.test(text);
  const hasCaptcha = CAPTCHA_BODY_RE.test(text);

  if (status === 403 || status === 429 || status === 503) {
    if (hasChallenge || hasCaptcha || server.includes('cloudflare')) {
      return { shielded: true, kind: len < 20000 && hasCaptcha ? 'captcha' : 'shield', reason: `HTTP ${status} ${server}` };
    }
  }
  // Cloudflare's interstitial is served with 403/503 and a tiny body.
  if (hasChallenge && len < 60000) return { shielded: true, kind: 'shield', reason: 'challenge-page' };
  if (hasCaptcha && len < 20000 && !/[<>]/.test(text.slice(0, 400)) === false) {
    // Only treat a captcha page as a shield when it is a full page rather than a
    // chapter that happens to mention a captcha in an ad.
    const looksLikePage = /<html|<body|<div/i.test(text);
    if (looksLikePage && text.length < 20000) return { shielded: true, kind: 'captcha', reason: 'captcha-page' };
  }
  return { shielded: false };
}

// ---------------------------------------------------------------------------
// Placeholder / incomplete content. "章节点开内容不全" shows up as an app
// download prompt, an end-of-preview notice, or an ad stub where the prose
// should be.

const PLACEHOLDER_RE = /请下载\s*APP|下载APP|APP阅读|请安装客户端|试读结束|本章未完|继续阅读请|登陆后阅读|登录后阅读|请先登录|需要登录|开通VIP|购买本章|余额不足|章节内容获取失败|获取失败|内容加载中|正在加载|广告|本站域名|备用域名|请记住本站|最新章节请|无弹窗|手机用户请|建议使用.*浏览器|检测到.*(?:广告|插件)/i;

export function looksLikePlaceholder(text) {
  const t = String(text || '').trim();
  if (!t) return true;
  if (t.length < 40) return true;
  // A short body that is mostly a notice rather than prose.
  if (t.length < 200 && PLACEHOLDER_RE.test(t)) return true;
  return false;
}

export function hasNextPageHint(text) {
  return /下一页|下页|继续阅读|本章未完|未完待续|翻页/i.test(String(text || ''));
}

// Chinese character ratio: real chapter prose is dense in Han characters, while
// navigation, captchas and ad copy are not.
export function hanRatio(text) {
  const t = String(text || '');
  if (!t) return 0;
  let han = 0, total = 0;
  for (const ch of t) {
    const c = ch.codePointAt(0);
    if (c <= 32) continue;
    total++;
    if (c >= 0x4e00 && c <= 0x9fff) han++;
  }
  return total ? han / total : 0;
}
