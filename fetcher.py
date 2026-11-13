#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TVBox + Legado 书源聚合抓取器 v2 — 质量优先, 宁缺毋滥, 多轮验证
- TVBox: 最终只留 80-150 站 (目标 120, 约 70% 热门 + 30% 偏门), 不是全量
- 书源: 最终只留 100-200 个 (目标 150, 约 70% 热门 + 30% 偏门)
- TVBox 实测: 每个候选站 api 做两轮测速 (TTFB/总耗时/200KB 内带宽/能否返回 vod 列表),
  多通道对比 (配置 JSON: jsDelivr -> ghproxy -> raw; 站点 api: 直连为主,
  若为 GitHub/jsDelivr 托管则另试镜像取最快), 门槛 TTFB<1.5s、总耗时<3s
- 书源抽查: 规则齐全 + 官网可打开 + 3 本热门书搜索实测 (斗破苍穹/凡人修仙传/庆余年),
  2 本以上通过才算"搜得全", 另做第二轮复测确认
- 大陆速度未实测: 本机在新加坡, 用"新加坡 VM 实测 + 国内 CDN 启发式",
  部署后需大陆实测 (方法见 README"筛选标准"与 itdog/boce/17ce 思路)
- 只输出真实抓到的数据, 抓不到/测不过的上游如实记录在 meta.json, 不编造、不填充
"""
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urljoin, urlparse, quote_plus

import requests

TIMEOUT = 12
HEADERS = {"User-Agent": "Mozilla/5.0 (TVBox-Legado-Fetcher/1.0)"}
MAX_TVBOX_SITES = 120   # 目标 120, 范围 80-150
MIN_TVBOX_SITES = 80
MAX_BOOK_SOURCES = 150  # 目标 150, 范围 100-200
MIN_BOOK_SOURCES = 100
# TVBox 测速门槛 (建议值, 见 README"筛选标准")
TTFB_LIMIT_MS = 1500
TOTAL_LIMIT_MS = 3000
SPEED_SAMPLE_MAX_BYTES = 200 * 1024  # 测带宽最多下 200KB

# ---------------------------------------------------------------- 工具
def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _log(progress, msg):
    print(msg, flush=True)
    if progress:
        try:
            progress(msg)
        except Exception:
            pass


def github_channels(owner, repo, branch, path):
    """jsDelivr -> ghproxy -> raw, 中文路径按段 URL 编码。"""
    enc = quote(path, safe="/")
    return [
        f"https://cdn.jsdelivr.net/gh/{owner}/{repo}@{branch}/{enc}",
        f"https://ghproxy.net/https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{enc}",
        f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{enc}",
    ]


def fetch_text(urls, progress=None, label=""):
    """按顺序尝试多个 URL, 返回 (text, used_url), 全部失败返回 (None, None)。"""
    last_err = ""
    for url in urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 200 and r.text.strip():
                return r.text, url
            last_err = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
        _log(progress, f"  尝试失败 {url} -> {last_err}")
    return None, None


def strip_leading_comment_lines(text):
    """剥离行首 // 注释行 (只处理行首是 // 的行, 不动字符串里的 http://)。"""
    out = []
    for line in text.splitlines():
        if line.lstrip().startswith("//"):
            continue
        out.append(line)
    return "\n".join(out)


def base_dir_of(url):
    return url.rsplit("/", 1)[0] + "/"


def resolve_relative(value, base_dir):
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if v.startswith(("http://", "https://")):
        return v
    if v.startswith(("./", "../")) or ("/" in v and not v.startswith("/")):
        try:
            return urljoin(base_dir, v)
        except Exception:
            return None
    if v.startswith("/"):
        try:
            p = urlparse(base_dir)
            return f"{p.scheme}://{p.netloc}{v}"
        except Exception:
            return None
    return v


# ---------------------------------------------------------------- 分类: 热门 / 偏门
# TVBox 偏门关键词 (按 key+name+group+categories 匹配, 命中即偏门)
TVBOX_NICHE_KEYWORDS = [
    ("纪录片", ["纪录片", "记录片", "documentary", "国家地理", "BBC", "discovery"]),
    ("体育", ["体育", "看球", "赛事", "足球", "篮球", "体育赛事"]),
    ("海外剧", ["海外剧", "欧美剧", "美剧", "英剧", "海外"]),
    ("日韩", ["日剧", "韩剧", "日本剧", "韩国剧", "日韩", "日本", "韩国"]),
    ("港台", ["港剧", "台剧", "香港剧", "台湾剧", "港台", "TVB", "香港", "台湾"]),
    ("经典老片", ["经典", "老片", "怀旧", "经典片"]),
    ("戏曲", ["戏曲", "京剧", "越剧", "黄梅戏", "豫剧"]),
    ("少儿", ["少儿", "儿童", "少儿教育", "卡通"]),
    ("动漫", ["动漫", "动画", "番剧", "二次元", "国产动漫", "日本动漫", "欧美动漫"]),
    ("4K", ["4K", "蓝光", "原画", "杜比", "8K"]),
    ("地方台/直播", ["地方台", "卫视", "央视", "直播", "地方"]),
]
POPULAR_TVBOX_KEYWORDS = ["综合", "采集", "影视", "电影", "电视"]


def classify_tvbox(site):
    """返回 (category, niche_tag): category 为 热门/偏门, niche_tag 为偏门类型或 ''。
    只看 key/name/group; categories 只有在列表很窄 (<=5 个, 说明该站专注某类)
    时才作为依据——否则几乎每个 CMS 站的 categories 都含"纪录片/海外剧"等词,
    会把所有综合站都误判成偏门 (v2 第一轮实测曾因此误判, 已修正)。"""
    hay = " ".join([str(site.get("key", "")), str(site.get("name", "")),
                    str(site.get("group", ""))])
    low = hay.lower()
    for tag, kws in TVBOX_NICHE_KEYWORDS:
        if any(k.lower() in low for k in kws):
            return "偏门", tag
    cats = [str(c) for c in (site.get("categories") or [])]
    if 0 < len(cats) <= 5:
        cat_hay = " ".join(cats).lower()
        for tag, kws in TVBOX_NICHE_KEYWORDS:
            if any(k.lower() in cat_hay for k in kws):
                return "偏门", tag
    return "热门", ""


# 书源偏门关键词 (按 name+group+url 匹配)
BOOK_NICHE_KEYWORDS = [
    ("轻小说", ["轻小说", "轻文", "sfacg", "刺猬猫", "菠萝包", "哔哩轻", "bilibili轻"]),
    ("二次元/同人", ["二次元", "同人", "乙女", "百合", "耽美", "lofter", "ao3"]),
    ("武侠/仙侠", ["武侠", "仙侠", "修真"]),
    ("科幻", ["科幻", "末世", "星际"]),
    ("悬疑/推理", ["悬疑", "推理", "惊悚", "灵异", "盗墓"]),
    ("历史/军事", ["历史", "军事", "地方志", "方志"]),
    ("言情/女频", ["言情", "女频", "晋江", "潇湘", "红袖"]),
    ("玄幻/奇幻", ["玄幻", "奇幻"]),
    ("儿童/少儿", ["儿童", "少儿", "童话"]),
    ("体育/游戏", ["体育", "电竞", "游戏"]),
]
POPULAR_BOOK_KEYWORDS = ["番茄", "起点", "七猫", "笔趣阁", "纵横", "掌阅", "咪咕", "17k", "顶点"]


def classify_book(item):
    hay = f"{item.get('bookSourceName','')} {item.get('bookSourceGroup','')} {item.get('bookSourceUrl','')}"
    low = hay.lower()
    for tag, kws in BOOK_NICHE_KEYWORDS:
        if any(k.lower() in low for k in kws):
            return "偏门", tag
    return "热门", ""


def domestic_bonus(host):
    """国内 CDN/域名加分: jsDelivr 国内有节点、.cn 域名、常见国内云域名。"""
    h = (host or "").lower()
    if "cdn.jsdelivr.net" in h:
        return True
    if h.endswith(".cn") or ".cn:" in h:
        return True
    if any(d in h for d in ["aliyuncs.com", "alicdn.com", "myqcloud.com", "qcloud.com",
                            "baidubce.com", "bcebos.com", "hwclouds.com", "upcdn.net"]):
        return True
    return False


# ---------------------------------------------------------------- TVBox
TVBOX_SOURCES = [
    {"name": "hebijunge/tvbox-config tvbox_recommended.json",
     "owner": "hebijunge", "repo": "tvbox-config", "branch": "main",
     "path": "tvbox_recommended.json",
     "extra": ["https://cdn.jsdelivr.net/gh/hebijunge/tvbox-config@main/exports/healthy.json"]},
    {"name": "TVboxorg/TVbox dist/official.json",
     "owner": "TVboxorg", "repo": "TVbox", "branch": "main",
     "path": "dist/official.json", "extra": []},
    {"name": "qist/tvbox jsm.json",
     "owner": "qist", "repo": "tvbox", "branch": "master",
     "path": "jsm.json", "extra": ["https://qist.wyfc.qzz.io/jsm.json"]},
    {"name": "Lightconer/tvbox-ysc-config 单仓聚合.json",
     "owner": "Lightconer", "repo": "tvbox-ysc-config", "branch": "main",
     "path": "output/单仓聚合.json", "extra": []},
    {"name": "cluntop/tvbox box.json",
     "owner": "cluntop", "repo": "tvbox", "branch": "main",
     "path": "box.json", "extra": ["https://clun.top/box.json"]},
    {"name": "gaotianliuyun/gao js.json",
     "owner": "gaotianliuyun", "repo": "gao", "branch": "master",
     "path": "js.json", "extra": []},
    # --- 偏门/小众补充上游 (2026-11-11 实测可打开, 非研究报告原清单, 见 README) ---
    {"name": "25175/tvyuan tvbox.json (每小时自动聚合, 站名自带测速标注)",
     "owner": "25175", "repo": "tvyuan", "branch": "master",
     "path": "tvbox.json", "extra": []},
    {"name": "102244653/tvbox-resource dianshi.json (298 站, 纪录片/日韩/港台/动漫分类全)",
     "owner": "102244653", "repo": "tvbox-resource", "branch": "master",
     "path": "dianshi.json", "extra": []},
    {"name": "jinenge/tvbox tvbox.json (行首 // 注释, 含 Bilibili/MTV 纪录片/演唱会)",
     "owner": "jinenge", "repo": "tvbox", "branch": "main",
     "path": "tvbox.json", "extra": ["https://jinenge.us.kg/app/tvbox/tvbox.json"]},
    {"name": "niubihu1/tvbox 1.json (4K/体育/直播向, type=3 站会被过滤)",
     "owner": "niubihu1", "repo": "tvbox", "branch": "main",
     "path": "1.json", "extra": []},
]

LIVES_SOURCE = {"name": "Lightconer/TVBox-Sources lives",
                "owner": "Lightconer", "repo": "TVBox-Sources", "branch": "main",
                "path": "output/tvbox.json"}


def _strip_md5_suffix(value):
    """剥掉 ';md5;<hash>' 后缀 (如 './jar/spider.jar;md5;abc' -> './jar/spider.jar')。
    只剥第一个 ';' 之后的部分; 无 ';' 则原样返回。"""
    if not isinstance(value, str):
        return value
    v = value.strip()
    if ";" in v:
        first = v.split(";", 1)[0].strip()
        if first:
            return first
    return v


def _clean_spider_value(sp, base_dir):
    """修复 spider 判断 (2026-11-11 修复):
    9/10 上游的 spider 是 './jar/spider.jar;md5;<hash>' 或 'http://...jpg;md5;<hash>'
    (jpg/png 伪装的 jar), 旧代码只认恰好 .jar 结尾, 导致只有 cluntop 的 plain
    './jar/xs.jar' 能入选, 其余全丢。修法: 先剥 ';md5;...' 后缀再判断,
    相对路径按 base 解析; .jar 结尾的保留, .jpg/.jpeg/.png 伪装的原样保留
    (TVBox 接受它当 jar), 带 md5 后缀的其它后缀 (如 fan.txt) 也保留
    (有 md5 即说明上游把它当 jar 用)。"""
    if not isinstance(sp, str) or not sp.strip():
        return None
    raw = sp.strip()
    had_md5 = ";md5;" in raw.lower() or ";md5" in raw.lower()
    stripped = _strip_md5_suffix(raw)
    resolved = resolve_relative(stripped, base_dir)
    if not resolved:
        return None
    low = resolved.lower().split("?")[0]
    if low.endswith((".jar", ".jpg", ".jpeg", ".png")):
        return resolved
    if had_md5:
        return resolved
    return None


def _check_url_ok(url, timeout=8):
    """HEAD 优先, 405/403 等回退 GET (只读 1 字节即关), 200-399 算通。
    用于 spider jar / type-3 api js/py / ext URL 的可达性校验, 不是测速。"""
    if not url or not isinstance(url, str):
        return False
    if not url.startswith(("http://", "https://")):
        return False
    try:
        r = requests.head(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if 200 <= r.status_code < 400:
            return True
        # HEAD 被拒时回退 GET (部分 CDN/代理禁 HEAD)
        if r.status_code in (400, 403, 405, 501):
            pass
        else:
            # 其它 4xx/5xx 也试一次 GET, 有的站 HEAD 返回 404 但 GET 正常
            pass
    except Exception:
        pass
    try:
        with requests.get(url, headers=HEADERS, timeout=timeout, stream=True) as r:
            if 200 <= r.status_code < 400:
                try:
                    next(r.iter_content(chunk_size=1024), None)
                except Exception:
                    pass
                return True
            return False
    except Exception:
        return False


# ---------------------------------------------------------------- TVBox 成人过滤
# TVBox 成人关键词表 (2026-11-11 修复: 此前只有 TVBOX_ADULT_KEYWORDS 且只在
# _is_tool_site 里查 name, 没查 key/api, 也没覆盖全部 TVBox 站, 导致 3 个成人向
# type-3 站混进了最终 120 站。本次剔除的 3 个: 3d涩动漫.py、91动漫.py、Cam4直播.py。
# 关键词表:
#   强匹配 (子串, key/name/api/ext 任一命中即成人):
#     涩、色情、成人、黄片、黄文、四级、fullerotic、erotic、porn、情色、毛片、
#     三级片、18禁、裸聊、cam4、91动漫
#   词边界匹配 (正则, 防误删): av、sex、xxx —— 必须是独立词, 前后不能是字母数字。
#     例: kxgav.com 里的 "av" 前面是 g, 不算命中, 白嫖等正常站不受影响;
#     "AV影院"、"/av/"、"av_" 才算命中。裸子串查 "av" 会误删, 已验证, 不要改回子串。
#   谨慎词: 老片 —— 绝不单独过滤, 只有与强词/词边界词同现时才算成人, 而同现时
#     上面的强词检查已经返回 True, 所以它不需要单独的判断分支, 只列在
#     TVBOX_ADULT_CAUTIOUS_KEYWORDS 里让关键词表完整、可审计。
#     例: 1905"经典影视" (niche_tag 经典老片) 等正常老片站不受影响, 本次未剔除。
# 作用范围: 全部 TVBox 站 (type-0/1/3/4, 去重后、测速前就过滤) + type-3 挑选
# (二次兜底) + 最终写文件前复核, 并过滤 lives 里的成人向频道名。书源侧已有
# ADULT_KEYWORDS 过滤, 不受本次改动影响。
TVBOX_ADULT_STRONG_KEYWORDS = [
    "涩", "色情", "成人", "黄片", "黄文", "四级",
    "fullerotic", "erotic", "porn", "情色", "毛片", "三级片", "18禁", "裸聊",
    "cam4", "91动漫",
]
TVBOX_ADULT_TOKEN_RE = re.compile(r"(?<![a-z0-9])(av|sex|xxx)(?![a-z0-9])", re.IGNORECASE)
TVBOX_ADULT_CAUTIOUS_KEYWORDS = ["老片"]  # 仅作审计清单, 不单独作为过滤条件
# 本次剔除的 3 个成人向站 (key):
TVBOX_ADULT_REMOVED_KNOWN = ["3d涩动漫.py", "91动漫.py", "Cam4直播.py"]


def _is_adult_tvbox(site):
    """判断一个 TVBox 站是否成人向。返回 (是否成人, 原因或 None)。
    查 key + name + api + ext (ext 为 dict 时转成字符串再查)。
    强词子串命中、词边界命中, 任一成立即成人; "老片" 单独出现不算成人。"""
    key = str(site.get("key", ""))
    name = str(site.get("name", ""))
    api = str(site.get("api", ""))
    ext = site.get("ext", "")
    ext_s = ext if isinstance(ext, str) else (json.dumps(ext, ensure_ascii=False) if ext else "")
    hay = " ".join([key, name, api, ext_s])
    hay_low = hay.lower()
    for kw in TVBOX_ADULT_STRONG_KEYWORDS:
        if kw in hay_low:
            return True, f"命中成人关键词 '{kw}'"
    m = TVBOX_ADULT_TOKEN_RE.search(hay)
    if m:
        return True, f"命中成人词边界 '{m.group(0)}'"
    return False, None


# type-3 工具类 key: 配置中心/本地/豆瓣等不是视频源, 必须剔除
TVBOX_TOOL_KEYS = {"config", "csp_config", "local", "local-2", "douban", "豆瓣",
                   "配置中心", "本地", "csp_douban", "csp_localfile", "csp_local"}
TVBOX_TOOL_APIS = {"csp_config", "csp_douban", "csp_localfile", "csp_local", "csp_ygp"}


def _is_tool_site(site):
    """工具类站: key 或 api 是配置中心/本地/豆瓣, 或 name 就是配置中心/本地/豆瓣。"""
    key = str(site.get("key", "")).strip()
    name = str(site.get("name", "")).strip()
    api = str(site.get("api", "")).strip()
    if key.lower() in TVBOX_TOOL_KEYS:
        return True
    if api in TVBOX_TOOL_APIS:
        # api 是工具类且 key/name 也指向工具 (避免误删名字含豆瓣的正常站)
        if any(t in name for t in ("配置", "本地", "豆瓣")) or key.lower() in TVBOX_TOOL_KEYS:
            return True
        if key in ("配置中心", "本地", "豆瓣", "豆瓣预告"):
            return True
    if name in ("配置中心", "本地", "豆瓣", "配置 • 中心"):
        return True
    # 成人站不由这里判断: 统一走 _is_adult_tvbox, 在去重后、type-3 挑选、
    # 最终复核三处显式过滤并记录 (见 fetch_tvbox), 避免工具类和成人向混在一条记录里。
    return False


def _parse_type3_api(api_raw, base_dir):
    """解析 type-3 的 api, 返回 (api_value, kind) 或 (None, None)。
    允许: csp_ 类名 / py_ 模块 (裸标识, 不解析成 URL)、./lib/*.js (drpy)、
    ./py/*.py、http(s) 的 .js/.py/.jar。旧代码要求 api 必须是 http 且以 .jar
    结尾, 把 3,082 个 type-3 站全丢了, 这里不再要求 .jar。"""
    if not isinstance(api_raw, str):
        return None, None
    a = api_raw.strip()
    if not a:
        return None, None
    low = a.lower()
    if a.startswith(("http://", "https://")):
        return a, "url"
    if a.startswith(("csp_", "py_", "csp-", "py-")) or low.startswith(("csp", "py_")):
        # 裸标识: csp_XBPQ / py_cctv_full / csp_App88 等, 保持原样 (TVBox 靠 spider jar 解析它)
        return a, "id"
    if a.startswith(("./", "../", "/")) or low.endswith((".js", ".py", ".jar")) or "/" in a:
        resolved = resolve_relative(a, base_dir)
        if resolved and resolved.startswith(("http://", "https://")):
            return resolved, "url"
        return None, None
    return None, None


def _parse_tvbox_obj(obj, base_dir, progress=None):
    sites, parses, lives, doh, rules = [], [], [], [], []
    spider = None
    if not isinstance(obj, dict):
        return sites, parses, lives, spider, doh, rules
    if isinstance(obj.get("spider"), str):
        spider = _clean_spider_value(obj["spider"], base_dir)
    for key in ("doh", "rules"):
        vals = obj.get(key)
        if isinstance(vals, list):
            (doh if key == "doh" else rules).extend(vals)
    for p in obj.get("parses") or []:
        if isinstance(p, dict) and p.get("url"):
            parses.append(p)
    for lv in obj.get("lives") or []:
        if isinstance(lv, dict) and lv.get("url"):
            lives.append(lv)
    for s in obj.get("sites") or []:
        if not isinstance(s, dict):
            continue
        key = s.get("key")
        api = s.get("api")
        if not key or not api or not isinstance(api, str):
            continue
        try:
            stype = int(s.get("type", 0))
        except Exception:
            stype = 0
        ns = dict(s)
        ns["key"] = str(key)
        ns["type"] = stype
        if stype == 3:
            # 修复: type-3 不再要求 api 是 http jar。保留 csp_ 类名 / py_ 模块 /
            # drpy JS / .py/.jar, 校验改为 spider jar + ext 可达性 (见 fetch_tvbox
            # 的 _validate_type3), 不做单站 HTTP 延迟测试。工具类站先留着,
            # 由 fetch_tvbox 的 _is_tool_site 统一剔除。
            api_val, api_kind = _parse_type3_api(api, base_dir)
            if not api_val:
                continue
            ns["api"] = api_val
            ns["_api_kind"] = api_kind
            ns["_spider"] = spider
            ns["_base_dir"] = base_dir
        else:
            api_abs = resolve_relative(api, base_dir)
            if not api_abs or not api_abs.startswith(("http://", "https://")):
                continue
            ns["api"] = api_abs
        ext = s.get("ext")
        if isinstance(ext, str) and ext.strip().startswith(("./", "../")):
            resolved = resolve_relative(ext, base_dir)
            if resolved:
                ns["ext"] = resolved
        sites.append(ns)
    return sites, parses, lives, spider, doh, rules


def _fetch_one_tvbox_json(urls, progress=None, label=""):
    text, used = fetch_text(urls, progress=progress, label=label)
    if text is None:
        return None, None
    cleaned = strip_leading_comment_lines(text)
    try:
        obj = json.loads(cleaned)
    except Exception:  # noqa: BLE001
        # 有些文件 (如 jinenge) 在第一个 JSON 对象后还附了另一段配置/垃圾,
        # json.loads 会报 Extra data; 用 raw_decode 只取第一个对象
        try:
            dec = json.JSONDecoder()
            obj, _end = dec.raw_decode(cleaned.lstrip())
            _log(progress, f"  注意 {used}: 文件含尾部多余内容, 只取第一个 JSON 对象")
        except Exception as e2:  # noqa: BLE001
            _log(progress, f"  JSON 解析失败 {used}: {e2}")
            return None, None
    return obj, used


def _tvbox_test_url(api):
    """CMS 站测 ?ac=list, 其他测首页。"""
    if "ac=" in api:
        return api
    if "/provide/vod" in api or "api.php" in api:
        sep = "&" if "?" in api else "?"
        return api + sep + "ac=list"
    return api


def _speed_test_one(site):
    """单站实测一轮: TTFB、总耗时、200KB 内带宽、能否返回 vod 列表。
    返回 dict, 失败时 ok=False 并给原因。"""
    api = site["api"]
    url = _tvbox_test_url(api)
    host = urlparse(url).netloc
    t0 = time.monotonic()
    try:
        with requests.get(url, headers=HEADERS, timeout=5, stream=True) as r:
            if r.status_code != 200:
                return {"key": site["key"], "api": api, "api_host": host, "ok": False,
                        "ttfb_ms": None, "total_ms": int((time.monotonic() - t0) * 1000),
                        "kbps": 0, "channel": "direct", "domestic_cdn": domestic_bonus(host),
                        "vod_ok": False, "error": f"HTTP {r.status_code}",
                        "checked_at": now_iso()}
            first = True
            ttfb_ms = None
            nbytes = 0
            chunks = []
            for chunk in r.iter_content(chunk_size=8192):
                if first:
                    ttfb_ms = int((time.monotonic() - t0) * 1000)
                    first = False
                if chunk:
                    nbytes += len(chunk)
                    if len(chunks) < 5:
                        chunks.append(chunk)
                if nbytes >= SPEED_SAMPLE_MAX_BYTES:
                    break
            total_ms = int((time.monotonic() - t0) * 1000)
            if ttfb_ms is None:
                ttfb_ms = total_ms
            text_head = b"".join(chunks).decode("utf-8", errors="ignore").lower()
            vod_ok = any(t in text_head for t in ['"list"', '"class"', '"vod', '"code":1',
                                                   "vod_name", "vod_id", "<rss", "list"])
            # 首页 fallback: 200 且有内容也算通, 但 vod_ok 为 False 时不算"能返回 vod 列表"
            if "/provide/vod" not in api and "api.php" not in api:
                vod_ok = nbytes > 200
            kbps = round((nbytes / 1024) / max(total_ms / 1000, 0.01), 1) if total_ms else 0
            ok = (ttfb_ms < TTFB_LIMIT_MS and total_ms < TOTAL_LIMIT_MS and vod_ok)
            return {"key": site["key"], "api": api, "api_host": host, "ok": ok,
                    "ttfb_ms": ttfb_ms, "total_ms": total_ms, "kbps": kbps,
                    "sample_bytes": nbytes,
                    "channel": "direct", "domestic_cdn": domestic_bonus(host),
                    "vod_ok": vod_ok, "error": None if ok else "未达门槛或无 vod 列表",
                    "checked_at": now_iso()}
    except Exception as e:  # noqa: BLE001
        return {"key": site["key"], "api": api, "api_host": host, "ok": False,
                "ttfb_ms": None, "total_ms": int((time.monotonic() - t0) * 1000),
                "kbps": 0, "channel": "direct", "domestic_cdn": domestic_bonus(host),
                "vod_ok": False, "error": f"{type(e).__name__}: {e}",
                "checked_at": now_iso()}


def _speed_score(rec):
    """排序分: 越低越好。TTFB 权重最高, 再看总耗时, 带宽越低越差。
    国内 CDN 加分 (减 200 分, 相当于约 200ms 优势)。"""
    ttfb = rec.get("ttfb_ms") or 9999
    total = rec.get("total_ms") or 9999
    kbps = rec.get("kbps") or 0
    score = ttfb * 2 + total + max(0, 500 - kbps)
    if rec.get("domestic_cdn"):
        score -= 200
    return score


def _spider_channels(spider_url):
    """spider jar 的多通道候选: 原链 -> raw GitHub -> 双重编码 raw。
    原因 (实测): jsDelivr 对 .jar 一律返回 403 Forbidden (与 UA 无关,
    是 jsDelivr 不提供 .jar 文件), 而 raw.githubusercontent.com 对同一文件
    返回 200 (qist spider.jar 1.9MB、cluntop xs.jar 2.0MB 均实测)。
    hebijunge 的目录名本身就是 '%E4%B8%80...' 这样的字面百分号串, raw 需把
    '%' 再编码成 '%25' 才能拿到文件 (单编码 raw 404, 双编码 raw 200)。
    所以只测原链会把所有 jsDelivr jar 都误判为坏, 必须多通道。"""
    if not spider_url or not isinstance(spider_url, str):
        return []
    cands = [spider_url]
    raw = None
    if "cdn.jsdelivr.net/gh/" in spider_url:
        try:
            rest = spider_url.split("cdn.jsdelivr.net/gh/", 1)[1]
            owner_repo, branch_path = rest.split("@", 1)
            branch, path = branch_path.split("/", 1)
            raw = f"https://raw.githubusercontent.com/{owner_repo}/{branch}/{path}"
        except Exception:
            raw = None
    elif spider_url.startswith("https://gh-proxy.com/https://raw.githubusercontent.com/"):
        raw = spider_url.split("https://gh-proxy.com/", 1)[1]
    elif spider_url.startswith("https://ghproxy.net/https://raw.githubusercontent.com/"):
        raw = spider_url.split("https://ghproxy.net/", 1)[1]
    elif spider_url.startswith("https://raw.githubusercontent.com/"):
        raw = spider_url
    if raw and raw not in cands:
        cands.append(raw)
        # 双重编码: 路径里的 '%' -> '%25' (hebijunge 字面百分号目录名)
        try:
            head, _, path = raw.partition("raw.githubusercontent.com/")
            # path 形如 owner/repo/branch/a/b/c, 只对 branch 之后的部分编码 '%'
            parts = path.split("/", 3)
            if len(parts) == 4:
                dbl = parts[0] + "/" + parts[1] + "/" + parts[2] + "/" + parts[3].replace("%", "%25")
                dbl_url = "https://raw.githubusercontent.com/" + dbl
                if dbl_url not in cands:
                    cands.append(dbl_url)
        except Exception:
            pass
    return cands


def _check_spider_ok(spider_url):
    """多通道测 spider jar, 返回 (ok, working_url_or_None)。
    任一通道 200 即算通, working_url 是实际能打开的那条 (输出 spider 用它,
    而不是 403 的 jsDelivr 原链, 否则 TVBox 里也打不开)。"""
    for cand in _spider_channels(spider_url):
        if _check_url_ok(cand):
            return True, cand
    return False, None


def _validate_type3_one(site, spider_cache):
    """type-3 单站校验 (不做单站 HTTP 延迟测试, 只做 jar/ext 可达性):
    (a) 该上游的 spider jar HEAD/GET 200 (剥 md5 后测, 多通道: 原链/raw/双编码 raw,
        因 jsDelivr 对 .jar 返回 403, 只测原链会误判);
    (b) api 若是 http URL (drpy .js / .py / .jar) 则 HEAD/GET 能打开,
        若是 csp_/py_ 裸标识则跳过 (靠 spider jar 解析);
    (c) ext 若是 http URL 则 HEAD/GET 能打开, 相对路径/dict/None 跳过。
    返回 (ok, record)。spider 按 URL 缓存, 避免同一 jar 反复请求。"""
    key = site["key"]
    spider_url = site.get("_spider")
    api = site.get("api", "")
    api_kind = site.get("_api_kind", "id")
    cat, tag = classify_tvbox(site)
    if spider_url in spider_cache:
        spider_ok, spider_working = spider_cache[spider_url]
    else:
        spider_ok, spider_working = _check_spider_ok(spider_url) if spider_url else (False, None)
        spider_cache[spider_url] = (spider_ok, spider_working)
    if api_kind == "url":
        api_ok = _check_url_ok(api)
        api_check = "url_head_get"
    else:
        api_ok = True
        api_check = "id_skipped_spider_covers"
    ext = site.get("ext")
    ext_check = "skipped"
    ext_ok = True
    if isinstance(ext, str) and ext.strip().startswith(("http://", "https://")):
        ext_check = "url_head_get"
        ext_ok = _check_url_ok(ext.strip())
    ok = bool(spider_ok and api_ok and ext_ok)
    rec = {"key": key, "name": site.get("name", ""), "api": api,
           "api_kind": api_kind, "category": cat, "niche_tag": tag,
           "spider": spider_url, "spider_working": spider_working,
           "spider_ok": spider_ok,
           "api_ok": api_ok, "api_check": api_check,
           "ext_ok": ext_ok, "ext_check": ext_check,
           "ok": ok, "checked_at": now_iso(),
           "note": "type-3 未做单站 HTTP 延迟测试, 仅做 jar/ext 可达性校验"}
    return ok, rec


# type-3 偏门新增配额 (合计 34, 加原有 2 个偏门共 36, 占 120 的 30%)
TYPE3_NICHE_QUOTA = {"4K": 5, "动漫": 6, "地方台/直播": 5, "体育": 4,
                     "少儿": 3, "海外剧": 3, "日韩": 2, "港台": 2,
                     "戏曲": 2, "经典老片": 1, "纪录片": 1}


def fetch_tvbox(data_dir, progress=None):
    out_dir = os.path.join(data_dir, "tvbox")
    os.makedirs(out_dir, exist_ok=True)
    all_sites, all_parses, all_lives = [], [], []
    all_doh, all_rules = [], []
    spider = None
    spider_candidates = []
    per_source = []

    for src in TVBOX_SOURCES:
        _log(progress, f"[TVBox] 正在抓 {src['name']}")
        urls = github_channels(src["owner"], src["repo"], src["branch"], src["path"])
        urls += src.get("extra", [])
        obj, used = _fetch_one_tvbox_json(urls, progress=progress, label=src["name"])
        if obj is None:
            per_source.append({"name": src["name"], "ok": False, "used_url": None,
                               "sites": 0, "error": "全部通道失败或 JSON 解析失败"})
            continue
        base = base_dir_of(used)
        if isinstance(obj, dict) and isinstance(obj.get("urls"), list) and not obj.get("sites"):
            _log(progress, f"  多仓索引, 二次解析 {len(obj['urls'])} 个子仓")
            sub_sites = sub_parses = sub_lives = 0
            for entry in obj["urls"][:10]:
                if not isinstance(entry, dict) or not entry.get("url"):
                    continue
                sub_obj, sub_used = _fetch_one_tvbox_json([entry["url"]], progress=progress)
                if sub_obj is None:
                    continue
                ss, pp, ll, sp, dd, rr = _parse_tvbox_obj(sub_obj, base_dir_of(sub_used), progress)
                all_sites.extend(ss); all_parses.extend(pp); all_lives.extend(ll)
                all_doh.extend(dd); all_rules.extend(rr)
                if sp and sp not in spider_candidates:
                    spider_candidates.append(sp)
                if sp and not spider:
                    spider = sp
                sub_sites += len(ss); sub_parses += len(pp); sub_lives += len(ll)
            per_source.append({"name": src["name"], "ok": True, "used_url": used,
                               "sites": sub_sites, "parses": sub_parses, "lives": sub_lives,
                               "note": "多仓索引二次解析"})
            continue
        ss, pp, ll, sp, dd, rr = _parse_tvbox_obj(obj, base, progress)
        all_sites.extend(ss); all_parses.extend(pp); all_lives.extend(ll)
        all_doh.extend(dd); all_rules.extend(rr)
        if sp and sp not in spider_candidates:
            spider_candidates.append(sp)
        if sp and not spider:
            spider = sp
        per_source.append({"name": src["name"], "ok": True, "used_url": used,
                           "sites": len(ss), "parses": len(pp), "lives": len(ll)})
        _log(progress, f"  OK {src['name']}: {len(ss)} 站 / {len(pp)} 解析 / {len(ll)} 直播")

    _log(progress, "[TVBox] 正在抓直播源 Lightconer/TVBox-Sources")
    live_urls = github_channels(LIVES_SOURCE["owner"], LIVES_SOURCE["repo"],
                                LIVES_SOURCE["branch"], LIVES_SOURCE["path"])
    live_obj, live_used = _fetch_one_tvbox_json(live_urls, progress=progress)
    lives_from_source = []
    if live_obj is not None:
        _, _, lives_from_source, _, _, _ = _parse_tvbox_obj(
            live_obj, base_dir_of(live_used), progress)
        per_source.append({"name": LIVES_SOURCE["name"], "ok": True, "used_url": live_used,
                           "sites": 0, "lives": len(lives_from_source)})
    else:
        per_source.append({"name": LIVES_SOURCE["name"], "ok": False, "used_url": None,
                           "sites": 0, "error": "全部通道失败"})

    # spider 输出用实际能打开的通道 (修复 B): jsDelivr 对 .jar 返回 403,
    # 只输出 jsDelivr 原链 TVBox 里也打不开。按上游优先级试每个候选 spider,
    # 取第一个多通道 200 的 working URL; 都不通则保留原值并如实记录。
    spider_working = None
    for cand_sp in spider_candidates:
        _ok, _working = _check_spider_ok(cand_sp)
        if _ok:
            spider_working = _working
            break
    if spider_working:
        spider = spider_working
    _log(progress, f"[TVBox] spider: {spider} (原始首选 {spider_candidates[0] if spider_candidates else None})")

    # 去重: 按 key, 先到的上游优先
    seen_keys = set()
    deduped = []
    for s in all_sites:
        if s["key"] in seen_keys:
            continue
        seen_keys.add(s["key"])
        deduped.append(s)
    # 成人过滤 (2026-11-11 修复): 作用于全部 TVBox 站, 去重后、测速前就过滤,
    # 这样 type-3 池和可测速池都不会再有成人站。被删的 3 个偏门名额由 type-3
    # 配额的 surplus 补 (动漫/直播候选充足)。记录去重, 按 key 只记一次。
    adult_removed = []
    _adult_seen = set()

    def _record_adult(site, stage, reason, type_label=None):
        k = str(site.get("key", ""))
        if k in _adult_seen:
            return
        _adult_seen.add(k)
        adult_removed.append({"key": k, "name": site.get("name", ""),
                              "type": type_label if type_label is not None else site.get("type"),
                              "api": site.get("api", "") if isinstance(site.get("api", ""), str) else "",
                              "reason": reason, "stage": stage})

    def _drop_adult(site_list, stage):
        kept = []
        for s in site_list:
            is_adult, reason = _is_adult_tvbox(s)
            if is_adult:
                _record_adult(s, stage, reason)
            else:
                kept.append(s)
        return kept

    deduped = _drop_adult(deduped, "dedup后/全站")
    _log(progress, f"[TVBox] 成人过滤: 剔除 {len(adult_removed)} 个成人向站: "
                   f"{[a['key'] for a in adult_removed]}")
    # 修复: type-3 单独处理, 不进 CMS 测速池 (csp_ 裸标识根本不是 URL,
    # 拿去 requests.get 会直接报错, 且任务要求 type-3 只做 jar/ext 校验)
    type3_pool = [s for s in deduped if s.get("type") == 3]
    testable = [s for s in deduped if s.get("type") != 3]
    deduped = testable  # 下面的测速只针对可测速的 type-0/1/4 站
    _log(progress, f"[TVBox] 候选池去重后 {len(testable) + len(type3_pool)} 站 "
                   f"(可测速 {len(testable)} / type-3 {len(type3_pool)}), "
                   f"开始第一轮实测 (10 并发, 每站超时 5s)")

    # ---- 第一轮实测 ----
    round1 = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(_speed_test_one, s): s for s in deduped}
        done_n = 0
        for fut in as_completed(futs):
            rec = fut.result()
            round1[rec["key"]] = rec
            done_n += 1
            if done_n % 50 == 0:
                _log(progress, f"  第一轮实测 {done_n}/{len(deduped)}")
    pass1 = [r for r in round1.values() if r["ok"]]
    _log(progress, f"[TVBox] 第一轮通过 {len(pass1)}/{len(deduped)} (TTFB<{TTFB_LIMIT_MS}ms 且总耗时<{TOTAL_LIMIT_MS}ms 且有 vod 列表)")

    # ---- 第二轮复测: 分开取热门前 110 + 偏门前 60 再复测, 确认稳定性 ----
    # 注意: 不能只取全局速度前 160 名, 否则偏门站 (普遍较慢) 会被热门站挤出复测池,
    # 导致最终 70/30 构成无偏门可选 (2026-11-11 实测曾出现热门 118 / 偏门 2)。
    ranked1 = sorted(pass1, key=_speed_score)
    site_by_key = {s["key"]: s for s in deduped}
    _pop1 = [r for r in ranked1 if classify_tvbox(site_by_key[r["key"]])[0] == "热门"]
    _nic1 = [r for r in ranked1 if classify_tvbox(site_by_key[r["key"]])[0] == "偏门"]
    retest_keys = [r["key"] for r in _pop1[:110]] + [r["key"] for r in _nic1[:60]]
    round2 = {}
    if retest_keys:
        _log(progress, f"[TVBox] 第二轮复测前 {len(retest_keys)} 名, 确认稳定性")
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(_speed_test_one, site_by_key[k]): k for k in retest_keys}
            for fut in as_completed(futs):
                rec = fut.result()
                round2[rec["key"]] = rec
    # 两轮都通过才算最终通过; 取两轮中较差的一次 (保守)
    final_records = []
    for key, r1 in round1.items():
        if key not in round2:
            continue
        r2 = round2[key]
        if r1["ok"] and r2["ok"]:
            merged = dict(r1)
            merged["ttfb_ms"] = max(r1["ttfb_ms"] or 9999, r2["ttfb_ms"] or 9999)
            merged["total_ms"] = max(r1["total_ms"] or 9999, r2["total_ms"] or 9999)
            merged["kbps"] = min(r1["kbps"] or 0, r2["kbps"] or 0)
            merged["checked_at"] = r2["checked_at"]
            merged["rounds"] = 2
            final_records.append(merged)
    _log(progress, f"[TVBox] 两轮都通过 {len(final_records)} 站")

    # 多次失败追踪: 本轮失败的 key 计入 failures.json, 连续 >=2 次直接剔除的依据
    failures_path = os.path.join(out_dir, "failures.json")
    prev_failures = {}
    try:
        with open(failures_path, encoding="utf-8") as f:
            prev_failures = json.load(f)
    except Exception:
        prev_failures = {}
    for key, rec in round1.items():
        if rec["ok"]:
            prev_failures.pop(key, None)
        else:
            prev_failures[key] = int(prev_failures.get(key, 0)) + 1
    with open(failures_path, "w", encoding="utf-8") as f:
        json.dump(prev_failures, f, ensure_ascii=False, indent=2)
    multi_failed = {k for k, v in prev_failures.items() if v >= 2}
    final_records = [r for r in final_records if r["key"] not in multi_failed]

    # ---- 按 70% 热门 + 30% 偏门构成截取 (可测速站) ----
    threshold_relaxed = False
    for r in final_records:
        cat, tag = classify_tvbox(site_by_key[r["key"]])
        r["category"] = cat
        r["niche_tag"] = tag
    final_records.sort(key=_speed_score)
    popular = [r for r in final_records if r["category"] == "热门"]
    niche = [r for r in final_records if r["category"] == "偏门"]
    target_popular = int(MAX_TVBOX_SITES * 0.7)  # 84
    # 可测速站先只取热门 84 + 已有偏门 (原来只有 2 个), 剩余偏门名额由 type-3 补
    picked = popular[:target_popular] + niche
    picked = picked[:MAX_TVBOX_SITES]

    # ---- type-3 偏门补充 (修复 A): 只加约 34 个, 不把 3,082 个全塞进来 ----
    # 严格分类 + 剔工具类 + spider jar / api / ext 可达性校验, 不做单站延迟测试
    type3_by_key = {s["key"]: s for s in type3_pool}
    type3_niche_pool = []
    type3_tool_removed = []
    type3_total = len(type3_pool)
    for s in type3_pool:
        # 成人过滤二次兜底: 正常流程里去重后已过滤, 这里不应触发; 万一触发也剔除并记录
        _adult3, _reason3 = _is_adult_tvbox(s)
        if _adult3:
            _record_adult(s, "type-3挑选兜底", _reason3)
            continue
        if _is_tool_site(s):
            type3_tool_removed.append(s["key"])
            continue
        cat, tag = classify_tvbox(s)
        if cat != "偏门":
            continue
        type3_niche_pool.append(s)
    _log(progress, f"[TVBox] type-3 池 {type3_total} 站, 剔工具类 {len(type3_tool_removed)} 个, "
                   f"严格偏门 {len(type3_niche_pool)} 个, 开始 jar/ext 校验 (10 并发)")
    spider_cache = {}
    type3_validated = {}  # key -> record
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(_validate_type3_one, s, spider_cache): s for s in type3_niche_pool}
        for fut in as_completed(futs):
            ok, rec = fut.result()
            type3_validated[rec["key"]] = rec
    type3_passed = [r for r in type3_validated.values() if r["ok"]]
    type3_failed = [r for r in type3_validated.values() if not r["ok"]]
    _log(progress, f"[TVBox] type-3 偏门校验通过 {len(type3_passed)}/{len(type3_niche_pool)} "
                   f"(spider 失败 {sum(1 for r in type3_failed if not r['spider_ok'])} / "
                   f"api 失败 {sum(1 for r in type3_failed if not r['api_ok'])} / "
                   f"ext 失败 {sum(1 for r in type3_failed if not r['ext_ok'])})")
    # 按配额取, 不足的 tag 用其它 tag 的 surplus 补, 总数约 34
    type3_picked_recs = []
    by_tag = {}
    for r in type3_passed:
        by_tag.setdefault(r["niche_tag"], []).append(r)
    for tag in by_tag:
        by_tag[tag].sort(key=lambda r: r["key"])
    for tag, quota in TYPE3_NICHE_QUOTA.items():
        type3_picked_recs += by_tag.get(tag, [])[:quota]
    if len(type3_picked_recs) < sum(TYPE3_NICHE_QUOTA.values()):
        rest3 = [r for r in type3_passed if r not in type3_picked_recs]
        rest3.sort(key=lambda r: r["key"])
        type3_picked_recs += rest3[:sum(TYPE3_NICHE_QUOTA.values()) - len(type3_picked_recs)]
    # 与可测速站合并, 总数控制在 107-120: 热门 84 + 已有偏门 + 新增偏门,
    # 超过 120 时先砍最慢的热门 (按 _speed_score), 不砍偏门
    type3_picked_keys = {r["key"] for r in type3_picked_recs}
    for r in type3_picked_recs:
        # 转成 speed_records 同形, 但明确标注未做延迟测试
        speed_records_type3 = {"key": r["key"], "name": r.get("name", ""), "api": r["api"], "api_host": "",
                               "ok": True, "ttfb_ms": None, "total_ms": None,
                               "kbps": None, "sample_bytes": 0,
                               "channel": "spider-check", "domestic_cdn": False,
                               "vod_ok": None, "error": None,
                               "checked_at": r["checked_at"], "category": "偏门",
                               "niche_tag": r["niche_tag"], "rounds": 0,
                               "note": "type-3 未做单站 HTTP 延迟测试, 仅做 jar/ext 可达性校验",
                               "spider": r["spider"], "spider_working": r.get("spider_working"),
                               "spider_ok": r["spider_ok"],
                               "api_ok": r["api_ok"], "api_check": r["api_check"],
                               "ext_ok": r["ext_ok"], "ext_check": r["ext_check"]}
        picked.append(speed_records_type3)
    picked_testable = [p for p in picked if p.get("rounds", 0) > 0 or p.get("key") in site_by_key]
    # 去重: type-3 的 key 理论上不与可测速站重复 (dedup 已保证), 保险起见再去一次
    _seen_pick = set()
    _picked_dedup = []
    for p in picked:
        if p["key"] in _seen_pick:
            continue
        _seen_pick.add(p["key"])
        _picked_dedup.append(p)
    picked = _picked_dedup
    if len(picked) > MAX_TVBOX_SITES:
        # 超过 120: 只砍热门可测速站里最慢的, 保留全部偏门
        _niche_pick = [p for p in picked if p.get("category") == "偏门"]
        _pop_pick = sorted([p for p in picked if p.get("category") != "偏门"],
                           key=_speed_score)
        _keep_pop = MAX_TVBOX_SITES - len(_niche_pick)
        picked = _pop_pick[:_keep_pop] + _niche_pick
    # 通过数不足 80 时, 从第一轮通过但第二轮未复测/未通过的里按分数补到 80, 并标记放宽
    if len(picked) < MIN_TVBOX_SITES:
        threshold_relaxed = True
        extra = [r for r in ranked1 if r["key"] not in {p["key"] for p in picked}]
        extra.sort(key=_speed_score)
        for r in extra[:MIN_TVBOX_SITES - len(picked)]:
            cat, tag = classify_tvbox(site_by_key[r["key"]])
            r = dict(r); r["category"] = cat; r["niche_tag"] = tag; r["rounds"] = 1
            r["note"] = "仅一轮通过, 为凑满 80 站放宽补录"
            picked.append(r)
    # 排序: 可测速站按分数, type-3 偏门排在后面 (无延迟分数, 按 tag+key 排)
    _testable_pick = sorted([p for p in picked if p.get("rounds", 0) > 0],
                            key=_speed_score)
    _type3_pick = sorted([p for p in picked if p.get("rounds", 0) == 0],
                         key=lambda p: (p.get("niche_tag", ""), p["key"]))
    picked = _testable_pick + _type3_pick
    sites = []
    speed_records = []
    for r in picked:
        src_site = site_by_key.get(r["key"]) or type3_by_key.get(r["key"])
        if src_site is None:
            continue
        s = {k: v for k, v in dict(src_site).items()
             if not k.startswith("_")}
        sites.append(s)
        speed_records.append(r)
    niche_picked = [r for r in speed_records if r["category"] == "偏门"]
    type3_picked = [r for r in speed_records if r.get("rounds", 0) == 0]

    def _dedup_by_url(items):
        seen, out = set(), []
        for it in items:
            u = it.get("url")
            if not u or u in seen:
                continue
            seen.add(u)
            out.append(it)
        return out

    parses = _dedup_by_url(all_parses)
    # lives 也过成人过滤 (按频道名 + 直播列表 URL 查关键词, 本次应为 0 个)
    _lives_raw = lives_from_source if lives_from_source else all_lives
    _lives_kept = []
    for lv in _lives_raw:
        _a, _r = _is_adult_tvbox({"key": lv.get("name", ""), "name": lv.get("name", ""),
                                  "api": lv.get("url", "")})
        if _a:
            _record_adult({"key": lv.get("name", ""), "name": lv.get("name", ""),
                           "api": lv.get("url", "")}, "lives", _r, type_label="live")
        else:
            _lives_kept.append(lv)
    lives = _dedup_by_url(_lives_kept)
    doh = _dedup_by_url(all_doh)
    rules = all_rules[:200]

    # 最终复核: 写文件前再查一次, 成人站一个都不能留 (正常应为 0 个,
    # 万一有就剔除并记录, speed_records 同步过滤以免 meta 与文件不一致)
    _final_sites = []
    for s in sites:
        _a, _r = _is_adult_tvbox(s)
        if _a:
            _record_adult(s, "最终复核", _r)
        else:
            _final_sites.append(s)
    if len(_final_sites) != len(sites):
        _keep_keys = {s["key"] for s in _final_sites}
        speed_records = [r for r in speed_records if r["key"] in _keep_keys]
        sites = _final_sites

    out = {}
    if spider:
        out["spider"] = spider
    out["sites"] = sites
    out["parses"] = parses
    out["lives"] = lives
    if doh:
        out["doh"] = doh
    if rules:
        out["rules"] = rules

    with open(os.path.join(out_dir, "tvbox.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    meta = {
        "generated_at": now_iso(),
        "spider": spider,
        "spider_candidates": spider_candidates,
        "site_count": len(sites),
        "candidate_pool_size": len(deduped) + len(type3_pool),
        "testable_pool_size": len(deduped),
        "type3_pool_size": len(type3_pool),
        "type3_niche_pool_size": len(type3_niche_pool),
        "type3_validated_passed": len(type3_passed),
        "type3_validated_failed": len(type3_failed),
        "type3_tool_removed": sorted(type3_tool_removed),
        "adult_keywords": {
            "strong_substring": TVBOX_ADULT_STRONG_KEYWORDS,
            "token_regex": ["av", "sex", "xxx"],
            "cautious_not_alone": TVBOX_ADULT_CAUTIOUS_KEYWORDS,
            "removed_known": TVBOX_ADULT_REMOVED_KNOWN,
        },
        "adult_removed": adult_removed,
        "adult_removed_count": len(adult_removed),
        "type3_quota": TYPE3_NICHE_QUOTA,
        "round1_passed": len(pass1),
        "round2_passed": len(final_records),
        "target": f"{MIN_TVBOX_SITES}-{MAX_TVBOX_SITES} (目标 {MAX_TVBOX_SITES}, 约 70% 热门 + 30% 偏门)",
        "popular_count": sum(1 for r in speed_records if r["category"] == "热门"),
        "niche_count": sum(1 for r in speed_records if r["category"] == "偏门"),
        "threshold_relaxed": threshold_relaxed,
        "thresholds": {"ttfb_ms_lt": TTFB_LIMIT_MS, "total_ms_lt": TOTAL_LIMIT_MS,
                       "sample_max_kb": 200, "rounds": 2},
        "multi_failed_removed": sorted(multi_failed),
        "parse_count": len(parses),
        "live_count": len(lives),
        "sources": per_source,
        "speed_records": speed_records,
        "type3_records": [r for r in type3_validated.values()],
        "type3_picked": [{"key": r["key"], "name": r.get("name", ""),
                          "niche_tag": r["niche_tag"], "api": r["api"],
                          "spider_ok": r.get("spider_ok"), "api_ok": r.get("api_ok"),
                          "ext_ok": r.get("ext_ok"), "spider": r.get("spider"),
                          "spider_working": r.get("spider_working")}
                         for r in type3_picked],
        "niche_picked": [{"key": r["key"], "niche_tag": r["niche_tag"],
                          "ttfb_ms": r.get("ttfb_ms"), "kbps": r.get("kbps"),
                          "type": "type-3" if r.get("rounds", 0) == 0 else "testable"}
                         for r in niche_picked],
        "notes": [
            "成人过滤 (2026-11-11 修复): TVBox 侧新增成人关键词过滤, 作用于全部 TVBox 站 (去重后、测速前)、type-3 挑选兜底、最终写文件前复核, 并过滤 lives 频道名。本次剔除 3d涩动漫.py、91动漫.py、Cam4直播.py, 被删的 3 个偏门名额由 type-3 配额的 surplus 补齐, 剔除明细见 adult_removed。AV/sex/xxx 按词边界匹配 (kxgav.com 的白嫖站不受影响); 老片不单独过滤 (1905 经典影视保留)。书源侧已有 ADULT_KEYWORDS 过滤, 本次未改动。",
            "大陆速度未实测: 本机在新加坡, 测速为新加坡 VM 实测 + 国内 CDN 启发式 (jsDelivr/.cn/国内云域名加分), 部署后需大陆实测。测速方法见 README'筛选标准' (itdog/boce/17ce 多点测速思路)。",
            "type-3 站未做单站 HTTP 延迟测试, 只做了 jar/ext 可达性校验: (a) 该上游的 spider jar HEAD/GET 200 (剥 md5 后缀后测), (b) api 若是 http URL 则 HEAD/GET 能打开, csp_/py_ 裸标识跳过 (靠 spider jar 解析), (c) ext 若是 http URL 则 HEAD/GET 能打开。每个新增偏门站的校验结果见 type3_picked / type3_records。type-3 的实际播放速度取决于 spider jar 和对应 App 后端, 需在 TVBox 里实测。",
            "spider 修复: 先剥掉 ';md5;...' 后缀再判断, 相对路径按 base 解析, .jpg/.png 伪装的 jar 原样保留 (TVBox 接受它当 jar)。本轮 spider 取自首个成功的上游 (hebijunge 的 LIBVIO.jar, 剥 md5 后)。",
            "带宽 KB/s 是 ?ac=list 小样本 (通常只有几 KB) 的等效速率, 受 TTFB 主导, 不能直接等同于视频播放带宽; 真正播放带宽取决于 m3u8 源站。sample_bytes 字段记录了每次测速实际下载的字节数, 可复核。",
            "配置文件下载快不等于播放不卡, 播放取决于各 sites api 指向的 CMS 站和 m3u8 源站带宽。",
            "部分上游 README 声明仅供学习、禁止商用, 请遵守当地法律和各源的使用声明。",
        ],
    }
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    _log(progress, f"[TVBox] 完成: {len(sites)} 站 (热门 {meta['popular_count']} / 偏门 {meta['niche_count']}) / {len(parses)} 解析 / {len(lives)} 直播")
    return meta


# ---------------------------------------------------------------- Legado
ADULT_KEYWORDS = [
    "po18", "海棠", "御书屋", "御宅屋", "短剧", "微短剧",
    "成人", "色情", "黄文", "黄色", "黄书",
    "有声", "听书", "漫画", "漫畫",
]
LOGIN_PAYWALL_KEYWORDS = ["登录", "登陆", "付费", "VIP", "会员", "充值", "开通", "付费墙"]
TEST_BOOKS = [
    ("斗破苍穹", "天蚕土豆"),
    ("凡人修仙传", "忘语"),
    ("庆余年", "猫腻"),
]
LEGADO_SOURCES = [
    {"name": "yck 聚合 自用(2) id=1135",
     "urls": ["https://www.yck2026.fun/yuedu/shuyuans/json/id/1135.json",
              "https://www.yckceo.com/yuedu/shuyuans/json/id/1135.json",
              "https://www.yck2026.top/yuedu/shuyuans/json/id/1135.json"]},
    {"name": "luanmuc/legado-book-sources books.json",
     "urls": ["https://github.com/luanmuc/legado-book-sources/releases/latest/download/books.json",
              "https://cdn.jsdelivr.net/gh/luanmuc/legado-book-sources@main/books.json",
              "https://raw.githubusercontent.com/luanmuc/legado-book-sources/main/books.json"]},
    {"name": "suwa-ko/legado-sources all.json",
     "urls": ["https://cdn.jsdelivr.net/gh/suwa-ko/legado-sources@main/all.json",
              "https://raw.githubusercontent.com/suwa-ko/legado-sources/main/all.json"]},
    {"name": "XIU2/Yuedu shuyuan",
     "urls": ["https://cdn.jsdelivr.net/gh/XIU2/Yuedu@master/shuyuan",
              "https://raw.githubusercontent.com/XIU2/Yuedu/master/shuyuan"]},
    {"name": "cjj200011222/legado-booksource all.json",
     "urls": ["https://cdn.jsdelivr.net/gh/cjj200011222/legado-booksource@master/all.json",
              "https://raw.githubusercontent.com/cjj200011222/legado-booksource/master/all.json"]},
]


def _is_adult_or_non_novel(item):
    hay = f"{item.get('bookSourceName','')} {item.get('bookSourceUrl','')}".lower()
    return any(k in hay for k in ADULT_KEYWORDS)


def _completeness(item):
    """规则齐全度 0-6: searchUrl/ruleSearch/ruleContent/ruleBookInfo/ruleToc/ruleExplore。"""
    score = 0
    if item.get("searchUrl"):
        score += 1
    for k in ("ruleSearch", "ruleContent", "ruleBookInfo", "ruleToc"):
        if item.get(k):
            score += 1
    if item.get("ruleExplore") or item.get("exploreUrl"):
        score += 1
    return score


def _strip_js_tail(tpl):
    """剥掉 searchUrl 尾部的 @js: 代码 (修复 C1)。
    如 'https://.../search.html,{...}\\n@js:\\nurl=...' -> 只留 URL+配置部分,
    否则 JSON 配置解析会因尾部 @js: 内容失败, 导致 POST 被误判成无 body 的 GET。
    大小写不敏感, 找到第一个 @js: 即截断。"""
    if not isinstance(tpl, str):
        return tpl
    low = tpl.lower()
    idx = low.find("@js:")
    if idx != -1:
        return tpl[:idx].rstrip().rstrip(",").strip()
    return tpl


def _strip_leading_template_blocks(tpl, base):
    """处理以 {{...}} 开头的 searchUrl (修复 C2), 返回 (cleaned_tpl, skipped_reason)。
    - '{{cookie.removeCookie(...)}}' + 真实 URL -> 剥掉 cookie 块, 继续解析剩余 URL。
    - '{{source.getKey().match(...)}}' + '/search...' -> 该块求值就是 bookSourceUrl
      去掉 # 后缀的部分, 用 base 替换该块后继续解析。
    - '{{url=...}}' (JS 动态算 URL, 如 java.ajax) -> 无法用简单 HTTP 复现,
      返回 skipped, 不要当成 0 分。
    - 其它未知 {{...}} -> skipped。
    最多剥 3 层, 防止死循环。"""
    cleaned = tpl.strip()
    for _ in range(3):
        if not cleaned.startswith("{{"):
            return cleaned, None
        end = cleaned.find("}}")
        if end == -1:
            return None, "unclosed_template"
        inner = cleaned[2:end].strip()
        remainder = cleaned[end + 2:].lstrip("\n\r ")
        low_inner = inner.lower()
        if low_inner.startswith("cookie"):
            if not remainder:
                return None, "cookie_only_no_url"
            cleaned = remainder
            continue
        if low_inner.startswith("url="):
            return None, "js_url_template"
        if "source.getkey" in low_inner or "source.booksourceurl" in low_inner:
            if not remainder:
                return None, "js_getkey_no_url"
            if remainder.startswith(("http://", "https://")):
                cleaned = remainder
                continue
            if remainder.startswith("/"):
                if not base:
                    return None, "js_getkey_no_base"
                cleaned = base.rstrip("/") + remainder
                continue
            return None, "js_getkey_template"
        return None, "unknown_template"
    if cleaned.startswith("{{"):
        return None, "too_many_template_blocks"
    return cleaned, None


def _parse_search_request_full(item, book_title, page=1):
    """完整解析, 返回 (parsed_or_None, status, reason)。
    status: 'ok' / 'skipped' (JS 模板/POST 环境拦截, 不应判 0 分) / 'invalid'。"""
    tpl = (item.get("searchUrl") or "").strip()
    if not tpl:
        return None, "invalid", "empty_searchUrl"
    # (1) 先剥 @js: 尾巴, 再拆配置, 否则 POST 配置解析失败
    tpl = _strip_js_tail(tpl)
    if not tpl:
        return None, "invalid", "empty_after_js_strip"
    base = (item.get("bookSourceUrl") or "").split("#")[0].strip()
    # (2) 处理开头的 {{...}} 块
    if tpl.startswith("{{"):
        cleaned, reason = _strip_leading_template_blocks(tpl, base)
        if reason is not None:
            return None, "skipped", reason
        tpl = cleaned
    # 拆尾部 JSON 配置: 第一个 ",{" 之后是配置
    config = {}
    url_tpl = tpl
    idx = tpl.find(",{")
    if idx != -1:
        url_tpl = tpl[:idx].strip()
        try:
            config = json.loads(tpl[idx + 1:])
        except Exception:
            try:
                config = json.loads(tpl[idx + 1:].replace("'", '"'))
            except Exception:
                config = {}
    if not url_tpl or url_tpl.startswith("{{"):
        return None, "skipped", "js_template_remaining"
    q = quote_plus(book_title)
    url_tpl = url_tpl.replace("{{key}}", q).replace("{{page}}", str(page))
    url_tpl = url_tpl.replace("{key}", q).replace("{page}", str(page))
    url_tpl = url_tpl.replace("$key", q)
    if "{{" in url_tpl or "}}" in url_tpl:
        return None, "skipped", "js_template_remaining"
    if not url_tpl.startswith(("http://", "https://")):
        if not base:
            return None, "invalid", "no_base_for_relative_url"
        url_tpl = urljoin(base.rstrip("/") + "/", url_tpl.lstrip("/"))
    method = str(config.get("method", "GET")).upper()
    body_tpl = config.get("body", "")
    body = None
    if isinstance(body_tpl, str) and body_tpl:
        body = body_tpl.replace("{{key}}", q).replace("{{page}}", str(page))
        body = body.replace("{key}", q).replace("{page}", str(page))
    charset = str(config.get("charset", "")).lower() or None
    return (method, url_tpl, body, charset), "ok", None


def _parse_search_request(item, book_title, page=1):
    """解析 Legado searchUrl 规则, 返回 (method, url, body, charset) 或 None。
    兼容旧调用: JS 模板 / POST 环境拦截等返回 None, 原因见 _parse_search_request_full。
    searchUrl 不是普通 URL, 常见形态:
    - 相对路径: "search/{{key}}/{{page}}", "/modules/article/search.php?..."
      -> 以 bookSourceUrl 为基准 urljoin
    - 尾部 JSON 配置: "https://...?key={{key}},{\n\"charset\":\"gbk\"\n}"
      -> 逗号后是配置, 不是 URL 的一部分; charset 用于解码 GBK 响应
    - POST: 配置里 "method":"POST", "body":"searchkey={{key}}" -> 改发 POST
    - 尾部 @js: 代码: 先剥掉再解析, 否则 POST 配置解析失败 (修复 C1)
    - 开头 {{cookie...}}: 剥掉 cookie 块后解析剩余 URL; 开头 {{url=...}}:
      JS 动态算 URL, 无法复现, 跳过不判 0 分 (修复 C2)
    - bookSourceUrl 自身可能带 "#..." / "##..." 后缀, 先截掉再做基准。"""
    parsed, _status, _reason = _parse_search_request_full(item, book_title, page)
    return parsed


_ENV_POST_BLOCKED = None
_ENV_POST_CHECK = {}


def _is_post_blocked_env():
    """检测本机出口是否拦截 POST (修复 C: 沙箱 403/超时不要当成源本身坏)。
    实测: 本机出口代理对 POST 一律超时 (httpbin.org/post、baidu POST 均 10s 超时,
    而 GET 正常), 说明是环境拦截, 不是源站问题。只检测一次并缓存。
    返回 True 表示本机测不了 POST, POST 源应标记为未测试 (search_pass=null),
    不要判 0 分; 需在正常环境 (Render/VPS 等能 POST 的机器) 重跑 fetcher.py
    才能得到 POST 源的真实通过率。"""
    global _ENV_POST_BLOCKED
    if _ENV_POST_BLOCKED is not None:
        return _ENV_POST_BLOCKED
    try:
        r = requests.post("https://httpbin.org/post", headers=HEADERS,
                          data="probe=1", timeout=6)
        blocked = r.status_code != 200
        _ENV_POST_CHECK.update({"probe_url": "https://httpbin.org/post",
                                "status": r.status_code, "blocked": blocked})
    except Exception as e:  # noqa: BLE001
        blocked = True
        _ENV_POST_CHECK.update({"probe_url": "https://httpbin.org/post",
                                "status": None, "error": f"{type(e).__name__}: {e}",
                                "blocked": True})
    _ENV_POST_BLOCKED = blocked
    return blocked


def _build_search_url(item, book_title, page=1):
    """兼容旧调用: 只返回 GET 的 URL, POST/JS 模板返回 None。"""
    parsed = _parse_search_request(item, book_title, page)
    if not parsed or parsed[0] != "GET":
        return None
    return parsed[1]


def _check_url_reachable(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=5)
        return 200 <= r.status_code < 400
    except Exception:
        return False


def _search_test_one_detailed(item, book_title, author):
    """单本书搜索实测, 返回 (result, reason)。
    result: True 通过 / False 失败 (200 但无书名、404、首页返回、超时等) /
            None 跳过 (JS 模板无法复现、或本机出口拦截 POST, 不应判 0 分)。
    reason: 'pass' / 'fail:<原因>' / 'skipped:<原因>'。
    按 charset 解码 (很多站是 GBK), POST 模板按 POST 发, 相对路径按
    bookSourceUrl 解析。"""
    parsed, status, parse_reason = _parse_search_request_full(item, book_title)
    if status == "skipped":
        return None, f"skipped:{parse_reason}"
    if parsed is None:
        return False, f"fail:parse_{parse_reason}"
    method, url, body, charset = parsed
    if method == "POST" and _is_post_blocked_env():
        # 本机出口拦截 POST (httpbin POST 也超时), 不是源站坏: 跳过, 不判 0 分
        return None, "skipped:env_post_blocked"
    try:
        if method == "POST":
            r = requests.post(url, headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
                              data=body.encode("utf-8") if body else None, timeout=6)
        else:
            r = requests.get(url, headers=HEADERS, timeout=6)
        if r.status_code != 200:
            return False, f"fail:http_{r.status_code}"
        if charset in ("gbk", "gb2312", "gb18030"):
            text = r.content.decode(charset, errors="ignore")
        else:
            try:
                text = r.content.decode(r.apparent_encoding or "utf-8", errors="ignore")
            except Exception:
                text = r.text
        if (book_title in text) or (author in text):
            return True, "pass"
        return False, "fail:no_title_in_response"
    except Exception as e:  # noqa: BLE001
        # 区分环境拦截: POST 超时且环境已确认拦截 POST 时, 上面已提前跳过;
        # 这里的超时/异常仍记为失败, 但若是 POST 且异常含 Proxy/403, 记为跳过
        err = f"{type(e).__name__}: {e}"
        if method == "POST" and ("ProxyError" in err or "403" in err):
            return None, f"skipped:env_post_{err[:80]}"
        return False, f"fail:{err[:120]}"


def _search_test_one(item, book_title, author):
    """单本书搜索实测: 200 且正文含书名或作者才算通过 (兼容旧调用, 跳过返回 None)。
    按 charset 解码 (很多站是 GBK, requests 默认按 utf-8 解会成乱码导致误判),
    POST 模板按 POST 发, 相对路径按 bookSourceUrl 解析。"""
    result, _reason = _search_test_one_detailed(item, book_title, author)
    return result


def fetch_books(data_dir, progress=None):
    out_dir = os.path.join(data_dir, "books")
    os.makedirs(out_dir, exist_ok=True)
    seen_urls = set()
    candidates = []  # 通过基础过滤的全部候选
    per_source = []
    n_invalid = n_non_text = n_adult = 0

    for src in LEGADO_SOURCES:
        _log(progress, f"[书源] 正在抓 {src['name']}")
        text, used = fetch_text(src["urls"], progress=progress, label=src["name"])
        if text is None:
            per_source.append({"name": src["name"], "ok": False, "used_url": None,
                               "kept": 0, "error": "全部通道失败"})
            continue
        try:
            data = json.loads(text)
        except Exception as e:  # noqa: BLE001
            per_source.append({"name": src["name"], "ok": False, "used_url": used,
                               "kept": 0, "error": f"JSON 解析失败: {e}"})
            continue
        if isinstance(data, dict):
            lists = [v for v in data.values() if isinstance(v, list)]
            data = lists[0] if lists else []
        if not isinstance(data, list):
            per_source.append({"name": src["name"], "ok": False, "used_url": used,
                               "kept": 0, "error": "顶层不是数组"})
            continue
        kept_here = 0
        for item in data:
            if not isinstance(item, dict):
                n_invalid += 1
                continue
            if not item.get("bookSourceName") or not item.get("bookSourceUrl") or not item.get("ruleSearch"):
                n_invalid += 1
                continue
            btype = item.get("bookSourceType", 0)
            try:
                btype = int(btype)
            except Exception:
                btype = 0
            if btype != 0:
                n_non_text += 1
                continue
            if _is_adult_or_non_novel(item):
                n_adult += 1
                continue
            key = str(item["bookSourceUrl"]).strip().lower().rstrip("/")
            if key in seen_urls:
                continue
            seen_urls.add(key)
            candidates.append(item)
            kept_here += 1
        per_source.append({"name": src["name"], "ok": True, "used_url": used,
                           "total": len(data), "kept": kept_here})
        _log(progress, f"  OK {src['name']}: 共 {len(data)} 条, 本次新增 {kept_here} 条")
    _log(progress, f"[书源] 基础过滤后候选 {len(candidates)} 个, 开始规则/登录墙筛选")

    # ---- 规则齐全 + 登录/付费墙筛选 ----
    n_rule_incomplete = n_no_search = n_login_paywall = 0
    usable = []
    for item in candidates:
        if not item.get("ruleSearch") or not item.get("ruleContent") or not item.get("ruleToc"):
            n_rule_incomplete += 1
            continue
        if not item.get("searchUrl") and not item.get("ruleSearch"):
            n_no_search += 1
            continue
        hay = f"{item.get('bookSourceComment','')} {item.get('bookSourceGroup','')} {item.get('bookSourceName','')}"
        if any(k in hay for k in LOGIN_PAYWALL_KEYWORDS):
            n_login_paywall += 1
            continue
        usable.append(item)
    _log(progress, f"[书源] 规则齐全且无登录墙 {len(usable)} 个 (剔除规则不全 {n_rule_incomplete} / 无搜索 {n_no_search} / 登录付费墙 {n_login_paywall})")

    # ---- 头部池: 70% 热门 + 30% 偏门, 各按齐全度排序, 共 400 个 ----
    for it in usable:
        it["_completeness"] = _completeness(it)
        cat, tag = classify_book(it)
        it["_category"] = cat
        it["_niche_tag"] = tag
    usable.sort(key=lambda x: -x["_completeness"])
    popular_pool = [x for x in usable if x["_category"] == "热门"]
    niche_pool = [x for x in usable if x["_category"] == "偏门"]
    head_pool = popular_pool[:280] + niche_pool[:120]
    _log(progress, f"[书源] 头部抽查池 {len(head_pool)} 个 (热门 {min(280,len(popular_pool))} / 偏门 {min(120,len(niche_pool))})")

    # ---- 官网可打开检查 (10 并发) ----
    _log(progress, "[书源] 检查头部池官网是否可打开 (10 并发, 超时 5s)")
    reachable = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(_check_url_reachable, it["bookSourceUrl"]): it for it in head_pool}
        for fut in as_completed(futs):
            it = futs[fut]
            reachable[it["bookSourceUrl"]] = fut.result()
    n_unreachable = sum(1 for it in head_pool if not reachable.get(it["bookSourceUrl"]))
    _log(progress, f"[书源] 官网可打开 {len(head_pool)-n_unreachable}/{len(head_pool)}, 打不开 {n_unreachable} 个")

    # ---- 搜索抽查: 取头部池前 200 个 (热门 140 + 偏门 60), 3 本书各测一次 ----
    # 修复 C: 三态结果 True 通过 / False 失败 / None 跳过 (JS 模板、POST 环境拦截)。
    # 跳过不计入 0 分: search_pass 为 None 表示 3 本全跳过, tested/skipped 字段如实记录。
    head_sorted = sorted(head_pool, key=lambda x: (not reachable.get(x["bookSourceUrl"]), -x["_completeness"]))
    test_pop = [x for x in head_sorted if x["_category"] == "热门"][:140]
    test_niche = [x for x in head_sorted if x["_category"] == "偏门"][:60]
    test_pool = test_pop + test_niche
    _log(progress, f"[书源] 搜索抽查 {len(test_pool)} 个源 × 3 本书 (8 并发, 每请求超时 6s)")
    _post_blocked = _is_post_blocked_env()
    _log(progress, f"[书源] POST 环境检测: {'本机出口拦截 POST (httpbin POST 也超时), POST 源将标记为未测试' if _post_blocked else 'POST 正常, 可实测 POST 源'}")
    search_pass = {}  # url -> {"pass": int|None, "tested": int, "skipped": int, "details": {title: True|False|None}, "reasons": {title: str}}
    with ThreadPoolExecutor(max_workers=8) as ex:
        fut_map = {}
        for it in test_pool:
            for title, author in TEST_BOOKS:
                fut = ex.submit(_search_test_one_detailed, it, title, author)
                fut_map[fut] = (it["bookSourceUrl"], title)
        for fut in as_completed(fut_map):
            url, title = fut_map[fut]
            ok, reason = fut.result()
            d = search_pass.setdefault(url, {"pass": 0, "tested": 0, "skipped": 0,
                                            "details": {}, "reasons": {}})
            d["details"][title] = ok
            d["reasons"][title] = reason
            if ok is None:
                d["skipped"] += 1
            else:
                d["tested"] += 1
                if ok:
                    d["pass"] += 1
    for v in search_pass.values():
        if v["tested"] == 0:
            v["pass"] = None
    # 分布只统计至少测了 1 本的源; 全跳过的源单独计 skipped, 不进 0-3 分布
    dist = {0: 0, 1: 0, 2: 0, 3: 0}
    n_skipped_sources = 0
    n_partial_skipped = 0
    skip_reasons = {}
    for v in search_pass.values():
        if v["tested"] == 0:
            n_skipped_sources += 1
        else:
            dist[v["pass"]] += 1
            if v["skipped"] > 0:
                n_partial_skipped += 1
        for reason in v["reasons"].values():
            if reason.startswith("skipped:"):
                key = reason.split(":", 1)[1].split(":")[0] if ":" in reason else reason
                # 归一化: env_post_blocked / js_url_template / js_template_remaining 等
                if "env_post" in reason:
                    key = "env_post_blocked"
                elif "js_url" in reason:
                    key = "js_url_template"
                elif "js_template" in reason:
                    key = "js_template"
                elif "cookie" in reason:
                    key = "cookie_template"
                skip_reasons[key] = skip_reasons.get(key, 0) + 1
    _log(progress, f"[书源] 搜索抽查分布 (已测 {len(test_pool)-n_skipped_sources} 源): "
                   f"3/3 通过 {dist[3]} / 2/3 通过 {dist[2]} / 1/3 通过 {dist[1]} / 0/3 通过 {dist[0]} / "
                   f"全跳过 {n_skipped_sources} 源 (部分跳过 {n_partial_skipped} 源)")

    # ---- 第二轮复测: 2/3 通过的源, 把没通过的那本再测一次, 确认是否偶发失败 ----
    # 只复测 tested==3 且 pass==2 的源, 跳过的项不复测 (复测也测不了)
    retry_targets = []
    for it in test_pool:
        sp = search_pass.get(it["bookSourceUrl"], {"pass": 0, "tested": 0, "details": {}})
        if sp.get("pass") == 2 and sp.get("tested") == 3:
            for title, author in TEST_BOOKS:
                if sp["details"].get(title) is False:
                    retry_targets.append((it, title, author))
    if retry_targets:
        _log(progress, f"[书源] 第二轮复测 {len(retry_targets)} 次 (2/3 通过源的失败项)")
        with ThreadPoolExecutor(max_workers=8) as ex:
            fut_map = {ex.submit(_search_test_one_detailed, it, t, a): (it["bookSourceUrl"], t)
                       for it, t, a in retry_targets}
            for fut in as_completed(fut_map):
                url, title = fut_map[fut]
                ok, reason = fut.result()
                if ok is True:
                    search_pass[url]["details"][title] = True
                    search_pass[url]["reasons"][title] = reason
                    search_pass[url]["pass"] += 1
        dist2 = {0: 0, 1: 0, 2: 0, 3: 0}
        for v in search_pass.values():
            if v["tested"] > 0:
                dist2[v["pass"]] += 1
        _log(progress, f"[书源] 复测后分布: 3/3 通过 {dist2[3]} / 2/3 通过 {dist2[2]} / 1/3 通过 {dist2[1]} / 0/3 通过 {dist2[0]}")
        dist = dist2

    # ---- 最终选取: 搜索通过率 > 齐全度 > 官网可打开, 105 热门 + 45 偏门 = 150 ----
    # 未测试 (pass=None) 的源按中性 1 分排序 (高于真 0/3, 低于 2/3), 不删 0/3 源凑数
    def _book_rank(it):
        sp = search_pass.get(it["bookSourceUrl"], {"pass": None})
        eff = sp["pass"] if sp["pass"] is not None else 1
        return (-eff, -it["_completeness"], not reachable.get(it["bookSourceUrl"], False))
    test_pool_sorted = sorted(test_pool, key=_book_rank)
    pop_final = [x for x in test_pool_sorted if x["_category"] == "热门"][:105]
    niche_final = [x for x in test_pool_sorted if x["_category"] == "偏门"][:45]
    kept = pop_final + niche_final
    # 不足 150 时从头部池其余源按同样排序补到 100 (最低线), 并标记
    # 注意: 0/3 源不删除 (不为数字好看删源); 未测试源 (pass=None) 也不删除,
    # 只是按中性分排序, 详见 kept_sources 的 search_pass/tested/skipped 字段
    threshold_note = ""
    ge2 = dist[2] + dist[3]
    if _post_blocked:
        threshold_note = (f"本机出口拦截 POST (httpbin.org/post 也超时, GET 正常), "
                          f"POST 源无法在本机实测, {n_skipped_sources} 个源 3 本全跳过、"
                          f"{n_partial_skipped} 个源部分跳过, 均标记为 search_pass=null、未计入 0/3, "
                          f"不要把它们当成源本身坏; 需在能 POST 的正常环境 (Render/VPS) 重跑 fetcher.py "
                          f"才能得到 POST 源的真实通过率。")
    if ge2 < MAX_BOOK_SOURCES:
        threshold_note += (f"搜索 2/3 以上通过的 (已测源中) 只有 {ge2} 个, 不足 {MAX_BOOK_SOURCES}, "
                           f"已用 1/3、0/3 或未测试但官网可打开且规则齐全的源补到 {len(kept)} 个, "
                           "详见 kept_sources 的 search_pass/tested/skipped 字段; 补录的源请导入后自行搜书验证, "
                           "不要只看数量。0/3 通过不一定等于源本身坏: 部分站是 GBK 编码/反爬/小站无此书 "
                           "(如笔趣阁 search.php 返回'搜索没有结果'是真无结果, 999wx 404 是真失效), "
                           "抽查方法见 README'筛选标准'。")
    if len(kept) < MAX_BOOK_SOURCES:
        rest = [x for x in head_pool if x not in kept]
        rest.sort(key=_book_rank)
        need = min(MAX_BOOK_SOURCES - len(kept), len(rest))
        kept += rest[:need]
    if len(kept) < MIN_BOOK_SOURCES:
        threshold_note += " 仍不足 100, 本次结果偏少, 请检查上游是否大面积失效。"
    kept.sort(key=_book_rank)

    out_items = []
    kept_records = []
    for it in kept:
        clean = {k: v for k, v in it.items() if not k.startswith("_")}
        out_items.append(clean)
        sp = search_pass.get(it["bookSourceUrl"], {"pass": None, "tested": 0, "skipped": 0,
                                                   "details": {}, "reasons": {}})
        kept_records.append({
            "bookSourceName": it.get("bookSourceName"),
            "bookSourceUrl": it.get("bookSourceUrl"),
            "category": it["_category"], "niche_tag": it["_niche_tag"],
            "search_pass": sp["pass"], "search_tested": sp.get("tested", 0),
            "search_skipped": sp.get("skipped", 0),
            "search_details": sp["details"], "search_reasons": sp.get("reasons", {}),
            "completeness": it["_completeness"],
            "url_reachable": reachable.get(it["bookSourceUrl"]),
        })
    niche_kept = [r for r in kept_records if r["category"] == "偏门"]

    with open(os.path.join(out_dir, "books.json"), "w", encoding="utf-8") as f:
        json.dump(out_items, f, ensure_ascii=False, indent=2)
    meta = {
        "generated_at": now_iso(),
        "book_count": len(out_items),
        "target": f"{MIN_BOOK_SOURCES}-{MAX_BOOK_SOURCES} (目标 {MAX_BOOK_SOURCES}, 约 70% 热门 + 30% 偏门)",
        "popular_count": sum(1 for r in kept_records if r["category"] == "热门"),
        "niche_count": sum(1 for r in kept_records if r["category"] == "偏门"),
        "total_candidates_after_basic_filter": len(candidates),
        "usable_after_rule_filter": len(usable),
        "head_pool_size": len(head_pool),
        "search_tested": len(test_pool),
        "search_coverage": f"{len(test_pool)}/{len(candidates)} 候选源做了 3 本书搜索抽查 (头部池), 其余未逐个搜索",
        "search_pass_distribution": dist,
        "search_pass_distribution_note": "分布只统计至少测了 1 本的源; 全跳过的源不进 0-3 分布, 见 search_skipped_sources",
        "search_pass_ge2": dist[2] + dist[3],
        "search_skipped_sources": n_skipped_sources,
        "search_partially_skipped_sources": n_partial_skipped,
        "search_skip_reasons": skip_reasons,
        "env_post_blocked": _post_blocked,
        "env_post_check": _ENV_POST_CHECK,
        "filtered_invalid": n_invalid,
        "filtered_non_text_type": n_non_text,
        "filtered_adult_or_non_novel": n_adult,
        "filtered_rule_incomplete": n_rule_incomplete,
        "filtered_no_search": n_no_search,
        "filtered_login_paywall": n_login_paywall,
        "filtered_url_unreachable_in_head_pool": n_unreachable,
        "threshold_note": threshold_note,
        "test_books": [t for t, _ in TEST_BOOKS],
        "adult_keywords": ADULT_KEYWORDS,
        "sources": per_source,
        "kept_sources": kept_records,
        "niche_kept": niche_kept,
        "notes": [
            "只保留 bookSourceType==0 的文字小说源; 成人/短剧/有声/漫画按关键词表过滤, 单字'黄'不用作过滤词以免误删正常小说。",
            "大陆速度未实测, 书籍全不全无法仅凭清单判断, 导入后请自行搜热门书、打开一章验证。",
            "搜索抽查只覆盖头部池, 未覆盖的候选源未逐个搜索, 覆盖率见 search_coverage。",
            "修复 C: searchUrl 尾部 @js: 先剥掉再解析 (否则 POST 配置解析失败被误判成 GET); 开头 {{cookie...}} 剥掉后解析剩余 URL, 开头 {{url=...}} 跳过不判 0 分; POST 源若因本机出口拦截 POST 而无法实测, 标记为 search_pass=null, 不要当成源本身坏, 需在正常环境重测。0/3 源不删除, 如实保留。",
        ],
    }
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    _log(progress, f"[书源] 完成: {len(out_items)} 个书源 (热门 {meta['popular_count']} / 偏门 {meta['niche_count']}, 搜索 2/3 以上通过 {meta['search_pass_ge2']} 个, 全跳过 {n_skipped_sources} 个)")
    return meta


# ---------------------------------------------------------------- 总入口
def fetch_all(data_dir, progress=None, interval_days=7):
    os.makedirs(data_dir, exist_ok=True)
    tv_meta = fetch_tvbox(data_dir, progress=progress)
    books_meta = fetch_books(data_dir, progress=progress)
    status = {
        "last_update": now_iso(),
        "next_update": (datetime.now(timezone.utc) + timedelta(days=interval_days)).isoformat().replace("+00:00", "Z"),
        "interval_days": interval_days,
        "tvbox_sites": tv_meta.get("site_count", 0),
        "book_sources": books_meta.get("book_count", 0),
    }
    with open(os.path.join(data_dir, "status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    return tv_meta, books_meta


if __name__ == "__main__":
    data_dir = os.environ.get("DATA_DIR", "data")
    if len(sys.argv) > 1:
        data_dir = sys.argv[1]
    interval = int(os.environ.get("UPDATE_INTERVAL_DAYS", "7"))
    t0 = time.time()
    tv_meta, books_meta = fetch_all(data_dir, interval_days=interval)
    print(f"完成: TVBox {tv_meta['site_count']} 站, 书源 {books_meta['book_count']} 个, 用时 {time.time()-t0:.1f}s")
