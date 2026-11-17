#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TVBox + Legado 书源聚合抓取器 v2 — 质量优先, 宁缺毋滥, 多轮验证
- TVBox: 最终只留 80-150 站 (目标 120, 约 70% 热门 + 30% 偏门), 不是全量
- 书源: 最终只留 100-200 个 (目标 150, 约 70% 热门 + 30% 偏门)
- TVBox 实测: 每个候选站 api 做两轮测速 (TTFB/总耗时/200KB 内带宽/能否返回 vod 列表),
  多通道对比 (配置 JSON: jsDelivr -> ghproxy -> raw; 站点 api: 直连为主,
  若为 GitHub/jsDelivr 托管则另试镜像取最快), 门槛 TTFB<1.5s、总耗时<3s
- 书源抽查: 规则齐全 + 官网可打开 + Node 四步校验 (搜索->详情->目录->正文,
  @js: 规则进沙箱执行、POST 按配置发), 测试书男女频混合 (斗破苍穹/凡人修仙传/
  庆余年 + 缠珑/知否知否应是绿肥红瘦; 女频源用女频 5 本题库), 5 本过 3 本
  才算"搜得全", 正文系统性不符 (content_bad>=2) 不进最终 150, 另做第二轮复测确认
- 大陆速度未实测: 本机在新加坡, 用"新加坡 VM 实测 + 国内 CDN 启发式",
  部署后需大陆实测 (方法见 README"筛选标准"与 itdog/boce/17ce 思路)
- 只输出真实抓到的数据, 抓不到/测不过的上游如实记录在 meta.json, 不编造、不填充
"""
import hashlib
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
# 数量目标 (环境变量可覆盖, 2026-11-13 并入他方项目配置口径):
# TVBOX_SITE_COUNT / NOVEL_SOURCE_COUNT 对应对方 config 的 tvboxCount/novelCount。
MAX_TVBOX_SITES = int(os.environ.get("TVBOX_SITE_COUNT", "120") or 120)
MIN_TVBOX_SITES = 80
MAX_BOOK_SOURCES = int(os.environ.get("NOVEL_SOURCE_COUNT", "150") or 150)
MIN_BOOK_SOURCES = 100
# 书源校验池上限 (对方 NOVEL_VALIDATE_MAX=1600; 我方头部池 400、实测池 200,
# 200 已是本机 60 分钟预算内的上限, 默认 200)。
NOVEL_VALIDATE_MAX = int(os.environ.get("NOVEL_VALIDATE_MAX", "200") or 200)
# yck 按下载量取前 N 个详情 (对方 NOVEL_FETCH_TOP=1200; 我方聚合 id 列表另算)。
NOVEL_FETCH_TOP = int(os.environ.get("NOVEL_FETCH_TOP", "1200") or 1200)
# 书源单请求超时 (2026-11-13 并入他方口径: 对方各阶段用 15-20s, 我方原来 6s
# 过严——代理劣化时 6s 会把活站误判为超时; 改为 15s, 仍是质量门槛的一部分)。
BOOK_SEARCH_TIMEOUT = 15.0
# TVBox 测速门槛 (建议值, 见 README"筛选标准")
TTFB_LIMIT_MS = 1500
TOTAL_LIMIT_MS = 3000
SPEED_SAMPLE_MAX_BYTES = 200 * 1024  # 测带宽最多下 200KB

# 可选代理 (2026-11-13, 默认不开): 环境变量 PROXY_URL 或 CHINA_PROXY_URL,
# 值如 http://user:pass@host:port。只用于 TVBox 站点测速、jar/ext 校验与
# 书源搜书抽查 (其余上游抓取仍走默认出口, 避免代理拖慢 GitHub 拉取)。
# 注意: Shifter 免费网页代理 (shifter.io/cn/web-proxy) 是浏览器内嵌的免费
# 工具, 没有可供脚本用的 HTTP 代理地址, 不能接 fetcher; 付费住宅代理才会
# 给代理地址 (Lite 套餐 $10/月起含 5GB, 详见 README)。未配置时为 None,
# requests 走默认出口, 行为与之前一致。
_TEST_PROXIES = None
_PROXY_URL = os.environ.get("PROXY_URL") or os.environ.get("CHINA_PROXY_URL")
if _PROXY_URL:
    _TEST_PROXIES = {"http": _PROXY_URL, "https": _PROXY_URL}

# ---------------------------------------------------------------- 工具
def _ensure_writable_dir(path):
    """确保数据目录可写, 否则报明确的错而不是原始 [Errno 30]。
    Vercel 部署后的文件系统除 /tmp 外全部只读: 若 DATA_DIR 指向项目内
    的 data/, os.makedirs 就会报 [Errno 30] Read-only file system。
    正确做法: 在 Vercel 上把 DATA_DIR 设为 /tmp/tvbox-legado-data
    (app.py 在 VERCEL=1 时已默认这么做), 或只读仓库 data/ 快照不抓取。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        raise RuntimeError(
            f"数据目录 {path} 不可写 ({e})。在 Vercel 上请把 DATA_DIR 设为 "
            f"/tmp/tvbox-legado-data (app.py 在 VERCEL=1 时默认即如此), "
            f"或只读仓库 data/ 快照而不抓取; 本地请检查目录权限。"
        ) from e


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _log(progress, msg):
    print(msg, flush=True)
    if progress:
        try:
            progress(msg)
        except Exception:
            pass


# ---------------- 结构化抓取进度 (供 app.py 前端进度条用) ----------------
# 8 个阶段 (2026-11-13 加第 0 阶段“发现新源”): 发现约 0-10%, TVBox 约 10-53%,
# 书源约 53-92%, 写文件 92-100%。_emit 只在长阶段里每 10-20 个单位报一次,
# 避免日志刷屏; percent 单调递增, 绝不回跳。
STAGE_NAMES = ["发现新源", "抓取 TVBox 上游", "TVBox 测速第 1 轮",
               "TVBox 测速复测（第 2+3 轮）", "TVBox type-3 校验", "抓取书源",
               "书源搜书抽查", "写文件"]
STAGE_TOTAL = len(STAGE_NAMES)
STAGE_SPANS = [(0, 10), (10, 20), (20, 34), (34, 41), (41, 53),
               (53, 64), (64, 92), (92, 100)]
_LAST_PERCENT = [0.0]


# ---------------- 内存诊断 (2026-11-16 循环定位) ----------------
# Render 实例 3 分钟内重启两次、几轮后报 Failed service, 但应用日志里没有
# OOM 字样——重启原因未确认 (候选: 内存、健康检查超时、平台回收; OOM 杀进程
# 一般不打印到应用日志, 只在 Render Events 里可见, 用户那边也没看到)。
# 因此每轮开始、每阶段切换、TVBox 上游循环中每 60 秒、每轮结束都打 RSS,
# 超过 400MB (免费版上限 512MB) 再打一行警告, 下次看 Render 日志就能判断
# 是不是内存一路涨到顶被杀的。只用标准库, 失败则跳过不报错。
# 2026-11-17 书源入口重启定位: TVBox 结束 RSS 已 175MB, 书源入口
# _fetch_aoa_sources 并发全量解析 7 个合集 (3,907 条) 又把 items 全留
# 在内存 (调用方只用 count, 主循环还会重拉一遍), 4 并发大 JSON 解析
# 是 OOM 主因 (见 _fetch_bundle 注释)。故加两道防线: (1) 单次响应
# 20MB 上限 (MAX_FETCH_BYTES, 流式读超限即弃); (2) 书源主循环每次
# 抓上游前查 RSS, 超过 MEM_LIMIT_MB (默认 420MB) 即收尾写 partial,
# 宁可少抓也不被平台 OOM 杀掉 (重启会清空无持久盘的 data/ 从头再来)。
MEM_WARN_MB = int(os.environ.get("MEM_WARN_MB", "400") or 400)
MEM_LIMIT_MB = int(os.environ.get("MEM_LIMIT_MB", "420") or 420)
MAX_FETCH_BYTES = int(os.environ.get("MAX_FETCH_BYTES", str(20 * 1024 * 1024))
                      or 20 * 1024 * 1024)
_LAST_STAGE_IDX = [0]


def _rss_mb():
    """当前进程 RSS (MB)。Linux 读 /proc/self/status, 失败回退到
    resource.ru_maxrss (峰值, 仅作参考), 再失败返回 None。"""
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except Exception:
        pass
    try:
        import resource
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:
        return None


def _log_mem(progress, where):
    """打一行内存占用; 超过 MEM_WARN_MB 再打一行警告。失败不报错。"""
    rss = _rss_mb()
    if rss is None:
        return
    _log(progress, f"[内存] {where} RSS {rss} MB")
    if rss >= MEM_WARN_MB:
        _log(progress, f"[内存警告] RSS {rss} MB 已超过 {MEM_WARN_MB} MB "
                       f"(免费版上限 512MB), 有 OOM 被杀风险, 请看 Render Events")


def _emit(progress, stage_index, frac, current, done=None, total=None, log=None):
    """上报结构化进度 dict 给 progress 回调, 同时可附带一条字符串日志。
    frac 为本阶段内 0-1 的进度。progress 为 None 时只打印日志
    (python3 fetcher.py 单独运行就是这种情况, 不报错)。
    阶段切换时打一行内存占用 (2026-11-16 循环定位用)。"""
    if stage_index != _LAST_STAGE_IDX[0]:
        prev = _LAST_STAGE_IDX[0]
        _LAST_STAGE_IDX[0] = stage_index
        if prev:
            _log_mem(progress, f"阶段 {prev}（{STAGE_NAMES[prev - 1]}）结束 / "
                               f"阶段 {stage_index}（{STAGE_NAMES[stage_index - 1]}）开始")
        else:
            _log_mem(progress, f"进入阶段 {stage_index}（{STAGE_NAMES[stage_index - 1]}）")
    lo, hi = STAGE_SPANS[stage_index - 1]
    pct = lo + max(0.0, min(1.0, frac)) * (hi - lo)
    pct = max(pct, _LAST_PERCENT[0])  # 单调递增, 不回跳
    pct = min(100.0, pct)
    _LAST_PERCENT[0] = pct
    if log:
        _log(progress, log)
    if progress:
        try:
            progress({"type": "progress", "stage": STAGE_NAMES[stage_index - 1],
                      "stage_index": stage_index, "stage_total": STAGE_TOTAL,
                      "percent": round(pct, 1), "current": current,
                      "done": done, "total": total})
        except Exception:
            pass


def _atomic_write_json(path, data):
    """原子写 JSON (先写 .tmp 再 rename): 长抓取中途被杀也不会留下半截文件。
    Render 免费版休眠可能冻住/杀掉后台抓取, 靠这个 + 阶段检查点保留已测数据。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------- 总时间预算 (2026-11-13: 60 分钟, 慢无所谓, 但要有上限) ----------------
# fetch_all 开始时起算, 各阶段循环里检查, 超时即收尾 (已抓到的数据已落盘)。
# 环境变量 FETCH_BUDGET_MINUTES 可改, 默认 60。
FETCH_BUDGET_MINUTES = int(os.environ.get("FETCH_BUDGET_MINUTES", "60") or 60)
_DEADLINE = [None]  # 绝对时间戳 (time.time()), fetch_all 里设置


def _deadline_left_s():
    if _DEADLINE[0] is None:
        return None
    return _DEADLINE[0] - time.time()


def _deadline_hit():
    left = _deadline_left_s()
    return left is not None and left <= 0


def github_channels(owner, repo, branch, path):
    """jsDelivr -> ghproxy -> raw, 中文路径按段 URL 编码。"""
    enc = quote(path, safe="/")
    return [
        f"https://cdn.jsdelivr.net/gh/{owner}/{repo}@{branch}/{enc}",
        f"https://ghproxy.net/https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{enc}",
        f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{enc}",
    ]


def _host_of(url):
    """URL 的 host (小写, 不带端口), 供死域名缓存与失败分类用。"""
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


# ---------------- 失败分类 / 降噪 / 死域名缓存 (2026-11-16) ----------------
# 背景: 一轮抓取里同一个死域名 (pandown.pro 12 秒超时、gitee 423、DNS 失败、
# SSL 证书过期、ghproxy 不可达、404) 会在几十个 seed 里反复出现, 每条都打
# 日志还各烧 12 秒超时。处理: (a) 同一 (host, 失败原因) 一轮内只记一次详情,
# 其余计数汇总; (b) 同一 host 网络层连续失败达阈值 (默认 3 次) 后 24 小时内
# 直接跳过 (持久化在 DATA_DIR/dead_hosts.json, 重启后仍有效); (c) 每轮结束
# 打一行失败汇总。注意: HTTP 状态码 (404/423 等) 说明 host 还活着, 不计入
# 死域名; 只有网络层失败 (超时/DNS/被拒/SSL) 才计数; “网络不可达”多半是
# 本机出口问题, 也不计入——否则出口一抖就把所有 host 拉黑。
# 缓存只影响“跳过重试”, 不影响已抓到的数据; 24 小时后自动放行再试。
DEAD_HOST_FAILS = int(os.environ.get("DEAD_HOST_FAILS", "3") or 3)
DEAD_HOST_SKIP_HOURS = int(os.environ.get("DEAD_HOST_SKIP_HOURS", "24") or 24)
_DEAD_COUNT_KINDS = {"连接超时", "DNS解析失败", "连接被拒/重置", "SSL证书"}
_DEAD_HOSTS = {}  # host -> {"fails": int, "last_fail": iso, "dead_until": iso|None}
_DEAD_HOSTS_DIR = None
_DEAD_HOSTS_LAST_SAVE = [0.0]
_FAIL = {"by_kind": {}, "detail_seen": set(), "skipped_dead": {}, "total": 0}


def _fail_reset():
    """新一轮抓取开始时清空失败统计 (fetch_all 里调)。"""
    _FAIL["by_kind"].clear()
    _FAIL["detail_seen"].clear()
    _FAIL["skipped_dead"].clear()
    _FAIL["total"] = 0


def _fail_count(kind):
    _FAIL["by_kind"][kind] = _FAIL["by_kind"].get(kind, 0) + 1
    _FAIL["total"] += 1


def _fail_summary_text():
    parts = [f"{k} {v} 次" for k, v in
             sorted(_FAIL["by_kind"].items(), key=lambda kv: -kv[1])]
    skipped = sum(_FAIL["skipped_dead"].values())
    s = f"[失败汇总] 本轮共 {_FAIL['total']} 次失败" + \
        ("：" + " / ".join(parts) if parts else " (0 次)")
    if skipped:
        s += f"；死域名跳过 {skipped} 次（{len(_FAIL['skipped_dead'])} 个 host）"
    return s


def _classify_fetch_error(err_text, status=None):
    """把一次请求失败归类 (供失败汇总计数)。err_text 为异常字符串,
    status 为 HTTP 状态码 (有状态码时直接按状态码归类)。"""
    if status is not None:
        return f"HTTP {status}"
    t = err_text or ""
    if ("ConnectTimeout" in t or "ReadTimeout" in t or "Timeout" in t
            or "timed out" in t):
        return "连接超时"
    if ("NameResolutionError" in t or "Failed to resolve" in t
            or "Name or service not known" in t
            or "No address associated with hostname" in t):
        return "DNS解析失败"
    if ("CERTIFICATE_VERIFY_FAILED" in t or "SSLError" in t
            or "certificate has expired" in t):
        return "SSL证书"
    if "Network is unreachable" in t:
        return "网络不可达"
    if ("Connection refused" in t or "Connection reset" in t
            or "Connection aborted" in t or "ConnectionResetError" in t):
        return "连接被拒/重置"
    return "其它网络错误"


def _dead_hosts_init(data_dir):
    """读 DATA_DIR/dead_hosts.json 到内存 (fetch_all 开始时调一次)。
    文件不存在即空缓存, 不报错。"""
    global _DEAD_HOSTS_DIR
    _DEAD_HOSTS_DIR = data_dir
    _DEAD_HOSTS.clear()
    try:
        with open(os.path.join(data_dir, "dead_hosts.json"), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for host, rec in (data.get("hosts") or {}).items():
                if isinstance(rec, dict) and isinstance(rec.get("fails"), int):
                    _DEAD_HOSTS[str(host).lower()] = rec
    except Exception:
        pass


def _dead_hosts_save(force=False):
    """死域名缓存落盘 (节流: 最多每 30 秒写一次, 除非 force)。
    只在 fetch_all 初始化后才写; 写失败不报错。"""
    if _DEAD_HOSTS_DIR is None:
        return
    now = time.monotonic()
    if not force and now - _DEAD_HOSTS_LAST_SAVE[0] < 30:
        return
    _DEAD_HOSTS_LAST_SAVE[0] = now
    try:
        os.makedirs(_DEAD_HOSTS_DIR, exist_ok=True)
        _atomic_write_json(os.path.join(_DEAD_HOSTS_DIR, "dead_hosts.json"),
                           {"hosts": _DEAD_HOSTS,
                            "updated_at": now_iso(),
                            "fail_threshold": DEAD_HOST_FAILS,
                            "skip_hours": DEAD_HOST_SKIP_HOURS})
    except Exception:
        pass


def _dead_host_is_dead(host):
    """host 连续失败达阈值且未过跳过期 -> True (本轮不再试)。
    过期则清掉记录放行再试; 记录损坏时按已死处理并补一个 24 小时期限。"""
    if not host:
        return False
    rec = _DEAD_HOSTS.get(host)
    if not rec:
        return False
    if int(rec.get("fails", 0)) < DEAD_HOST_FAILS:
        return False
    until = rec.get("dead_until")
    if until:
        try:
            if datetime.fromisoformat(until.replace("Z", "+00:00")) > datetime.now(timezone.utc):
                return True
        except Exception:
            return True
        _DEAD_HOSTS.pop(host, None)  # 已过期: 放行再试
        _dead_hosts_save()
        return False
    rec["dead_until"] = (datetime.now(timezone.utc)
                         + timedelta(hours=DEAD_HOST_SKIP_HOURS)).isoformat().replace("+00:00", "Z")
    _dead_hosts_save(force=True)
    return True


def _dead_host_fail(host):
    """记一次 host 网络层失败; 达阈值时设 24 小时跳过期并立即落盘。"""
    if not host:
        return
    rec = _DEAD_HOSTS.setdefault(host, {"fails": 0, "last_fail": None, "dead_until": None})
    rec["fails"] = int(rec.get("fails", 0)) + 1
    rec["last_fail"] = now_iso()
    if rec["fails"] >= DEAD_HOST_FAILS and not rec.get("dead_until"):
        rec["dead_until"] = (datetime.now(timezone.utc)
                             + timedelta(hours=DEAD_HOST_SKIP_HOURS)).isoformat().replace("+00:00", "Z")
        _dead_hosts_save(force=True)
    else:
        _dead_hosts_save()


def _dead_host_ok(host):
    """host 一次成功即清掉失败记录 (死域名复活)。"""
    if host and host in _DEAD_HOSTS:
        _DEAD_HOSTS.pop(host, None)
        _dead_hosts_save()


def fetch_text(urls, progress=None, label=""):
    """按顺序尝试多个 URL, 返回 (text, used_url, content_type),
    全部失败返回 (None, None, None)。
    2026-11-16 改: 返回值多了 content_type (供调用方判断“响应根本不是 JSON”);
    之前返回 (text, used_url) 二元组, 调用方已全部改成三元组。
    死域名缓存: 同一 host 网络层连续失败达阈值 (默认 3 次) 后 24 小时内直接
    跳过, 不再烧超时时间; 同一 (host, 失败原因) 一轮内只记一次详情日志,
    其余只计数 (见 _fail_summary_text)。"""
    last_err = ""
    last_kind = None
    content_type = None
    for url in urls:
        host = _host_of(url)
        if host and _dead_host_is_dead(host):
            _FAIL["skipped_dead"][host] = _FAIL["skipped_dead"].get(host, 0) + 1
            dkey = ("dead-skip", host)
            if dkey not in _FAIL["detail_seen"]:
                _FAIL["detail_seen"].add(dkey)
                _log(progress, f"  跳过死域名 {host}（已连续失败 "
                               f"{_DEAD_HOSTS.get(host, {}).get('fails', DEAD_HOST_FAILS)} 次，"
                               f"{DEAD_HOST_SKIP_HOURS} 小时内不再试）")
            continue
        try:
            # 2026-11-17: 流式读 + MAX_FETCH_BYTES 上限, 防单个超大响应
            # (如几百 MB 的错误页/死循环分块流) 一次撑爆内存导致 OOM 被杀。
            # 超限按失败处理 (记"响应过大"), 不抛异常、不计死域名。
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True)
            if r.status_code == 200:
                buf = bytearray()
                too_big = False
                try:
                    for chunk in r.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        buf.extend(chunk)
                        if len(buf) > MAX_FETCH_BYTES:
                            too_big = True
                            break
                finally:
                    r.close()
                if too_big:
                    last_err = f"响应过大 (>{MAX_FETCH_BYTES // 1024 // 1024}MB)"
                    last_kind = "响应过大"
                else:
                    text = bytes(buf).decode(r.encoding or "utf-8", errors="replace")
                    if text.strip():
                        content_type = r.headers.get("Content-Type", "")
                        if host:
                            _dead_host_ok(host)
                        return text, url, content_type
                    last_err = "空响应"
                    last_kind = _classify_fetch_error("")
            else:
                r.close()
                # 有 HTTP 响应 (哪怕 404/423) 说明 host 还活着: 只分类计数, 不计死域名。
                last_err = f"HTTP {r.status_code}"
                last_kind = _classify_fetch_error("", status=r.status_code)
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            last_kind = _classify_fetch_error(str(e))
            # 只有网络层失败才计死域名; “网络不可达”是本机出口问题, 不计入。
            if host and last_kind in _DEAD_COUNT_KINDS:
                _dead_host_fail(host)
        # 降噪: 同一 (host, kind) 一轮内只记一次详情, 其余只计数。
        if last_kind:
            _fail_count(last_kind)
            dkey = (host, last_kind)
            if dkey not in _FAIL["detail_seen"]:
                _FAIL["detail_seen"].add(dkey)
                _log(progress, f"  尝试失败 {url} -> {last_err}")
    return None, None, None


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


# ---------------------------------------------------------------- 自动发现新源
# 每轮抓取前先联网搜索找新上游, 而不是只靠写死的 TVBOX_SOURCES / LEGADO_SOURCES。
# 方向 (用户 2026-11-13 11:20 定): 搜索面尽量铺开、筛选从严、慢无所谓,
# 全程总预算 60 分钟。发现 ≠ 收录: 候选上游仍要走同一套格式校验、去重、
# 受限内容过滤、速度/搜书实测, 不合规照样淘汰, 结果记在 meta.json 的 discovered_upstreams。
# 开关: DISCOVERY_ENABLED (默认 1 开启, 设 0 关闭, 只用固定清单)。
# 可选: GITHUB_TOKEN (有则 GitHub 搜索限流从 10 次/分钟提到 30 次/分钟,
# 且能用 code search 补一轮; 没有也不报错, 只是搜得少一点)。
# 限流/超时/无候选都只降级 (记日志、跳过发现阶段), 绝不让整轮抓取失败。
DISCOVERY_MAX_TVBOX = 50   # 每轮最多新增 TVBox 候选上游
DISCOVERY_MAX_BOOKS = 30   # 每轮最多新增书源候选上游
DISCOVERY_BUDGET_S = 900   # 发现阶段最多 15 分钟, 超时即停
TVBOX_SEARCH_QUERIES = ["tvbox", "tvbox-config", "tvbox源", "tvbox配置",
                        "dianshi", "电视源", "tvbox config", "tvbox json"]
BOOK_SEARCH_QUERIES = ["legado", "阅读书源", "shuyuan", "yuedu",
                       "book-source", "legado book source"]
TVBOX_CONFIG_PATHS = ["tvbox.json", "box.json", "tvbox_recommended.json",
                      "jsm.json", "js.json", "config.json",
                      "dist/official.json", "output/单仓聚合.json"]
BOOK_FILE_PATHS = ["books.json", "all.json", "shuyuan", "shuyuan.json"]
YCK_MIRRORS = ["https://www.yck2026.fun", "https://www.yckceo.com",
               "https://www.yck2026.top"]


def _discovery_enabled():
    return os.environ.get("DISCOVERY_ENABLED", "1") != "0"


def _github_token():
    return (os.environ.get("GITHUB_TOKEN") or "").strip()


def _github_headers():
    h = {"User-Agent": HEADERS["User-Agent"], "Accept": "application/vnd.github+json"}
    tok = _github_token()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _search_pace():
    """GitHub 搜索限流: 未登录 10 次/分钟, 登录 30 次/分钟。调用间歇一下, 别顶着限流撞。
    只有搜索 API 需要 pacing, 普通 raw 文件下载不走这个限流。"""
    time.sleep(2.5 if _github_token() else 7.0)


def _github_search(query, kind="repositories", page=1, per_page=30, progress=None):
    """GitHub 搜索 API (按最近更新排序)。返回 (items, rate_limited)。
    403/429 即限流: 记日志返回限流标志, 调用方降级继续, 不抛异常。"""
    url = f"https://api.github.com/search/{kind}"
    try:
        r = requests.get(url, headers=_github_headers(),
                         params={"q": query, "sort": "updated", "order": "desc",
                                 "per_page": per_page, "page": page}, timeout=15)
    except Exception as e:  # noqa: BLE001
        _log(progress, f"  [发现] GitHub 搜索请求失败 {query} p{page}: {e}")
        return [], False
    if r.status_code in (403, 429):
        reset = r.headers.get("X-RateLimit-Reset")
        _log(progress, f"  [发现] GitHub 搜索限流 (HTTP {r.status_code}, reset={reset}), "
                       "停止继续搜索, 用已找到的候选继续")
        return [], True
    if r.status_code != 200:
        _log(progress, f"  [发现] GitHub 搜索 HTTP {r.status_code} {query} p{page}")
        return [], False
    try:
        return r.json().get("items", []), False
    except Exception:
        return [], False


def _head_ok(url, timeout=8):
    try:
        r = requests.head(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        return 200 <= r.status_code < 400
    except Exception:
        return False


def _discovery_channels(owner, repo, branch, path):
    """发现探测专用通道: 只用已允许的域名 (jsDelivr + raw), 不走 ghproxy.net
    (新域名, 每碰一次就要用户点一次允许; 且 jsDelivr/raw 通常已够用)。
    注意: 正式抓取仍用 github_channels (jsDelivr -> ghproxy -> raw 三通道),
    那是原有核心逻辑, 不在这里改。"""
    enc = quote(path, safe="/")
    return [
        f"https://cdn.jsdelivr.net/gh/{owner}/{repo}@{branch}/{enc}",
        f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{enc}",
    ]


def _probe_tvbox_candidate(owner, repo, branch, paths, progress=None):
    """探测一个仓库里的 TVBox 配置文件: 先 HEAD 找存在的路径, 再整文件拉下来做
    JSON 校验 (含 sites 数组、或多仓索引 urls 才算数)。返回 (path, used_url, 站数) 或 None。"""
    for path in paths:
        raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{quote(path, safe='/')}"
        if not _head_ok(raw):
            continue
        text, used, _ct = fetch_text(_discovery_channels(owner, repo, branch, path), progress=None)
        if text is None:
            continue
        try:
            obj = json.loads(text, strict=False)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        sites = obj.get("sites")
        urls = obj.get("urls")
        n = len(sites) if isinstance(sites, list) else 0
        if n > 0 or (isinstance(urls, list) and urls):
            return path, used, n
    return None


def _probe_book_candidate(owner, repo, branch, paths, progress=None):
    """探测一个仓库里的书源文件: JSON 数组且含 bookSourceUrl 的条目 >= 20 才算数。
    返回 (path, used_url, 条数) 或 None。"""
    for path in paths:
        raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{quote(path, safe='/')}"
        if not _head_ok(raw):
            continue
        text, used, _ct = fetch_text(_discovery_channels(owner, repo, branch, path), progress=None)
        if text is None:
            continue
        try:
            data = json.loads(text, strict=False)
        except Exception:
            continue
        if isinstance(data, dict):
            lists = [v for v in data.values() if isinstance(v, list)]
            data = lists[0] if lists else []
        if isinstance(data, list):
            n = sum(1 for it in data if isinstance(it, dict) and it.get("bookSourceUrl"))
            if n >= 20:
                return path, used, n
    return None


# 域名收敛 (2026-11-13, 用户反馈每碰一个新域名就要点一次允许):
# 发现阶段只用已经允许过的域名 (github.com / api.github.com /
# raw.githubusercontent.com / cdn.jsdelivr.net) + 固定 2 个 SearXNG 实例,
# 不再 6 个实例挨个试、也不再用 DuckDuckGo (html.duckduckgo.com 是新域名,
# 且从机房 IP 常返回 202 验证页, 不可靠)。实例失效就换下一个 (最多 2 个),
# 两个都不通则只用 GitHub 搜索 + yck, 绝不为搜索再碰新域名。
SEARXNG_INSTANCES = [
    "https://baresearch.org",  # 2026-11-13 全量实测可用
    "https://searx.be",
]
SEARCH_ENGINE_QUERIES_TVBOX = ["tvbox源", "电视直播源", "tvbox配置",
                               "tvbox github", "tvbox.json"]
SEARCH_ENGINE_QUERIES_BOOKS = ["legado 阅读书源", "阅读书源 github",
                               "legado book source github", "shuyuan 书源"]


# 浏览器 UA: DuckDuckGo HTML 会挡简陋 UA (返回 202 空页), SearXNG 部分实例亦然,
# 搜素引擎请求必须用完整浏览器 UA 才能拿到真实结果页 (2026-11-13 实测)。
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _searx_search(instance, query, progress=None):
    """SearXNG 公开实例 JSON 搜索, 返回结果 URL 列表; 实例不可用返回 None
    (调用方换下一个实例)。只用公开免费实例, 不注册不付费。
    有的实例禁了 JSON (返回机器人验证页), 此时退回解析 HTML 结果页;
    验证页里没有可用外链时仍返回 None。"""
    try:
        r = requests.get(f"{instance}/search",
                         headers={**HEADERS, "User-Agent": BROWSER_UA},
                         params={"q": query, "format": "json"},
                         timeout=12)
        if r.status_code != 200:
            return None
        try:
            data = r.json()
            return [it.get("url", "") for it in data.get("results", [])
                    if it.get("url")]
        except Exception:
            urls = [u for u in re.findall(r'href="(https?://[^"]+)"', r.text)
                    if "searx" not in u and "/search" not in u]
            return urls or None
    except Exception:
        return None


def _extract_github_targets(urls):
    """从搜索结果 URL 里解析 GitHub 仓库与直链文件:
    返回 (repos {(owner, repo)}, files [(owner, repo, branch, path)])。
    raw/jsDelivr 直链带精确路径, 探测时只探那一个文件。"""
    repos = set()
    files = []
    seen_files = set()
    for u in urls or []:
        if not isinstance(u, str):
            continue
        m = re.search(r"github\.com/([^/\s?#]+)/([^/\s?#]+)", u)
        if m:
            repos.add((m.group(1), m.group(2).removesuffix(".git")))
        m = re.search(r"raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)", u)
        if m:
            key = (m.group(1), m.group(2), m.group(3), m.group(4).split("?")[0])
            if key not in seen_files:
                seen_files.add(key)
                files.append(key)
            continue
        m = re.search(r"cdn\.jsdelivr\.net/gh/([^/]+)/([^/@]+)@([^/]+)/(.+)", u)
        if m:
            key = (m.group(1), m.group(2), m.group(3), m.group(4).split("?")[0])
            if key not in seen_files:
                seen_files.add(key)
                files.append(key)
    return repos, files


def _search_engine_targets(queries, progress=None, t_end=None):
    """免费公开搜索引擎补充发现: 先逐个试 SearXNG 公开实例 (通的那个才用),
    全部不通则用 DuckDuckGo HTML。返回 (repos, files, engine_name)。
    任何一个搜索源挂了都只记日志跳过, 不抛异常。"""
    urls = []
    engine = None
    for inst in SEARXNG_INSTANCES:
        if t_end is not None and time.time() > t_end:
            break
        first = _searx_search(inst, queries[0], progress)
        if first is None:
            _log(progress, f"  [发现] SearXNG 实例 {inst} 不可用, 换下一个")
            continue
        engine = f"searxng:{inst}"
        urls.extend(first)
        for q in queries[1:]:
            if t_end is not None and time.time() > t_end:
                break
            res = _searx_search(inst, q, progress)
            if res:
                urls.extend(res)
        break
    if engine is None:
        _log(progress, "  [发现] SearXNG 两个固定实例都不可用, 搜索引擎补充跳过, "
                       "只用 GitHub 搜索 + yck")
        engine = "none"
    _log(progress, f"  [发现] 搜索引擎 ({engine}) 返回 {len(urls)} 条结果链接")
    repos, files = _extract_github_targets(urls)
    return repos, files, engine, urls


def discover_tvbox(progress=None):
    """GitHub 多关键词多页搜索 TVBox 相关仓库, 探测配置文件, 返回 (candidates, rate_limited)。
    candidates 为 TVBOX_SOURCES 同形 dict (多 discovery_source/discovered 字段)。
    已在固定清单里的仓库跳过; fork 跳过 (多为陈旧复制); 每轮最多 50 个。"""
    known = {(s["owner"].lower(), s["repo"].lower()) for s in TVBOX_SOURCES}
    seen_repos = set()
    repos = []  # (full_name, owner, repo, branch, updated_at)
    code_hits = []  # (owner, repo, path) 来自 code search, 有 token 时才有
    rate_limited = False
    t_end = time.time() + DISCOVERY_BUDGET_S
    total_steps = len(TVBOX_SEARCH_QUERIES) * 2
    q_done = 0
    for q in TVBOX_SEARCH_QUERIES:
        if rate_limited or time.time() > t_end or _deadline_hit():
            break
        for page in (1, 2):
            if rate_limited or time.time() > t_end or _deadline_hit():
                break
            items, rate_limited = _github_search(q, "repositories", page, 30, progress)
            q_done += 1
            _emit(progress, 1, q_done / max(1, total_steps),
                  f"发现新源：GitHub 搜索“{q}”第 {page} 页，已收 {len(repos)} 个仓库",
                  q_done, total_steps)
            if rate_limited:
                break
            for it in items:
                fn = (it.get("full_name") or "").lower()
                if not fn or fn in seen_repos:
                    continue
                owner = (it.get("owner") or {}).get("login", "")
                name = it.get("name", "")
                if (owner.lower(), name.lower()) in known:
                    continue
                if it.get("fork"):
                    continue
                seen_repos.add(fn)
                repos.append((it["full_name"], owner, name,
                              it.get("default_branch") or "main",
                              it.get("updated_at") or ""))
            _search_pace()
    # code search 补一轮: 搜 tvbox.json / box.json 文件名 (需要 token, 未登录不可用)。
    # 2026-11-17 发现调研扩展: 再按内容搜 CMS 特征串 (api.php/provide/vod、
    # vod_play_url、vod_name), 能挖到文件名不叫 tvbox.json 的配置 (如 LunaTV 类
    # 清单、CMS 资源站)。code search 限流 10 次/分钟, 每问之间 _search_pace()。
    if _github_token() and not rate_limited and time.time() <= t_end and not _deadline_hit():
        for q in ["tvbox.json in:path", "box.json in:path",
                  "\"api.php/provide/vod\" extension:json",
                  "\"vod_play_url\" extension:json",
                  "\"vod_name\" extension:json"]:
            if rate_limited or time.time() > t_end or _deadline_hit():
                break
            items, rate_limited = _github_search(q, "code", 1, 30, progress)
            _log(progress, f"  [发现] code search“{q}”返回 {len(items)} 条")
            if rate_limited:
                break
            for it in items:
                repo = it.get("repository") or {}
                fn = (repo.get("full_name") or "").lower()
                owner = (repo.get("owner") or {}).get("login", "")
                name = repo.get("name", "")
                path = it.get("path", "")
                if not fn or (owner.lower(), name.lower()) in known or not path:
                    continue
                key = (fn, path)
                if key in seen_repos:
                    continue
                seen_repos.add(key)
                code_hits.append((owner, name, path))
            _search_pace()
    # 免费公开搜索引擎补充 (2026-11-13): SearXNG 公开实例逐个试通为止,
    # 全部不通改用 DuckDuckGo HTML; 结果里解析出的仓库/直链文件再走同一套探测校验
    try:
        se_repos, se_files, se_engine, _se_urls = _search_engine_targets(
            SEARCH_ENGINE_QUERIES_TVBOX, progress, t_end)
    except Exception as e:  # noqa: BLE001
        _log(progress, f"  [发现] 搜索引擎补充失败, 跳过: {e}")
        se_repos, se_files, se_engine, _se_urls = set(), [], "none", []
    for owner, repo in se_repos:
        fn = f"{owner}/{repo}".lower()
        if fn in seen_repos or (owner.lower(), repo.lower()) in known:
            continue
        seen_repos.add(fn)
        repos.append((f"{owner}/{repo}", owner, repo, "main", ""))
    _log(progress, f"  [发现] 搜索引擎补充仓库 {len(se_repos)} 个 / 直链文件 {len(se_files)} 个 ({se_engine})")
    # 探测: 仓库按更新时间倒序, 最多探 150 个仓库 (8 并发); code 命中直接按精确路径探
    _log(progress, f"  [发现] TVBox 待探测仓库 {min(len(repos), 150)} 个 + code 命中 {len(code_hits)} 个")
    candidates = []
    jobs = [(owner, repo, branch, TVBOX_CONFIG_PATHS, updated)
            for _, owner, repo, branch, updated in repos[:150]]
    if jobs:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_probe_tvbox_candidate, o, r, b, p): (o, r, b, u)
                    for o, r, b, p, u in jobs}
            for fut in as_completed(futs):
                if time.time() > t_end or _deadline_hit():
                    ex.shutdown(wait=False, cancel_futures=True)
                    break
                owner, repo, branch, updated = futs[fut]
                try:
                    hit = fut.result()
                except Exception:
                    hit = None
                if hit:
                    path, used, n = hit
                    candidates.append({
                        "name": f"{owner}/{repo} {path}（发现）",
                        "owner": owner, "repo": repo, "branch": branch,
                        "path": path, "extra": [],
                        "discovered": True, "discovery_source": "github搜索",
                        "discovered_sites": n, "discovered_updated": updated,
                    })
    for owner, repo, path, _src in (
            [(o, r, p, "github code搜索") for o, r, p in code_hits]
            + [(o, r, p, "搜索引擎") for o, r, _b, p in se_files]):
        if len(candidates) >= DISCOVERY_MAX_TVBOX or time.time() > t_end or _deadline_hit():
            break
        hit = None
        for branch in ("main", "master"):
            hit = _probe_tvbox_candidate(owner, repo, branch, [path], progress)
            if hit:
                break
        if hit:
            _p, used, n = hit
            candidates.append({
                "name": f"{owner}/{repo} {path}（发现/{_src}）",
                "owner": owner, "repo": repo,
                "branch": "main", "path": path, "extra": [],
                "discovered": True, "discovery_source": _src,
                "discovered_sites": n, "discovered_updated": "",
            })
    candidates.sort(key=lambda c: c.get("discovered_updated") or "", reverse=True)
    candidates = candidates[:DISCOVERY_MAX_TVBOX]
    _log(progress, f"  [发现] TVBox 候选 {len(candidates)} 个 (上限 {DISCOVERY_MAX_TVBOX})")
    return candidates, rate_limited


def _yck_aggregate_ids(progress=None):
    """从 yck 聚合索引页解析聚合 id: /yuedu/shuyuans/content/id/<id>.html,
    按 id 倒序 (越新越大), 排除固定清单里已有的。返回 [id...]。"""
    known_ids = set()
    for src in LEGADO_SOURCES:
        for u in src.get("urls", []):
            m = re.search(r"shuyuans/json/id/(\d+)", u)
            if m:
                known_ids.add(int(m.group(1)))
    ids = set()
    for base in YCK_MIRRORS:
        text, _used, _ct = fetch_text([f"{base}/yuedu/shuyuans/index.html"], progress=None)
        if text:
            for m in re.findall(r"shuyuans/content/id/(\d+)\.html", text):
                ids.add(int(m))
            break  # 一个镜像成功即可, 不必三个都拉
    ids = sorted((i for i in ids if i not in known_ids), reverse=True)
    _log(progress, f"  [发现] yck 聚合索引解析出 {len(ids)} 个新聚合 id (已排除固定清单)")
    return ids


def _yck_aggregate_ok(agg_id, progress=None):
    """轻量探针: Range 只取聚合 JSON 的前 4KB, 确认是 JSON 数组开头。
    聚合文件常有几 MB, 发现阶段不整文件下载, 真正抓取时 fetch_books 再全量拉。"""
    for base in YCK_MIRRORS:
        url = f"{base}/yuedu/shuyuans/json/id/{agg_id}.json"
        try:
            r = requests.get(url, headers={**HEADERS, "Range": "bytes=0-4095"},
                             timeout=12)
            if r.status_code in (200, 206) and r.text.lstrip().startswith("["):
                return True
        except Exception:
            continue
    return False


def discover_books(progress=None):
    """书源发现: (1) yck 聚合索引全量扫 id, 取最新聚合 (最多 10 个);
    (2) GitHub 多关键词搜索 legado 相关仓库, 探测书源文件 (补到 30 个)。
    返回 (candidates, rate_limited)。"""
    candidates = []
    rate_limited = False
    t_end = time.time() + DISCOVERY_BUDGET_S
    # (1) yck 聚合
    try:
        agg_ids = _yck_aggregate_ids(progress)
    except Exception as e:  # noqa: BLE001
        _log(progress, f"  [发现] yck 聚合索引解析失败: {e}")
        agg_ids = []
    n_yck = 0
    for agg_id in agg_ids[:20]:  # 最新 20 个里取
        if n_yck >= 10 or len(candidates) >= DISCOVERY_MAX_BOOKS:
            break
        if time.time() > t_end or _deadline_hit():
            break
        if _yck_aggregate_ok(agg_id, progress):
            candidates.append({
                "name": f"yck 聚合 id={agg_id}（发现）",
                "urls": [f"{base}/yuedu/shuyuans/json/id/{agg_id}.json"
                         for base in YCK_MIRRORS],
                "discovered": True, "discovery_source": "yck聚合",
            })
            n_yck += 1
    _log(progress, f"  [发现] yck 聚合候选 {n_yck} 个")
    # (2) GitHub 搜索
    known = set()
    for src in LEGADO_SOURCES:
        for u in src.get("urls", []):
            m = re.search(r"github\.com/([^/]+)/([^/]+)", u)
            if m:
                known.add((m.group(1).lower(), m.group(2).lower()))
            m2 = re.search(r"cdn\.jsdelivr\.net/gh/([^/]+)/([^/@]+)", u)
            if m2:
                known.add((m2.group(1).lower(), m2.group(2).lower()))
    seen_repos = set()
    repos = []
    total_steps = len(BOOK_SEARCH_QUERIES) * 2
    q_done = 0
    for q in BOOK_SEARCH_QUERIES:
        if rate_limited or time.time() > t_end or _deadline_hit():
            break
        for page in (1, 2):
            if rate_limited or time.time() > t_end or _deadline_hit():
                break
            items, rate_limited = _github_search(q, "repositories", page, 30, progress)
            q_done += 1
            _emit(progress, 1, 0.5 + 0.5 * q_done / max(1, total_steps),
                  f"发现新源：GitHub 搜索书源“{q}”第 {page} 页，已收 {len(repos)} 个仓库",
                  q_done, total_steps)
            if rate_limited:
                break
            for it in items:
                fn = (it.get("full_name") or "").lower()
                if not fn or fn in seen_repos:
                    continue
                owner = (it.get("owner") or {}).get("login", "")
                name = it.get("name", "")
                if (owner.lower(), name.lower()) in known:
                    continue
                if it.get("fork"):
                    continue
                seen_repos.add(fn)
                repos.append((it["full_name"], owner, name,
                              it.get("default_branch") or "main",
                              it.get("updated_at") or ""))
            _search_pace()
    # 免费公开搜索引擎补充: 仓库进探测池; 结果里的 yck 聚合 id 也收进来
    try:
        se_repos, se_files, se_engine, se_urls = _search_engine_targets(
            SEARCH_ENGINE_QUERIES_BOOKS, progress, t_end)
    except Exception as e:  # noqa: BLE001
        _log(progress, f"  [发现] 搜索引擎补充失败, 跳过: {e}")
        se_repos, se_files, se_engine, se_urls = set(), [], "none", []
    for owner, repo in se_repos:
        fn = f"{owner}/{repo}".lower()
        if fn in seen_repos or (owner.lower(), repo.lower()) in known:
            continue
        seen_repos.add(fn)
        repos.append((f"{owner}/{repo}", owner, repo, "main", ""))
    for agg_id_s in sorted({int(m) for u in se_urls
                            for m in re.findall(r"shuyuans/json/id/(\d+)", u or "")},
                           reverse=True):
        if len([c for c in candidates if c.get("discovery_source") == "yck聚合"]) >= 10:
            break
        if time.time() > t_end or _deadline_hit():
            break
        if _yck_aggregate_ok(agg_id_s, progress):
            candidates.append({
                "name": f"yck 聚合 id={agg_id_s}（发现/搜索引擎）",
                "urls": [f"{base}/yuedu/shuyuans/json/id/{agg_id_s}.json"
                         for base in YCK_MIRRORS],
                "discovered": True, "discovery_source": "yck聚合",
            })
    _log(progress, f"  [发现] 搜索引擎补充仓库 {len(se_repos)} 个 / 直链文件 {len(se_files)} 个 ({se_engine})")
    _log(progress, f"  [发现] 书源待探测仓库 {min(len(repos), 100)} 个")
    if repos:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_probe_book_candidate, o, r, b, BOOK_FILE_PATHS): (o, r, b, u)
                    for _, o, r, b, u in repos[:100]}
            for fut in as_completed(futs):
                if time.time() > t_end or _deadline_hit():
                    ex.shutdown(wait=False, cancel_futures=True)
                    break
                owner, repo, branch, updated = futs[fut]
                try:
                    hit = fut.result()
                except Exception:
                    hit = None
                if hit:
                    path, used, n = hit
                    candidates.append({
                        "name": f"{owner}/{repo} {path}（发现）",
                        "urls": github_channels(owner, repo, branch, path),
                        "discovered": True, "discovery_source": "github搜索",
                        "discovered_items": n, "discovered_updated": updated,
                    })
                    if len(candidates) >= DISCOVERY_MAX_BOOKS:
                        break
    for owner, repo, branch, path in se_files:
        if len(candidates) >= DISCOVERY_MAX_BOOKS or time.time() > t_end or _deadline_hit():
            break
        hit = _probe_book_candidate(owner, repo, branch, [path], progress)
        if hit:
            _p, used, n = hit
            candidates.append({
                "name": f"{owner}/{repo} {path}（发现/搜索引擎）",
                "urls": github_channels(owner, repo, branch, path),
                "discovered": True, "discovery_source": "搜索引擎",
                "discovered_items": n, "discovered_updated": "",
            })
    candidates.sort(key=lambda c: c.get("discovered_updated") or "", reverse=True)
    candidates = candidates[:DISCOVERY_MAX_BOOKS]
    _log(progress, f"  [发现] 书源候选 {len(candidates)} 个 (上限 {DISCOVERY_MAX_BOOKS})")
    return candidates, rate_limited


def discover_upstreams(progress=None):
    """发现总入口, 跑在正式抓取之前。返回 report dict:
    {enabled, skipped_reason, tvbox:[...], books:[...], elapsed_s, github_rate_limited}。
    任何一步失败/限流/超时都只降级 (记日志), 不抛异常, 不让整轮抓取失败。"""
    t0 = time.time()
    report = {"enabled": True, "skipped_reason": None, "tvbox": [], "books": [],
              "elapsed_s": 0.0, "github_rate_limited": False}
    if not _discovery_enabled():
        report["enabled"] = False
        report["skipped_reason"] = "DISCOVERY_ENABLED=0"
        _log(progress, "[发现] 自动发现已关闭 (DISCOVERY_ENABLED=0), 只用固定上游清单")
        _emit(progress, 1, 1.0, "发现新源：已关闭，跳过", 0, 0)
        return report
    _emit(progress, 1, 0.0, "发现新源：开始联网搜索", 0, 1,
          log="[发现] 开始联网搜索新源 (GitHub 多关键词多页 + yck 聚合索引)")
    try:
        tv, rl1 = discover_tvbox(progress)
    except Exception as e:  # noqa: BLE001
        _log(progress, f"[发现] TVBox 发现异常, 跳过: {e}")
        tv, rl1 = [], False
    try:
        bk, rl2 = discover_books(progress)
    except Exception as e:  # noqa: BLE001
        _log(progress, f"[发现] 书源发现异常, 跳过: {e}")
        bk, rl2 = [], False
    report["tvbox"] = tv
    report["books"] = bk
    report["github_rate_limited"] = bool(rl1 or rl2)
    report["elapsed_s"] = round(time.time() - t0, 1)
    _emit(progress, 1, 1.0,
          f"发现新源：TVBox {len(tv)} 个 / 书源 {len(bk)} 个候选",
          1, 1,
          log=f"[发现] 完成: TVBox 候选 {len(tv)} 个 / 书源候选 {len(bk)} 个, "
              f"用时 {report['elapsed_s']}s"
              + (" (GitHub 搜索曾限流, 已降级)" if report["github_rate_limited"] else ""))
    return report


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
    # --- 2026-11-17 发现调研并入: 多仓订阅 (Lightconer 每日两次更新, 最新鲜的聚合)
    # 与 LunaTV-config (88 个 MacCMS api.php/provide/vod 端口, 最全的 CMS 清单) ---
    {"name": "Lightconer/tvbox-ysc-config 多仓订阅.json",
     "owner": "Lightconer", "repo": "tvbox-ysc-config", "branch": "main",
     "path": "output/多仓订阅.json", "extra": []},
    {"name": "hafrey1/LunaTV-config LunaTV-config.json (88 个 MacCMS 端口)",
     "owner": "hafrey1", "repo": "LunaTV-config", "branch": "main",
     "path": "LunaTV-config.json", "extra": []},
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
        r = requests.head(url, headers=HEADERS, timeout=timeout, allow_redirects=True,
                          proxies=_TEST_PROXIES)
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
        with requests.get(url, headers=HEADERS, timeout=timeout, stream=True,
                          proxies=_TEST_PROXIES) as r:
            if 200 <= r.status_code < 400:
                try:
                    next(r.iter_content(chunk_size=1024), None)
                except Exception:
                    pass
                return True
            return False
    except Exception:
        return False


# ---------------------------------------------------------------- TVBox 受限内容过滤
# TVBox 受限内容关键词表 (2026-11-13 双套输出重构, 替代 2026-11-11 版):
# 只看三类直接证据, 不再对整条记录做子串匹配。
# 误杀根因 (已实证): 旧逻辑把 key+name+api+ext 拼成一个 haystack 做子串/词边界匹配,
# XBPQ 配置里 "分类url": "https://www.xsmp3.com/{cateId}/{catePg}.html;;av" 的 ";;av"
# 只是 XBPQ 的配置语法后缀 (同类后缀还有 ;;vr1au0、;;z), 被旧 token 正则当成 "av"
# 命中, 导致 相声/评书 被删; 九月影视的 ext 配置含受限内容字样同样被整串匹配命中。
# 而这两个站的 分类 全是 郭德纲/德云社/马三立、袁阔成/单田芳/田连元 (曲艺内容)。
# 新逻辑:
#   (1) 站名/key: 中文强词子串 + 英文短词 (av/sex/xxx) 词边界 + 严格词 (仅 A 套);
#   (2) 内容分类: 只看 ext 字典的 "分类" 字段和 type-1 的 categories 列表
#       (站点自声明的内容分类, 含受限内容词即判 B 套)——ext 的其它字段
#       (主页url/分类url/搜索url/请求头/正则/过滤规则等) 一律不匹配;
#   (3) URL: 只看域名标签/路径段完整等于受限内容词,
#       不拿整个 URL 做子串包含 (kxgav.com 的白嫖站、;;av 后缀都不受影响);
#   (4) ext 是相对路径时只看文件名 (如 ./js/xxx.js), 是短标签 (<=120 字符) 时按
#       站名规则判, 是长串/JSON 文本时跳过。
# 作用范围: 全部 TVBox 站 (type-0/1/3/4, 去重后、测速前就过滤, 严格模式) +
# type-3 挑选 (二次兜底) + 最终写文件前复核, 并过滤 lives 里的受限内容频道名。
# 命中者不丢弃, 进 B 套 (tvbox_b.json), 测速通过才入选。书源侧已有
# ADULT_KEYWORDS 过滤, 不受本次改动影响。
TVBOX_ADULT_STRONG_KEYWORDS = [
    "涩", "色情", "成人", "黄片", "黄文", "四级", "三级片", "毛片", "18禁", "裸聊",
    "fullerotic", "erotic", "porn", "cam4", "91动漫", "jav",
    # 已核实的受限内容品牌/俗称 (2026-11-13 复核当前 A 套时发现有漏网):
    "色逼", "色花堂", "新香蕉", "麻豆", "女优",
    # 2026-11-13 并入他方项目 TVBOX_ADULT_RE (对方有、我方没有的词):
    # 偷拍/裙底/黑料/大奶/杏吧/色猫/奶香/探花/楼凤/野模/禁漫/下海/爆乳/番号/
    # 里番/无码/有码/加勒比/一本道/东京热/福利姬/约啪/援交/色欲/老色/avbt/
    # souav/heiliao/danaizi/hlzy/xiuren/meitui。注意对方 "av" 用字母边界
    # (?<![a-z])av(?![a-z]), 我方已有 TVBOX_ADULT_TOKEN_RE 同等处理, 不另加裸 av。
    "偷拍", "裙底", "黑料", "大奶", "杏吧", "色猫", "奶香", "探花", "楼凤",
    "野模", "禁漫", "下海", "爆乳", "番号", "里番", "无码", "有码", "加勒比",
    "一本道", "东京热", "福利姬", "约啪", "援交", "色欲", "老色",
    "avbt", "souav", "heiliao", "danaizi", "hlzy", "xiuren", "meitui",
]
# 严格词: 只用于 A 套 (strict=True), 宁可错杀不可漏进 A 套;
# 命中严格词的站不丢弃, 进 B 套。
TVBOX_ADULT_STRICT_KEYWORDS = [
    "伦理", "情色", "情欲", "午夜", "激情", "限制级", "三级", "福利",
]
TVBOX_ADULT_TOKEN_RE = re.compile(r"(?<![a-z0-9])(av|sex|xxx)(?![a-z0-9])", re.IGNORECASE)
# URL 强证据: 域名标签或路径段完整等于以下词 (大小写不敏感)
TVBOX_ADULT_URL_TOKENS = {
    "av", "porn", "erotic", "sex", "xxx", "jav", "adult", "cam4",
    "pornhub", "xvideos", "xnxx", "redtube", "xhamster",
}
# 2026-11-11 剔除的 3 个受限内容站 (key, 审计用):
TVBOX_ADULT_REMOVED_KNOWN = ["3d涩动漫.py", "91动漫.py", "Cam4直播.py"]


def _url_adult_evidence(url):
    """URL 里的受限内容强证据: 域名标签/路径段完整等于受限内容词。返回命中的词, 无则 None。
    不做子串包含: kxgav.com、;;av 后缀、avzyapi.com 这类都不命中。"""
    if not isinstance(url, str) or not url.strip():
        return None
    u = _strip_md5_suffix(url.strip())
    if not u.lower().startswith(("http://", "https://")):
        return None
    try:
        p = urlparse(u)
    except Exception:
        return None
    segs = [s for s in p.netloc.split(".") if s]
    segs += [s for s in p.path.split("/") if s]
    for seg in segs:
        for tok in re.split(r"[^A-Za-z0-9]+", seg):
            if tok and tok.lower() in TVBOX_ADULT_URL_TOKENS:
                return tok.lower()
    return None


def _text_adult_reason(text, strict):
    """站名/key/分类文本的受限内容判定。返回 (命中词, 是否严格词命中) 或 (None, False)。"""
    t = (text or "").lower()
    for kw in TVBOX_ADULT_STRONG_KEYWORDS:
        if kw in t:
            return kw, False
    m = TVBOX_ADULT_TOKEN_RE.search(t)
    if m:
        return m.group(1).lower(), False
    if strict:
        for kw in TVBOX_ADULT_STRICT_KEYWORDS:
            if kw in t:
                return kw, True
    return None, False


def _is_adult_tvbox(site, strict=False):
    """受限内容判定。返回 (是否受限内容, 原因)。
    strict=True 时严格词也判受限内容
    (用于 A 套); strict=False 时只判强证据。"""
    key = str(site.get("key") or "")
    name = str(site.get("name") or "")
    # (1) 站名/key
    kw, strict_hit = _text_adult_reason(f"{key} {name}", strict)
    if kw:
        return True, ("strict:" if strict_hit else "strong:") + kw
    # (2) 内容分类: ext 字典的 "分类" 字段 + type-1 的 categories 列表
    ext = site.get("ext")
    taxonomy = ""
    if isinstance(ext, dict):
        taxonomy = str(ext.get("分类") or "")
    cats = site.get("categories")
    if isinstance(cats, list):
        taxonomy += " " + " ".join(str(c) for c in cats)
    if taxonomy.strip():
        kw, strict_hit = _text_adult_reason(taxonomy, strict)
        if kw:
            return True, ("category-strict:" if strict_hit else "category:") + kw
    # (3) URL 强证据: api 是 http URL 时看域名/路径段; ext 是 http URL 时同理
    for field in (site.get("api"), ext if isinstance(ext, str) else None):
        hit = _url_adult_evidence(field)
        if hit:
            return True, "url:" + hit
    # (4) ext 是相对路径/短标签: 只看文件名或短文本, 按站名规则判; 长串跳过
    if isinstance(ext, str) and not ext.strip().lower().startswith(("http://", "https://")):
        short = ext.strip()
        cand = os.path.basename(short) if len(short) > 120 else short
        kw, strict_hit = _text_adult_reason(cand, strict)
        if kw:
            return True, ("ext-strict:" if strict_hit else "ext:") + kw
    return False, ""


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
    # 受限内容站不由这里判断: 统一走 _is_adult_tvbox, 在去重后、type-3 挑选、
    # 最终复核三处显式过滤并记录 (见 fetch_tvbox), 避免工具类和受限内容混在一条记录里。
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
        # 配置所在目录（供相对路径解析用，输出时 _ 开头键会被剥掉，不进 JSON）。
        # 2026-11-17 jar 本地化需要它：./jar/*.jar、./deps/... 这类相对 jar
        # 要按配置所在目录解析成绝对 URL 再下载。
        ns["_base_dir"] = base_dir
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


def _json_head_ok(text):
    """正文首个非空白字符是不是 {/[ (JSON 对象/数组开头)。
    先去 BOM/行首注释/尾逗号再判断, 与 _fetch_one_tvbox_json 的解析前处理一致。"""
    if not isinstance(text, str):
        return False
    head = strip_json_noise(strip_leading_comment_lines(text)).lstrip()
    return bool(head) and head[0] in "{["


def _fetch_one_tvbox_json(urls, progress=None, label=""):
    text, used, content_type = fetch_text(urls, progress=progress, label=label)
    if text is None:
        return None, None
    # 2026-11-13 并入他方 stripJsonNoise: 先去 BOM/注释/尾逗号再解析
    cleaned = strip_json_noise(strip_leading_comment_lines(text))
    # 2026-11-16: 响应根本不是 JSON (agit.ai 等一堆
    # "Expecting value: line 1 column 1", 多半是 HTML 错误页/删库/需登录):
    # 先看 Content-Type 与正文首个非空白字符, 不是 {/[ 开头就直接跳过,
    # 日志写清原因, 不再走解析报错。
    head = cleaned.lstrip()
    if not head or head[0] not in "{[":
        _fail_count("非JSON")
        preview = head[:80].replace("\n", " ") if head else ""
        _log(progress, f"  非 JSON（Content-Type: {content_type or '未知'}，"
                       f"可能已删库/需登录）{used} 首内容: {preview!r}")
        return None, None
    try:
        # strict=False: 允许字符串内的非法控制字符 (如 dm.txt line 13、
        # cccimg down.php line 780 的控制字符, 之前整份解析失败)。
        obj = json.loads(cleaned, strict=False)
    except Exception:  # noqa: BLE001
        # 有些文件 (如 jinenge) 在第一个 JSON 对象后还附了另一段配置/垃圾,
        # json.loads 会报 Extra data; 用 raw_decode 只取第一个对象
        try:
            dec = json.JSONDecoder(strict=False)
            obj, _end = dec.raw_decode(cleaned.lstrip())
            _log(progress, f"  注意 {used}: 文件含尾部多余内容, 只取第一个 JSON 对象")
        except Exception as e2:  # noqa: BLE001
            _fail_count("JSON解析失败")
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
        with requests.get(url, headers=HEADERS, timeout=5, stream=True,
                          proxies=_TEST_PROXIES) as r:
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
            # sample: 首条 vod_name (对方 isAdultEntry 的受限内容判定用 name+sample+base;
            # 我方在最终入选后用 sample 再做一次受限内容复核, 见 fetch_tvbox 末尾)
            sample = ""
            _m = re.search(r'"vod_name"\s*:\s*"([^"]{1,80})"', text_head)
            if _m:
                sample = _m.group(1)
            # 首页 fallback: 200 且有内容也算通, 但 vod_ok 为 False 时不算"能返回 vod 列表"
            if "/provide/vod" not in api and "api.php" not in api:
                vod_ok = nbytes > 200
            kbps = round((nbytes / 1024) / max(total_ms / 1000, 0.01), 1) if total_ms else 0
            ok = (ttfb_ms < TTFB_LIMIT_MS and total_ms < TOTAL_LIMIT_MS and vod_ok)
            return {"key": site["key"], "api": api, "api_host": host, "ok": ok,
                    "ttfb_ms": ttfb_ms, "total_ms": total_ms, "kbps": kbps,
                    "sample_bytes": nbytes, "sample": sample,
                    "channel": "direct", "domestic_cdn": domestic_bonus(host),
                    "vod_ok": vod_ok, "error": None if ok else "未达门槛或无 vod 列表",
                    "checked_at": now_iso()}
    except Exception as e:  # noqa: BLE001
        return {"key": site["key"], "api": api, "api_host": host, "ok": False,
                "ttfb_ms": None, "total_ms": int((time.monotonic() - t0) * 1000),
                "kbps": 0, "channel": "direct", "domestic_cdn": domestic_bonus(host),
                "vod_ok": False, "error": f"{type(e).__name__}: {e}",
                "checked_at": now_iso()}


def _speed_fail_record(key, e):
    """测速子任务抛异常时的兜底记录: 该站记不通过, 不杀整轮。
    (2026-11-16 循环根因修复: 旧版 fut.result() 会把子任务异常抛回主线程,
    杀死整轮 TVBox 抓取, 进而整轮 fetch_all 没写 status.json。)"""
    return {"key": key, "api": "", "api_host": "", "ok": False,
            "ttfb_ms": None, "total_ms": None, "kbps": 0,
            "channel": "direct", "domestic_cdn": False, "vod_ok": False,
            "error": f"internal_{type(e).__name__}: {e}", "checked_at": now_iso()}


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


def _speed_test_pool(sites, by_key, progress, label, stage_no=3):
    """ B 套测速 (2026-11-13 双套输出): 与 A 套同样的三轮实测
    (第 1 轮全测, 第 2/3 轮复测全部通过者, 每轮取最差成绩; 阈值与
    _speed_test_one 相同: TTFB<1500ms / 总耗时<3000ms / 样本<=200KB)。
    B 套池很小 (通常 <30 站), 复测不设 110/60 上限, 全部复测。
    返回 (final_records, round1_passed_count); final_records 按速度排序。"""
    if not sites:
        return [], 0
    _emit(progress, stage_no, 0.0, f"{label}测速第 1 轮开始，共 {len(sites)} 站",
          0, len(sites), log=f"[TVBox] {label}第一轮实测开始，共 {len(sites)} 站")
    round1 = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(_speed_test_one, s): s for s in sites}
        done_n = 0
        pass_n = 0
        for fut in as_completed(futs):
            try:
                rec = fut.result()
            except Exception as e:  # noqa: BLE001
                rec = _speed_fail_record(futs[fut]["key"], e)
            round1[rec["key"]] = rec
            done_n += 1
            if rec.get("ok"):
                pass_n += 1
            if done_n % 10 == 0 or done_n == len(sites):
                _emit(progress, stage_no, 0.7 * done_n / max(1, len(sites)),
                      f"{label}测速第 1 轮 {done_n}/{len(sites)}，通过 {pass_n} 个",
                      done_n, len(sites))
    pass1 = [r for r in round1.values() if r.get("ok")]
    _log(progress, f"[TVBox] {label}第一轮通过 {len(pass1)}/{len(sites)}")
    cur = {r["key"]: r for r in pass1}
    for rnd in (2, 3):
        if not cur or _deadline_hit():
            break
        keys = sorted(cur.keys(), key=lambda k: _speed_score(cur[k]))
        _emit(progress, stage_no, 0.0, f"{label}测速第 {rnd} 轮复测，共 {len(keys)} 站",
              0, len(keys))
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(_speed_test_one, by_key[k]): k for k in keys}
            done_n = 0
            for fut in as_completed(futs):
                try:
                    rec = fut.result()
                except Exception as e:  # noqa: BLE001
                    rec = _speed_fail_record(futs[fut], e)
                k = futs[fut]
                old = cur[k]
                m = dict(old)
                m["ttfb_ms"] = max(old.get("ttfb_ms") or 9999, rec.get("ttfb_ms") or 9999)
                m["total_ms"] = max(old.get("total_ms") or 9999, rec.get("total_ms") or 9999)
                m["kbps"] = min(old.get("kbps") or 0, rec.get("kbps") or 0)
                m["checked_at"] = rec.get("checked_at")
                m["ok"] = bool(old.get("ok") and rec.get("ok"))
                m["rounds"] = old.get("rounds", 1) + 1
                m["channel"] = rec.get("channel", old.get("channel"))
                cur[k] = m
                done_n += 1
        cur = {k: r for k, r in cur.items() if r.get("ok")}
        _log(progress, f"[TVBox] {label}第 {rnd} 轮后通过 {len(cur)}/{len(pass1)}")
    final = sorted(cur.values(), key=_speed_score)
    return final, len(pass1)


# type-3 偏门新增配额 (合计 34, 加原有 2 个偏门共 36, 占 120 的 30%)
TYPE3_NICHE_QUOTA = {"4K": 5, "动漫": 6, "地方台/直播": 5, "体育": 4,
                     "少儿": 3, "海外剧": 3, "日韩": 2, "港台": 2,
                     "戏曲": 2, "经典老片": 1, "纪录片": 1}


# ---------------------------------------------------------------- 依赖包下载
# 用户反馈 (2026-11-13): 现在输出里一个需要加载 java 包的源都没有, jar 依赖
# 从来没被下载保存过。本模块把 spider jar + 入选 type-3 站的 api/ext 文件
# 下载到 data/tvbox/assets/, 随最终成果一起交付 (打包 ZIP 里含 assets/)。
# 2026-11-17 用户纠正 (之前理解错了): assets 不是"随包附带的备份", 它的用途
# 就是给大陆访问慢的远程 jar 准备本地副本——各站的 jar 字段必须改写成
# ./assets/<文件>（见 _localize_site_jars），否则 assets 里的文件没人用、
# 等于白下。api/ext 的远程文件仍只作备份随包附带，不改写（那是另一回事，
# 且 TVBox 对 api/ext 相对路径的支持未验证，不擅自改）。
# 注意: tvbox.json 与 assets/ 在同一目录层级（zip 里 tvbox.json 在根、assets/
# 是子目录），./assets/... 相对路径才能解析；不要改 jar 字段以外的路径形式。
ASSET_MAX_BYTES = 64 * 1024 * 1024  # 单个依赖文件上限 64MB


def _sanitize_asset_name(name):
    name = re.sub(r"[^A-Za-z0-9._\-]+", "_", (name or "").strip())
    name = name.strip("._") or "file"
    return name[:80]


def _asset_filename(clean_url, prefix, default_ext=None):
    """按下载规则算出的本地文件名: <prefix>_<clean URL sha256 前 8 位>_<净化后的 basename>。
    无扩展名时补 default_ext（未指定时 jar 类补 .jar——内容已验是 jar，.bin 可能让
    TVBox 认不出它是 jar；其它依赖文件补 .bin）。与 _download_asset 共用同一规则，
    jar 本地化靠它把 URL 映射回已下载的文件。"""
    if default_ext is None:
        default_ext = ".jar" if prefix == "jar" else ".bin"
    parsed = urlparse(clean_url)
    base = _sanitize_asset_name(os.path.basename(parsed.path))
    if "." not in base:
        base += default_ext
    tag = hashlib.sha256(clean_url.encode()).hexdigest()[:8]
    return f"{prefix}_{tag}_{base}"


def _download_asset(url, dest_dir, prefix, progress=None):
    """下载一个依赖文件, 返回 {file, url, size_bytes, sha256} 或 None。
    失败只记日志跳过, 不中断。URL 里的 ;md5; 后缀先剥掉; 文件名加 URL 短 hash 防重名;
    已下载过的文件直接复用 (断点续跑时不重复下载)。"""
    if not isinstance(url, str) or not url.strip():
        return None
    clean = _strip_md5_suffix(url.strip())
    if not clean.startswith(("http://", "https://")):
        return None
    fname = _asset_filename(clean, prefix)
    dest = os.path.join(dest_dir, fname)
    if os.path.exists(dest):
        try:
            h = hashlib.sha256()
            size = 0
            with open(dest, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    size += len(chunk)
                    h.update(chunk)
            return {"file": fname, "url": clean, "size_bytes": size,
                    "sha256": h.hexdigest()}
        except Exception:
            pass
    try:
        with requests.get(clean, headers=HEADERS, timeout=30, stream=True) as r:
            if r.status_code != 200:
                _log(progress, f"  [依赖] 下载失败 {clean} HTTP {r.status_code}")
                return None
            h = hashlib.sha256()
            size = 0
            tmp = dest + ".tmp"
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > ASSET_MAX_BYTES:
                        _log(progress, f"  [依赖] 跳过超大文件 {clean} (>64MB)")
                        f.close()
                        os.remove(tmp)
                        return None
                    h.update(chunk)
                    f.write(chunk)
            os.replace(tmp, dest)
        return {"file": fname, "url": clean, "size_bytes": size,
                "sha256": h.hexdigest()}
    except Exception as e:  # noqa: BLE001
        _log(progress, f"  [依赖] 下载失败 {clean}: {e}")
        return None


def _download_tvbox_assets(spider_url, type3_final, type3_by_key, assets_dir,
                           progress=None):
    """下载 spider jar + 入选 type-3 站的 api/ext 依赖文件到 assets_dir。
    type3_final 为最终入选的 type-3 记录 (picked 里 rounds==0 的项, 含 key/api/
    spider/spider_working); ext 从原始站字典取。返回 jar_files 清单
    [{file, url, source, size_bytes, sha256}] (按文件名排序)。"""
    os.makedirs(assets_dir, exist_ok=True)
    jobs = []  # (url, source_label, prefix)
    seen = set()

    def _add(url, source, prefix):
        if not isinstance(url, str):
            return
        clean = _strip_md5_suffix(url.strip())
        if not clean.startswith(("http://", "https://")) or clean in seen:
            return
        seen.add(clean)
        jobs.append((clean, source, prefix))

    if spider_url:
        _add(spider_url, "spider", "spider")
    for rec in type3_final or []:
        key = rec.get("key", "")
        _add(rec.get("spider_working") or rec.get("spider"),
             f"type3:{key} spider", "type3")
        _add(rec.get("api"), f"type3:{key} api", "type3")
        site = (type3_by_key or {}).get(key) or {}
        _add(site.get("ext"), f"type3:{key} ext", "type3")
    jar_files = []
    _log(progress, f"[依赖] 开始下载依赖包 {len(jobs)} 个 (spider jar + type-3 api/ext)")
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_download_asset, url, assets_dir, prefix, progress): (url, source)
                for url, source, prefix in jobs}
        done = 0
        for fut in as_completed(futs):
            url, source = futs[fut]
            done += 1
            try:
                rec = fut.result()
            except Exception:
                rec = None
            if rec:
                jar_files.append({"file": rec["file"], "url": rec["url"],
                                  "source": source, "size_bytes": rec["size_bytes"],
                                  "sha256": rec["sha256"]})
            _emit(progress, 5, 0.7 + 0.3 * done / max(1, len(jobs)),
                  f"下载依赖包 {done}/{len(jobs)}，已存 {len(jar_files)} 个",
                  done, len(jobs))
    jar_files.sort(key=lambda r: r["file"])
    total_kb = sum(r["size_bytes"] for r in jar_files) / 1024
    _log(progress, f"[依赖] 完成: {len(jar_files)}/{len(jobs)} 个文件, "
                   f"共 {total_kb:.0f} KB, 存于 tvbox/assets/")
    _atomic_write_json(os.path.join(os.path.dirname(assets_dir),
                                   "checkpoint_assets.json"),
                       {"jar_files": jar_files, "total_jobs": len(jobs)})
    return jar_files


def _parse_jar_md5(value):
    """从 jar 值解析 (clean_url, 声明的 md5 或 None)。
    支持 'url;md5;HEX' 与 'url;HEX'（32 位 hex，如 ...#.png;3130dcf...）两种写法；
    无 ';' 则 md5 为 None。截断规则与 _strip_md5_suffix 一致（从第一个 ';' 截断）。"""
    if not isinstance(value, str):
        return value, None
    clean = _strip_md5_suffix(value)
    md5 = None
    v = value.strip()
    if ";" in v:
        tail = v.split(";", 1)[1].strip()
        if tail.lower().startswith("md5;"):
            tail = tail[4:].strip()
        if re.fullmatch(r"[0-9a-fA-F]{32}", tail or ""):
            md5 = tail
    return clean, md5


def _md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_jar_bytes(path):
    """文件头是不是 jar（zip）: PK\\x03\\x04 / PK\\x05\\x06 / PK\\x07\\x08。
    改成 .png/.jpg 的伪装 jar 内容仍是 jar，照此判断；HTML 报错页、空文件都判否。"""
    try:
        with open(path, "rb") as f:
            head = f.read(4)
        return head[:2] == b"PK" and head[2:4] in (b"\x03\x04", b"\x05\x06", b"\x07\x08")
    except Exception:
        return False


def _candidate_bases(site):
    """相对 jar 的解析基准（按优先级）：站点 _base_dir（配置所在目录）；
    api URL 所在目录；ext URL（字符串时）所在目录。
    启发式：解析错了也没关系，下载 404 或文件头不是 jar 都会被拦下、不改写。"""
    bases = []

    def _add(b):
        if isinstance(b, str) and b.startswith(("http://", "https://")) and b not in bases:
            bases.append(b)

    _add(site.get("_base_dir"))
    for u in (site.get("api"), site.get("ext")):
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            _add(u.rsplit("/", 1)[0] + "/")
    return bases


def _localize_site_jars(sites, assets_dir, jar_files, progress=None,
                        download_fn=None):
    """把各站的 jar 改写成本地 ./assets/<文件>（2026-11-17 用户纠正）。
    就地改写 sites 的 jar 字段，返回统计 dict（记进 meta.json 的 jar_localize）：
      rewritten：成功改成本地路径的站数
      already_local：本来就是 ./assets/ 的站数
      kept_remote：[{key, jar, reason}] 改写失败、保留原值的站
      md5_mismatched：[{key, file, declared, actual}] 声明 md5 与下载文件不符、
        已去掉 ;md5; 后缀（上游 jar 已更新，本地文件仍可用）
    规则：
      - 远程 http(s) jar：下载到 assets/（已在 jar_files 清单里的直接复用，
        不重复下载），改写为 ./assets/<file>；下载失败保留原值。
      - 相对路径 jar（./jar/*.jar、./deps/...）：按 _candidate_bases 依次解析
        成绝对 URL 再下载改写；都解析/下载失败的保留原样并记录。
      - 已是 ./assets/ 的不动。
      - 下载/复用的文件必须通过 _is_jar_bytes（jar 是 zip，头是 PK），否则不改写；
        新下载的非 jar 文件直接删掉，不留坏文件。
      - 本地路径不带 ;md5; 后缀（用户给的例子就是 plain 路径；TVBox 对本地文件
        的 ;md5; 怎么处理未验证，2026-11-17 实测带后缀的版本导入报解析失败）。
        md5 只在内部校验下载文件，失配的记在 md5_mismatched（说明上游已更新）。
      - 无扩展名的 jar 下载保存为 .jar（不补 .bin，TVBox 可能认不出 .bin 是 jar）。
    download_fn 供测试注入，默认 _download_asset。"""
    if download_fn is None:
        download_fn = _download_asset
    os.makedirs(assets_dir, exist_ok=True)
    url_to_file = {j["url"]: j["file"] for j in (jar_files or [])
                   if j.get("url")}
    stats = {"rewritten": 0, "already_local": 0, "kept_remote": [],
             "md5_mismatched": []}

    def _keep(site, jar, reason):
        stats["kept_remote"].append({"key": site.get("key", ""),
                                     "jar": jar, "reason": reason})

    def _fetch_jar(clean, site):
        """取一个绝对 jar URL 对应的本地文件（复用清单里的，或新下载）。
        返回 (fname, ok, reason)。清单里的文件已不存在或不是 jar 时当作没下过，
        重新下载（不直接判失败）。"""
        fname = url_to_file.get(clean)
        if fname is not None:
            local_path = os.path.join(assets_dir, fname)
            if os.path.exists(local_path) and _is_jar_bytes(local_path):
                return fname, True, ""
            url_to_file.pop(clean, None)
        try:
            rec = download_fn(clean, assets_dir, "jar", progress)
        except Exception as e:  # noqa: BLE001
            rec = None
            _log(progress, f"  [jar 本地化] 下载异常 {clean}: {e}")
        if not rec:
            return None, False, "jar 下载失败"
        fname = rec["file"]
        local_path = os.path.join(assets_dir, fname)
        if not os.path.exists(local_path) or not _is_jar_bytes(local_path):
            try:
                os.remove(local_path)
            except Exception:
                pass
            return None, False, "下载的文件不是 jar（无 PK 文件头）"
        url_to_file[clean] = fname
        jar_files.append({"file": rec["file"], "url": rec["url"],
                          "source": "jar:%s" % site.get("key", ""),
                          "size_bytes": rec["size_bytes"],
                          "sha256": rec["sha256"]})
        return fname, True, ""

    for site in sites or []:
        jar = site.get("jar")
        if not isinstance(jar, str) or not jar.strip():
            continue
        jar = jar.strip()
        if jar.startswith("./assets/"):
            stats["already_local"] += 1
            continue
        clean, declared_md5 = _parse_jar_md5(jar)
        if clean.startswith(("http://", "https://")):
            candidates = [clean]
        elif clean.startswith(("./", "../", "/")) or (
                "/" in clean and not clean.startswith("/")):
            candidates = []
            for base in _candidate_bases(site):
                resolved = resolve_relative(clean, base)
                if resolved and resolved.startswith(("http://", "https://")) \
                        and resolved not in candidates:
                    candidates.append(resolved)
            if not candidates:
                _keep(site, jar, "相对路径 jar，无配置目录可解析")
                continue
        else:
            _keep(site, jar, "jar 值格式未知，未处理")
            continue
        fname = None
        last_reason = "jar 下载失败"
        for cand in candidates:
            fname, ok, reason = _fetch_jar(cand, site)
            if ok:
                break
            last_reason = reason
            fname = None
        if not fname:
            _keep(site, jar, last_reason)
            continue
        # 本地路径不带 ;md5; 后缀（用户给的例子就是 plain 路径；TVBox 对本地文件
        # 的 ;md5; 怎么处理未验证，带上反而可能导致解析失败）。md5 只在内部校验
        # 下载文件：失配的记下来（说明上游 jar 已更新），不影响改写。
        local_path = os.path.join(assets_dir, fname)
        if declared_md5:
            try:
                actual = _md5_file(local_path)
            except Exception:
                actual = None
            if not actual or actual.lower() != declared_md5.lower():
                stats["md5_mismatched"].append(
                    {"key": site.get("key", ""), "file": fname,
                     "declared": declared_md5, "actual": actual})
        site["jar"] = "./assets/%s" % fname
        stats["rewritten"] += 1
    _log(progress, f"[jar 本地化] 改写 {stats['rewritten']} 站，"
                   f"已本地 {stats['already_local']} 站，"
                   f"保留原值 {len(stats['kept_remote'])} 站")
    return stats


def _sub_config_urls(obj):
    """多仓索引的子配置 URL (对方 harvestConfig 的 subConfigUrls):
    json.urls || json.configs 里 .json/.txt 结尾的 URL (字典取 url 字段,
    字符串直接取); 无则全文正则提 https?://... .(json|txt)。返回去重后的列表。"""
    urls = []
    if isinstance(obj, dict):
        for key in ("urls", "configs"):
            v = obj.get(key)
            if isinstance(v, list):
                for entry in v:
                    u = entry.get("url") if isinstance(entry, dict) else entry
                    if isinstance(u, str) and re.search(r"\.(json|txt)(\?|$)", u, re.IGNORECASE):
                        urls.append(u.strip())
    if not urls and isinstance(obj, dict):
        text = json.dumps(obj, ensure_ascii=False)
        urls = re.findall(r"https?://[^\s\"'<>]+\.(?:json|txt)(?:\?[^\s\"'<>]*)?",
                          text, re.IGNORECASE)
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _harvest_tvbox_obj(obj, base, progress=None, depth=0):
    """解析一个 TVBox 配置对象, 多仓索引时递归展开 (最多 2 层, 对方口径)。
    返回 (sites, parses, lives, spiders, dohs, rules, stats)。
    stats: {"configs": 拉过的配置数, "sub_configs": 子配置 URL 数}。"""
    sites, parses, lives = [], [], []
    spiders, dohs, rules = [], [], []
    stats = {"configs": 1, "sub_configs": 0}
    if isinstance(obj, dict) and isinstance(obj.get("urls"), list) and not obj.get("sites"):
        if depth >= 2:
            return sites, parses, lives, spiders, dohs, rules, stats
        sub_urls = _sub_config_urls(obj)
        stats["sub_configs"] = len(sub_urls)
        for u in sub_urls[:10]:
            sub_obj, sub_used = _fetch_one_tvbox_json([u], progress=progress)
            if sub_obj is None:
                continue
            ss, pp, ll, sp2, dd, rr, st2 = _harvest_tvbox_obj(
                sub_obj, base_dir_of(sub_used) if sub_used else base, progress, depth + 1)
            sites.extend(ss); parses.extend(pp); lives.extend(ll)
            dohs.extend(dd); rules.extend(rr)
            for sp in sp2:
                if sp not in spiders:
                    spiders.append(sp)
            stats["configs"] += st2["configs"]
            stats["sub_configs"] += st2["sub_configs"]
        return sites, parses, lives, spiders, dohs, rules, stats
    ss, pp, ll, sp, dd, rr = _parse_tvbox_obj(obj, base, progress)
    sites.extend(ss); parses.extend(pp); lives.extend(ll)
    dohs.extend(dd); rules.extend(rr)
    if sp and sp not in spiders:
        spiders.append(sp)
    return sites, parses, lives, spiders, dohs, rules, stats


def _source_key(src):
    """上游去重键: direct_url 按规范化 URL (小写、去尾部 /);
    GitHub 形按 owner/repo/branch/path。"""
    if src.get("direct_url"):
        return ("direct", src["direct_url"].strip().lower().rstrip("/"))
    return ("gh", str(src.get("owner", "")).lower(), str(src.get("repo", "")).lower(),
            str(src.get("branch", "")), str(src.get("path", "")))


def _dedup_sources(sources, progress=None):
    """上游列表去重 (2026-11-16: 同一 seed URL 一轮只抓一次)。
    同一 URL 被抓两次会重复烧时间、重复打失败日志。返回 (去重后列表, 丢掉数)。"""
    seen, out, dropped = set(), [], 0
    for src in sources:
        k = _source_key(src)
        if k in seen:
            dropped += 1
            _log(progress, f"[TVBox] 跳过重复上游 {src.get('name', k)}（与已列入的上游相同）")
            continue
        seen.add(k)
        out.append(src)
    return out, dropped


def fetch_tvbox(data_dir, progress=None, extra_sources=None, discovery=None):
    # extra_sources 为发现阶段找到的新候选上游 (与固定清单走同一套流程);
    # discovery 为 discover_upstreams 的 report, 记进 meta.json
    sources = list(TVBOX_SOURCES) + list(extra_sources or [])
    # ---- 并入他方种子清单 (2026-11-13): seed-cfgs.txt 的 158 个配置 URL
    # (GitHub 仓转 TVBOX_SOURCES 形, 与固定清单走同一套流程; 直连 URL 另走
    # _fetch_one_tvbox_json, 在循环里按 direct_url 分支处理)。
    _seed_gh, _seed_direct = _load_seed_cfgs()
    sources = sources + _seed_gh
    for _du in _seed_direct:
        sources.append({"name": f"seed direct {_du}", "direct_url": _du})
    _seed_cfg_n = len(_seed_gh) + len(_seed_direct)
    # 2026-11-16 去重: 同一 URL 一轮只抓一次 (日志里同一 seed 连续出现两次)。
    sources, _dup_n = _dedup_sources(sources, progress)
    if _dup_n:
        _log(progress, f"[TVBox] 上游去重: 丢掉 {_dup_n} 个重复上游, 剩 {len(sources)} 个")
    out_dir = os.path.join(data_dir, "tvbox")
    os.makedirs(out_dir, exist_ok=True)
    all_sites, all_parses, all_lives = [], [], []
    all_doh, all_rules = [], []
    spider = None
    spider_candidates = []
    per_source = []

    _emit(progress, 2, 0.0, "开始抓取 TVBox 上游", 0, len(sources) + 1,
          log=f"[TVBox] 开始抓取上游 (固定 {len(TVBOX_SOURCES)} 个 + 发现 {len(sources) - len(TVBOX_SOURCES)} 个)")
    _mem_last = 0.0  # TVBox 上游循环内每 60 秒打一行内存 (2026-11-16 循环定位)
    for _si, src in enumerate(sources):
        if _deadline_hit():
            _log(progress, "[TVBox] 超时预算, 停止继续抓上游, 用已抓到的收尾")
            break
        _emit(progress, 2, _si / (len(sources) + 1),
              f"正在抓 {src['name']}（第 {_si + 1}/{len(sources) + 1} 个上游）",
              _si, len(sources) + 1)
        _now_mono = time.monotonic()
        if _now_mono - _mem_last >= 60:
            _mem_last = _now_mono
            _log_mem(progress, f"TVBox 上游抓取中 ({_si + 1}/{len(sources) + 1} {src['name']})")
        _log(progress, f"[TVBox] 正在抓 {src['name']}")
        if src.get("direct_url"):
            obj, used = _fetch_one_tvbox_json([src["direct_url"]], progress=progress,
                                              label=src["name"])
        else:
            urls = github_channels(src["owner"], src["repo"], src["branch"], src["path"])
            urls += src.get("extra", [])
            obj, used = _fetch_one_tvbox_json(urls, progress=progress, label=src["name"])
        if obj is None:
            per_source.append({"name": src["name"], "ok": False, "used_url": None,
                               "sites": 0, "error": "全部通道失败或 JSON 解析失败"})
            continue
        base = base_dir_of(used)
        # 多仓索引递归展开 (最多 2 层, 对方口径; 旧版只展开 1 层且只认 dict 条目)
        ss, pp, ll, sps, dd, rr, _hst = _harvest_tvbox_obj(obj, base, progress)
        all_sites.extend(ss); all_parses.extend(pp); all_lives.extend(ll)
        all_doh.extend(dd); all_rules.extend(rr)
        for sp in sps:
            if sp not in spider_candidates:
                spider_candidates.append(sp)
            if not spider:
                spider = sp
        per_source.append({"name": src["name"], "ok": True, "used_url": used,
                           "sites": len(ss), "parses": len(pp), "lives": len(ll),
                           "note": ("多仓索引递归解析" if _hst["sub_configs"] else "")})
        _log(progress, f"  OK {src['name']}: {len(ss)} 站 / {len(pp)} 解析 / {len(ll)} 直播")

    _emit(progress, 2, len(sources) / (len(sources) + 1),
          f"正在抓直播源 Lightconer/TVBox-Sources（第 {len(sources) + 1}/{len(sources) + 1} 个上游）",
          len(sources), len(sources) + 1,
          log="[TVBox] 正在抓直播源 Lightconer/TVBox-Sources")
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

    # ---- 并入他方种子直连接口 (2026-11-13): seed-bases.txt 轮换窗口 200 条
    # (裸 API base, 非配置文件)。直接合成 type-1 候选站, 与配置解析出的站同等
    # 走去重/受限内容过滤/三轮测速 (cfgRank=500, hits=1, seed=True, 对方口径)。
    # 注意: 种子池里多为裸 IP + 端口的采集接口, 域名收敛要求下这些新域名先收集、
    # 审批卡片用户正在逐张点, 不跳过任何源 (用户 2026-11-13 14:19 已更正)。
    _seed_bases, _seed_bases_total, _seed_offset = _load_seed_bases(data_dir)
    _seed_base_sites = 0
    for _b in _seed_bases:
        _host = urlparse(_b).netloc or _b
        all_sites.append({"key": "seed:" + _b.lower().rstrip("/"),
                          "name": _host, "type": 1, "api": _b,
                          "from_seed_base": True})
        _seed_base_sites += 1
    if _seed_base_sites:
        _log(progress, f"[TVBox] 种子直连接口并入 {_seed_base_sites} 条 "
                       f"(seed-bases 共 {_seed_bases_total} 条, 本轮窗口 offset {_seed_offset})")

    # 检查点: 上游抓完先落盘 (长抓取中途被杀也不丢已抓的上游结果)
    _atomic_write_json(os.path.join(out_dir, "checkpoint_upstreams.json"),
                       {"per_source": per_source, "sites_collected": len(all_sites),
                        "parses_collected": len(all_parses),
                        "spider_candidates": spider_candidates})
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

    # 去重: 按 key, 先到的上游优先; type-1 再按 api 去重 (种子直连接口与配置
    # 解析出的同一后端可能 key 不同、api 相同, 留先到的一个)
    seen_keys = set()
    seen_apis = set()
    deduped = []
    for s in all_sites:
        if s["key"] in seen_keys:
            continue
        if s.get("type") == 1 and isinstance(s.get("api"), str):
            _ak = s["api"].strip().lower().rstrip("/")
            if _ak in seen_apis:
                continue
            seen_apis.add(_ak)
        seen_keys.add(s["key"])
        deduped.append(s)
    # 受限内容过滤 (2026-11-13 双套重构): 严格模式, 作用于全部 TVBox 站, 去重后、
    # 测速前就过滤, 这样 type-3 池和可测速池都不会再有受限内容站。命中者不丢弃,
    # 进 B 套流程 (adult_pool), 测速/校验通过的进 tvbox_b.json。
    # 记录去重, 按 key 只记一次。
    adult_removed = []
    adult_pool = []  # 被判定受限内容的站 (含严格词命中), 走 B 套流程
    _adult_seen = set()

    def _record_adult(site, stage, reason, type_label=None):
        k = str(site.get("key", ""))
        if k in _adult_seen:
            return
        _adult_seen.add(k)
        adult_removed.append({"key": k, "name": site.get("name", ""),
                              "type": type_label if type_label is not None else site.get("type"),
                              "api": site.get("api", "") if isinstance(site.get("api", ""), str) else "",
                              "reason": reason, "stage": stage,
                              # 仅严格词命中,
                              # 强证据命中为 False
                              "strict_only": reason.startswith(
                                  ("strict:", "category-strict:", "ext-strict:")),
                              "in_adult_set": False})

    def _drop_adult(site_list, stage, collect=None):
        kept = []
        for s in site_list:
            is_adult, reason = _is_adult_tvbox(s, strict=True)
            if is_adult:
                _record_adult(s, stage, reason)
                if collect is not None:
                    collect.append(s)
            else:
                kept.append(s)
        return kept

    deduped = _drop_adult(deduped, "dedup后/全站", collect=adult_pool)
    _log(progress, f"[TVBox] 受限内容过滤: 剔除 {len(adult_removed)} 个受限内容站 "
                   f"(进 B 套流程 {len(adult_pool)} 个): "
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
    _emit(progress, 3, 0.0, f"TVBox 测速第 1 轮开始，共 {len(deduped)} 站",
          0, len(deduped), log=f"[TVBox] 第一轮实测开始，共 {len(deduped)} 站")
    round1 = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(_speed_test_one, s): s for s in deduped}
        done_n = 0
        pass_n = 0
        for fut in as_completed(futs):
            try:
                rec = fut.result()
            except Exception as e:  # noqa: BLE001
                rec = _speed_fail_record(futs[fut]["key"], e)
            round1[rec["key"]] = rec
            done_n += 1
            if rec.get("ok"):
                pass_n += 1
            if done_n % 10 == 0 or done_n == len(deduped):
                _emit(progress, 3, done_n / max(1, len(deduped)),
                      f"第一轮测速 {done_n}/{len(deduped)}，通过 {pass_n} 个",
                      done_n, len(deduped))
            if _deadline_hit():
                _log(progress, "[TVBox] 第一轮测速超时预算, 剩余用已测完的收尾")
                ex.shutdown(wait=False, cancel_futures=True)
                break
    pass1 = [r for r in round1.values() if r["ok"]]
    _log(progress, f"[TVBox] 第一轮通过 {len(pass1)}/{len(deduped)} (TTFB<{TTFB_LIMIT_MS}ms 且总耗时<{TOTAL_LIMIT_MS}ms 且有 vod 列表)")
    _atomic_write_json(os.path.join(out_dir, "checkpoint_round1.json"),
                       {"passed": len(pass1), "total": len(deduped),
                        "records": list(round1.values())})

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
        _emit(progress, 4, 0.0, f"TVBox 测速第 2 轮开始，共 {len(retest_keys)} 站",
              0, len(retest_keys),
              log=f"[TVBox] 第二轮复测前 {len(retest_keys)} 名, 确认稳定性")
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(_speed_test_one, site_by_key[k]): k for k in retest_keys}
            _done2 = 0
            _pass2 = 0
            for fut in as_completed(futs):
                try:
                    rec = fut.result()
                except Exception as e:  # noqa: BLE001
                    rec = _speed_fail_record(futs[fut], e)
                round2[rec["key"]] = rec
                _done2 += 1
                if rec.get("ok"):
                    _pass2 += 1
                if _done2 % 10 == 0 or _done2 == len(retest_keys):
                    _emit(progress, 4, 0.6 * _done2 / max(1, len(retest_keys)),
                          f"第二轮复测 {_done2}/{len(retest_keys)}，通过 {_pass2} 个",
                          _done2, len(retest_keys))
    else:
        _emit(progress, 4, 0.6, "无复测目标，跳过第二轮", 0, 0)
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

    # ---- 第三轮复测 (2026-11-13 从严): 两轮通过的再测一次, 取三轮最差值 ----
    # 只保留三轮都通过的站; 超时预算或无候选时回退到两轮结果并如实记录
    _round2_count = len(final_records)
    if final_records and not _deadline_hit():
        keys3 = [r["key"] for r in final_records]
        _emit(progress, 4, 0.6, f"TVBox 测速第 3 轮开始，共 {len(keys3)} 站",
              0, len(keys3),
              log=f"[TVBox] 第三轮复测 {len(keys3)} 站, 确认稳定性")
        round3 = {}
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(_speed_test_one, site_by_key[k]): k for k in keys3}
            _done3r = 0
            _pass3r = 0
            for fut in as_completed(futs):
                try:
                    rec = fut.result()
                except Exception as e:  # noqa: BLE001
                    rec = _speed_fail_record(futs[fut], e)
                round3[rec["key"]] = rec
                _done3r += 1
                if rec.get("ok"):
                    _pass3r += 1
                if _done3r % 10 == 0 or _done3r == len(keys3):
                    _emit(progress, 4, 0.6 + 0.4 * _done3r / max(1, len(keys3)),
                          f"第三轮复测 {_done3r}/{len(keys3)}，通过 {_pass3r} 个",
                          _done3r, len(keys3))
                if _deadline_hit():
                    _log(progress, "[TVBox] 第三轮复测超时预算, 剩余用两轮结果收尾")
                    break
        merged3 = []
        for r in final_records:
            r3 = round3.get(r["key"])
            if r3 is None or not r3.get("ok"):
                continue
            m = dict(r)
            m["ttfb_ms"] = max(r["ttfb_ms"] or 9999, r3["ttfb_ms"] or 9999)
            m["total_ms"] = max(r["total_ms"] or 9999, r3["total_ms"] or 9999)
            m["kbps"] = min(r["kbps"] or 0, r3["kbps"] or 0)
            m["checked_at"] = r3["checked_at"]
            m["rounds"] = 3
            merged3.append(m)
        _log(progress, f"[TVBox] 三轮都通过 {len(merged3)} 站 (两轮通过 {len(final_records)} 站)")
        final_records = merged3
    else:
        _log(progress, "[TVBox] 跳过第三轮复测 (无候选或超时预算), 用两轮结果收尾")

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
        # 受限内容过滤二次兜底 (严格模式): 正常流程里去重后已过滤, 这里不应触发;
        # 万一触发也剔除并记录 (不进 B 套, 因去重阶段已处理过 B 套池)
        _adult3, _reason3 = _is_adult_tvbox(s, strict=True)
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
    _emit(progress, 5, 0.0, f"type-3 校验开始，共 {len(type3_niche_pool)} 个偏门站",
          0, len(type3_niche_pool))
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(_validate_type3_one, s, spider_cache): s for s in type3_niche_pool}
        _done3 = 0
        _pass3 = 0
        for fut in as_completed(futs):
            try:
                ok, rec = fut.result()
            except Exception as e:  # noqa: BLE001
                ok, rec = False, {"key": futs[fut]["key"], "ok": False,
                                  "error": f"internal_{type(e).__name__}: {e}"}
            type3_validated[rec["key"]] = rec
            _done3 += 1
            if ok:
                _pass3 += 1
            if _done3 % 20 == 0 or _done3 == len(type3_niche_pool):
                _emit(progress, 5, 0.7 * _done3 / max(1, len(type3_niche_pool)),
                      f"type-3 校验 {_done3}/{len(type3_niche_pool)}，通过 {_pass3} 个",
                      _done3, len(type3_niche_pool))
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
    # 从严 (2026-11-13): 宁缺毋滥, 不足 80 站也不放宽补录, 数量如实记在 meta 里
    if len(picked) < MIN_TVBOX_SITES:
        _log(progress, f"[TVBox] 从严筛选后仅 {len(picked)} 站, 不足 {MIN_TVBOX_SITES} 站也不放宽补录 (数量如实记录)")
    # 排序: 可测速站按分数, type-3 偏门排在后面 (无延迟分数, 按 tag+key 排)
    _testable_pick = sorted([p for p in picked if p.get("rounds", 0) > 0],
                            key=_speed_score)
    _type3_pick = sorted([p for p in picked if p.get("rounds", 0) == 0],
                         key=lambda p: (p.get("niche_tag", ""), p["key"]))
    picked = _testable_pick + _type3_pick

    # ---- 真实带宽 + 大陆可达性 (2026-11-13 并入他方 measureThroughput/DoH/geo) ----
    # 只测可测速的入选站 (type-3 无 api, 不测); DoH 判 BLOCKED 的站剔除并从
    # 后备 (final_records 未入选者) 按速度回补; 首条片名 sample 再做一次受限内容
    # 复核 (对方口径: name + sample + base), 命中的站移入 B 套流程。
    # 注意: cn_reachability 是 DNS 层面判定, 不是真从大陆测速 (见 README/meta)。
    _extras_stats = {"measured": 0, "throughput_ok": 0, "blocked_removed": 0,
                     "backfilled": 0, "sample_adult_moved": 0}
    _testable_picked = [p for p in picked if p.get("rounds", 0) > 0]
    if _testable_picked and not _deadline_hit():
        _log(progress, f"[TVBox] 真实带宽/DoH 补测 {len(_testable_picked)} 站 (10 并发)")
        try:
            _extras = _measure_tvbox_extras(_testable_picked, site_by_key, progress)
        except Exception as e:  # noqa: BLE001
            # 补测入口抛异常只跳过补测 (站仍保留, throughput 留空),
            # 绝不杀整轮 (2026-11-16 循环根因修复)。
            _log(progress, f"[TVBox] 带宽/DoH 补测失败, 跳过: {type(e).__name__}: {e}")
            _extras = {}
        _extras_stats["measured"] = len(_extras)
        _extras_stats["throughput_ok"] = sum(
            1 for v in _extras.values() if v.get("throughput_kbps"))
        _blocked_keys = {k for k, v in _extras.items()
                         if v.get("cn_reachability") == "blocked"}
        if _blocked_keys:
            _extras_stats["blocked_removed"] = len(_blocked_keys)
            _log(progress, f"[TVBox] DoH 判定大陆封禁 (BLOCKED) {len(_blocked_keys)} 站, "
                           f"剔除并回补: {sorted(_blocked_keys)}")
            picked = [p for p in picked if p["key"] not in _blocked_keys]
            _picked_keys = {p["key"] for p in picked}
            _bf_pool = [r for r in final_records
                        if r["key"] not in _picked_keys and r["key"] not in _blocked_keys]
            _bf_pool.sort(key=_speed_score)
            _bf = _bf_pool[:len(_blocked_keys)]
            if _bf:
                try:
                    _bf_extras = _measure_tvbox_extras(_bf, site_by_key, progress)
                except Exception as e:  # noqa: BLE001
                    _log(progress, f"[TVBox] 回填站带宽/DoH 补测失败, 跳过: {type(e).__name__}: {e}")
                    _bf_extras = {}
                _extras.update(_bf_extras)
                _bf = [r for r in _bf
                       if _bf_extras.get(r["key"], {}).get("cn_reachability") != "blocked"]
                _extras_stats["backfilled"] = len(_bf)
                picked = picked + _bf
                _log(progress, f"[TVBox] BLOCKED 回补 {len(_bf)} 站")
        for p in picked:
            if p["key"] in _extras:
                p.update(_extras[p["key"]])
        # sample 受限内容复核: 名 + 首条片名 + api base。注意严格词里部分词
        # 常见于主流片名 (午夜凶铃、速度与激情), 用在 sample 上误杀
        # 太高, 故 sample 只查强证据 + 窄严格词,
        # 不用全量严格词。命中者移入 B 套 (不删除, 仍在交付物里)。
        _sample_moved = []
        _kept_picked = []
        _sample_strict_narrow = ["伦理", "情色", "情欲", "限制级"]
        for p in picked:
            if p.get("rounds", 0) == 0:
                _kept_picked.append(p)
                continue
            _hay = f"{p.get('name', '')} {p.get('sample', '')} {p.get('api', '')}"
            _kw, _sh = _text_adult_reason(_hay, strict=False)
            _hit_reason = ("sample:" + _kw) if _kw else None
            if not _hit_reason:
                for _sk in _sample_strict_narrow:
                    if _sk in _hay.lower():
                        _hit_reason = "sample-strict:" + _sk
                        break
            if _hit_reason:
                src = site_by_key.get(p["key"])
                if src is not None:
                    adult_pool.append(src)
                    _record_adult(src, "sample复核", _hit_reason)
                    _sample_moved.append(p["key"])
                    continue
            _kept_picked.append(p)
        if _sample_moved:
            _extras_stats["sample_adult_moved"] = len(_sample_moved)
            picked = _kept_picked
            _log(progress, f"[TVBox] sample 受限内容复核移入 B 套 {len(_sample_moved)} 站: {_sample_moved}")

    # ---- B 套 (2026-11-13 双套输出): 与 A 套同样的实测, 只换 sites 列表 ----
    # adult_pool 是去重阶段严格模式判受限内容的全部站 (含严格词命中); 分两路:
    #   (a) 可测速站 (type != 3): 三轮实测, 全部通过才进 B 套;
    #   (b) type-3 站: jar/ext 可达性校验, 全部通过才进 B 套 (不设配额、不分热门偏门)。
    # B 套不设 120 站上限和 70/30 配额, 有多少合格的要多少。
    # 文件在 A 套写盘时一并写出 (tvbox_b.json, spider/parses/lives 照搬 A 套)。
    adult_sites = []
    adult_speed_records = []
    adult_type3_records = []
    adult_meta = {"testable_pool": 0, "testable_passed": 0, "round1_passed": 0,
                  "type3_pool": 0, "type3_passed": 0, "site_count": 0}
    if adult_pool:
        _adult_testable = [s for s in adult_pool if s.get("type") != 3]
        _adult_type3 = [s for s in adult_pool if s.get("type") == 3]
        adult_meta["testable_pool"] = len(_adult_testable)
        adult_meta["type3_pool"] = len(_adult_type3)
        _adult_by_key = {s["key"]: s for s in _adult_testable}
        _adult_final, _adult_r1 = _speed_test_pool(
            _adult_testable, _adult_by_key, progress, " B 套 ", stage_no=5)
        adult_meta["round1_passed"] = _adult_r1
        adult_meta["testable_passed"] = len(_adult_final)
        adult_speed_records = _adult_final
        for r in _adult_final:
            cat, tag = classify_tvbox(_adult_by_key[r["key"]])
            r["category"] = cat
            r["niche_tag"] = tag
        # type-3 受限内容站: jar/ext 校验, 全部通过的都进 B 套
        _adult_type3_by_key = {s["key"]: s for s in _adult_type3}
        if _adult_type3:
            _log(progress, f"[TVBox] B 套 type-3 {len(_adult_type3)} 站, 开始 jar/ext 校验")
            _adult_t3_validated = {}
            with ThreadPoolExecutor(max_workers=10) as ex:
                futs = {ex.submit(_validate_type3_one, s, spider_cache): s
                        for s in _adult_type3}
                for fut in as_completed(futs):
                    try:
                        ok, rec = fut.result()
                    except Exception as e:  # noqa: BLE001
                        ok, rec = False, {"key": futs[fut]["key"], "ok": False,
                                          "error": f"internal_{type(e).__name__}: {e}"}
                    _adult_t3_validated[rec["key"]] = rec
            adult_type3_records = [r for r in _adult_t3_validated.values() if r["ok"]]
            adult_meta["type3_passed"] = len(adult_type3_records)
            _log(progress, f"[TVBox] B 套 type-3 校验通过 "
                           f"{len(adult_type3_records)}/{len(_adult_type3)}")
        # 组装 B 套 sites (与 A 套同形: 原站记录去 _ 私有字段), 按 key 去重
        _adult_seen_keys = set()
        for r in adult_speed_records:
            src = _adult_by_key.get(r["key"])
            if src is None or r["key"] in _adult_seen_keys:
                continue
            _adult_seen_keys.add(r["key"])
            adult_sites.append({k: v for k, v in dict(src).items()
                                if not k.startswith("_")})
        for r in sorted(adult_type3_records, key=lambda x: x["key"]):
            src = _adult_type3_by_key.get(r["key"])
            if src is None or r["key"] in _adult_seen_keys:
                continue
            _adult_seen_keys.add(r["key"])
            adult_sites.append({k: v for k, v in dict(src).items()
                                if not k.startswith("_")})
        adult_meta["site_count"] = len(adult_sites)
        _adult_keys = {s["key"] for s in adult_sites}
        for _rec in adult_removed:
            if _rec["key"] in _adult_keys:
                _rec["in_adult_set"] = True
        _log(progress, f"[TVBox] B 套: {len(adult_sites)} 站 "
                       f"(可测速 {len(adult_speed_records)} / type-3 {len(adult_type3_records)})")
        # B 套可测速站同样补真实带宽/DoH (BLOCKED 的受限内容站同样剔除)
        if adult_speed_records and not _deadline_hit():
            try:
                _adult_extras = _measure_tvbox_extras(adult_speed_records, _adult_by_key, progress)
            except Exception as e:  # noqa: BLE001
                _log(progress, f"[TVBox] B 套带宽/DoH 补测失败, 跳过: {type(e).__name__}: {e}")
                _adult_extras = {}
            _adult_blocked = {k for k, v in _adult_extras.items()
                              if v.get("cn_reachability") == "blocked"}
            if _adult_blocked:
                adult_sites = [s for s in adult_sites if s["key"] not in _adult_blocked]
                adult_speed_records = [r for r in adult_speed_records
                                       if r["key"] not in _adult_blocked]
                adult_meta["site_count"] = len(adult_sites)
                _log(progress, f"[TVBox] B 套 DoH BLOCKED 剔除 {len(_adult_blocked)} 站")
            for r in adult_speed_records:
                if r["key"] in _adult_extras:
                    r.update(_adult_extras[r["key"]])
            adult_meta["extras_measured"] = len(_adult_extras)
            adult_meta["extras_blocked"] = len(_adult_blocked)

    # ---- 依赖包下载 (2026-11-13): spider jar + 入选 type-3 站的 api/ext ----
    # 只下最终入选的 type-3 站 (picked 里 rounds==0 的项)；各站的 jar 字段在写盘前
    # 由 _localize_site_jars 统一改写成本地 ./assets/（见下文“jar 本地化”）
    _type3_final = [p for p in picked if p.get("rounds", 0) == 0]
    jar_files = _download_tvbox_assets(spider, _type3_final, type3_by_key,
                                       os.path.join(out_dir, "assets"), progress)
    # B 套 type-3 依赖包也一并下载 (同一 assets/ 目录; 文件名带 URL 短 hash,
    # 与 A 套重复的文件自动复用, 不重复下载; jar_files 按文件名去重合并)
    if adult_type3_records:
        _adult_t3_by_key = {s["key"]: s for s in adult_sites}
        _adult_jars = _download_tvbox_assets(spider, adult_type3_records,
                                             _adult_t3_by_key,
                                             os.path.join(out_dir, "assets"), progress)
        _seen_jar = {j["file"] for j in jar_files}
        jar_files += [j for j in _adult_jars if j["file"] not in _seen_jar]
        jar_files.sort(key=lambda r: r["file"])
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
    # lives 也过受限内容过滤 (按频道名 + 直播列表 URL 查关键词, 本次应为 0 个)
    _lives_raw = lives_from_source if lives_from_source else all_lives
    _lives_kept = []
    for lv in _lives_raw:
        _a, _r = _is_adult_tvbox({"key": lv.get("name", ""), "name": lv.get("name", ""),
                                  "api": lv.get("url", "")}, strict=True)
        if _a:
            _record_adult({"key": lv.get("name", ""), "name": lv.get("name", ""),
                           "api": lv.get("url", "")}, "lives", _r, type_label="live")
        else:
            _lives_kept.append(lv)
    lives = _dedup_by_url(_lives_kept)
    doh = _dedup_by_url(all_doh)
    rules = all_rules[:200]

    # 最终复核: 写文件前再查一次, 受限内容站一个都不能留 (正常应为 0 个,
    # 万一有就剔除并记录, speed_records 同步过滤以免 meta 与文件不一致)
    _final_sites = []
    for s in sites:
        _a, _r = _is_adult_tvbox(s, strict=True)
        if _a:
            _record_adult(s, "最终复核", _r)
        else:
            _final_sites.append(s)
    if len(_final_sites) != len(sites):
        _keep_keys = {s["key"] for s in _final_sites}
        speed_records = [r for r in speed_records if r["key"] in _keep_keys]
        sites = _final_sites

    # ---- jar 本地化 (2026-11-17): 各站远程 jar 下载到 assets/，jar 字段改写成
    # ./assets/<文件>（大陆访问远程 jar 慢）。失败的站保留远程 URL、不中断。
    _jar_localize = {"rewritten": 0, "already_local": 0, "kept_remote": [],
                     "md5_mismatched": []}
    try:
        _jar_localize = _localize_site_jars(list(sites) + list(adult_sites),
                                            os.path.join(out_dir, "assets"),
                                            jar_files, progress)
        jar_files.sort(key=lambda r: r["file"])
        _atomic_write_json(os.path.join(out_dir, "checkpoint_assets.json"),
                           {"jar_files": jar_files,
                            "total_jobs": len(jar_files)})
    except Exception as e:  # noqa: BLE001
        _log(progress, f"[TVBox] jar 本地化失败，保留远程 URL："
                       f"{type(e).__name__}: {e}")

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
    # B 套写盘 (与 A 套同结构, 只换 sites 列表; spider/parses/lives/doh/rules 照搬)
    if adult_sites:
        adult_out = {}
        if spider:
            adult_out["spider"] = spider
        adult_out["sites"] = adult_sites
        adult_out["parses"] = parses
        adult_out["lives"] = lives
        if doh:
            adult_out["doh"] = doh
        if rules:
            adult_out["rules"] = rules
        with open(os.path.join(out_dir, "tvbox_b.json"), "w", encoding="utf-8") as f:
            json.dump(adult_out, f, ensure_ascii=False, indent=2)
        _log(progress, f"[TVBox] B 套已写出 tvbox_b.json: {len(adult_sites)} 站")
    # 误杀修复核对 (2026-11-13): 用户点名的三站是否回到 A 套
    _clean_keys = {s["key"] for s in sites}
    _adult_keys2 = {s["key"] for s in adult_sites}
    _name_by_key = {a["key"]: a["name"] for a in adult_removed}
    for s in list(sites) + list(adult_sites):
        _name_by_key.setdefault(s["key"], s.get("name", ""))
    fixed_false_positives = []
    for _fk in ("相声", "评书", "九月影视"):
        if _fk in _clean_keys:
            fixed_false_positives.append({"key": _fk, "name": _name_by_key.get(_fk, ""),
                                          "status": "returned_to_clean",
                                          "reason_before": "旧逻辑整串匹配 ext 配置 "
                                                           "(';;av' 后缀 / 受限内容字样) 误判"})
        elif _fk in _adult_keys2:
            _rr = next((a["reason"] for a in adult_removed if a["key"] == _fk), "")
            fixed_false_positives.append({"key": _fk, "name": _name_by_key.get(_fk, ""),
                                          "status": "still_adult",
                                          "reason": _rr})
        else:
            fixed_false_positives.append({"key": _fk, "name": _name_by_key.get(_fk, ""),
                                          "status": "not_in_pool",
                                          "note": "本轮上游池中无此站, 无法验证回归; "
                                                  "分类器单元测试已确认不再误判"})
    _ps_by_name = {q.get("name"): q for q in per_source}
    _discovered_tvbox_entries = []
    for _src in (extra_sources or []):
        _q = _ps_by_name.get(_src.get("name")) or {}
        _discovered_tvbox_entries.append({
            "name": _src.get("name"),
            "discovery_source": _src.get("discovery_source"),
            "ok": bool(_q.get("ok")),
            "sites_parsed": _q.get("sites", 0),
            "passed_screening": bool(_q.get("ok") and _q.get("sites", 0) > 0),
        })
    meta = {
        "generated_at": now_iso(),
        "spider": spider,
        "spider_candidates": spider_candidates,
        "site_count": len(sites),
        "candidate_pool_size": len(deduped) + len(type3_pool) + len(adult_pool),
        "testable_pool_size": len(deduped),
        "type3_pool_size": len(type3_pool),
        "type3_niche_pool_size": len(type3_niche_pool),
        "type3_validated_passed": len(type3_passed),
        "type3_validated_failed": len(type3_failed),
        "type3_tool_removed": sorted(type3_tool_removed),
        "adult_keywords": {
            "strong_substring": TVBOX_ADULT_STRONG_KEYWORDS,
            "strict_substring_clean_only": TVBOX_ADULT_STRICT_KEYWORDS,
            "token_regex": ["av", "sex", "xxx"],
            "url_segment_tokens": sorted(TVBOX_ADULT_URL_TOKENS),
            "removed_known": TVBOX_ADULT_REMOVED_KNOWN,
        },
        "adult_removed": adult_removed,
        "adult_removed_count": len(adult_removed),
        # B 套 (2026-11-13 双套输出): 与 A 套同样的实测, 只换 sites 列表
        "adult_set": dict(adult_meta, **{
            "pass_rate": (adult_meta["testable_passed"] / adult_meta["testable_pool"]
                          if adult_meta["testable_pool"] else None),
            "type3_pass_rate": (adult_meta["type3_passed"] / adult_meta["type3_pool"]
                                if adult_meta["type3_pool"] else None),
            "speed_records": adult_speed_records,
            "type3_records": adult_type3_records,
        }),
        "fixed_false_positives": fixed_false_positives,
        "type3_quota": TYPE3_NICHE_QUOTA,
        "round1_passed": len(pass1),
        "round2_passed": _round2_count,
        "round3_passed": len(final_records),
        "rounds": 3,
        # 并入他方项目 (2026-11-13): 种子清单与真实带宽/DoH 补测
        "seed_cfgs_added": _seed_cfg_n,
        "seed_bases_window": _seed_base_sites,
        "seed_bases_total": _seed_bases_total,
        "seed_bases_offset": _seed_offset,
        "extras": _extras_stats,
        "extras_note": ("throughput_kbps 为单分片 (2MB/5s 窗口) 实测带宽; "
                        "cn_reachability/region 为 DoH + ip-api 的 DNS/地理层面判定 "
                        "(大陆解析器答不出、境外能解析即判污染), 不是真从大陆测速; "
                        "BLOCKED 的站已从 A 套与 B 套剔除并回补。"),
        "strict_no_relax": True,
        "discovery": {k: v for k, v in (discovery or {}).items()
                      if k in ("enabled", "skipped_reason", "elapsed_s",
                               "github_rate_limited")},
        "discovered_upstreams": _discovered_tvbox_entries,
        "jar_files": jar_files,
        "jar_localize": _jar_localize,
        "assets_dir": "tvbox/assets",
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
            "受限内容过滤 (2026-11-13 双套输出重构): TVBox 最终生成两套——A 套 tvbox.json (严格模式, 宁可错杀不可漏进 A 套) 与 B 套 tvbox_b.json (被判受限内容的站, 同样过三轮测速 / type-3 jar-ext 校验, 通过才入选; spider/parses/lives 照搬 A 套)。误杀修复: 旧逻辑把 key+name+api+ext 拼串匹配, XBPQ 配置 '分类url' 里的 ';;av' 语法后缀被当成 av 命中, 导致 相声/评书 被删 (两者 分类 全是曲艺内容), 九月影视的 ext 含受限内容字样也被整串匹配命中; 新逻辑只看站名/key (中文强词子串 + av/sex/xxx 词边界)、ext 字典的 '分类' 字段与 type-1 的 categories 列表、URL 域名标签/路径段 (完整等于受限内容词才算), 不再匹配 ext 的其它字段。复核结果见 fixed_false_positives。另: 新逻辑的 categories 检查还揪出旧 A 套里的 3 个漏网站, 现均已移入 B 套。AV/sex/xxx 按词边界匹配 (kxgav.com 的白嫖站不受影响); 老片不单独过滤 (1905 经典影视保留)。书源侧已有受限内容关键词过滤, 本次未改动。",
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
    _log(progress, f"[TVBox] 失败汇总（本轮至今）: {_fail_summary_text()}")
    _log(progress, f"[TVBox] 完成: {len(sites)} 站 (热门 {meta['popular_count']} / 偏门 {meta['niche_count']}) / B 套 {len(adult_sites)} 站 / {len(parses)} 解析 / {len(lives)} 直播")
    return meta


# ---------------------------------------------------------------- Legado
# 书源受限内容判定 (2026-11-13 并入他方项目 NOVEL_ADULT_RE 原文, 词表见下方正则):
# 注意对方有意不含 言情/耽美 (不把正常女频当受限内容); 部分词为子串匹配 (对方原文如此)。
NOVEL_ADULT_RE = re.compile(
    r"🔞|海棠|po18|p18|popo|popo原创|御宅屋|肉文|肉宅|肉屋|肉肉|禁忌书屋|第一版主|版主|禁忌|"
    r"色情|黄色|成人|里番|本子|18禁|十八禁|h文|高h|r18|r-18|ノベル|绅士|福利|艳情|情色|"
    r"情欲小说|色欲|色中色|色书屋|色窝|涩涩|污文|小黄文|黄书|ntr|绿帽|乱伦|调教|凌辱|"
    r"兽交|幼交|痴汉|母狗|淫|xanimu|hanime|avmoo|sehuatang|souav|98tang|jiuse|tt1069|"
    r"(?<![a-z])av(?![a-z])|(?<![a-z])porn(?![a-z])|(?<![a-z])hentai(?![a-z])|"
    r"(?<![a-z])adult(?![a-z])|(?<![a-z])xxx(?![a-z])",
    re.IGNORECASE)
# 非小说过滤 (有声/漫画/短剧, 与受限内容判定分开; 原 ADULT_KEYWORDS 里的有声/听书/
# 漫画/短剧/微短剧移到这里, 判定逻辑不变)。
NON_NOVEL_KEYWORDS = ["有声", "听书", "漫画", "漫畫", "短剧", "微短剧"]
# 兼容旧名: 仅作 meta 展示 (受限内容词 + 非小说词合并)。
ADULT_KEYWORDS = [
    "po18", "海棠", "御书屋", "御宅屋", "短剧", "微短剧",
    "成人", "色情", "黄文", "黄色", "黄书",
    "有声", "听书", "漫画", "漫畫",
]
LOGIN_PAYWALL_KEYWORDS = ["登录", "登陆", "付费", "VIP", "会员", "充值", "开通", "付费墙"]
# 测试书 (2026-11-13 女频缺口修正): 原来 5 本全是男频玄幻, 女频源用这套题
# 永远测不过。改成男女频混合: 3 本男频 + 2 本女频 (《缠珑》为用户点名的验收书,
# 《知否》为晋江最具代表性的女频书)。5 本里过 3 本即通过不变。
# 女频源另有专用题库 TEST_BOOKS_FEMALE (5 本全女频): 女频源用混合题最多只能过
# 2/5 (两本女频), 永远到不了 3/5, 所以按分类分题——每源仍是"5 本过 3 本",
# 只是题库按源的分类取, 哪套题在 meta 的 test_books_used 里如实记录。
TEST_BOOKS = [
    ("斗破苍穹", "天蚕土豆"),
    ("凡人修仙传", "忘语"),
    ("庆余年", "猫腻"),
    ("缠珑", "尤四姐"),
    ("知否知否应是绿肥红瘦", "关心则乱"),
]
TEST_BOOKS_FEMALE = [
    ("缠珑", "尤四姐"),
    ("知否知否应是绿肥红瘦", "关心则乱"),
    ("庶女攻略", "吱吱"),
    ("甄嬛传", "流潋紫"),
    ("三生三世十里桃花", "唐七公子"),
]
# 真值章节数与主角名 (四步校验用): 《缠珑》57 章取自他方项目 test-books.json
# (晋江抓取快照); 其余为公开常见数值 (约数, 仅作 0.8 章节比值的补充信号,
# toc-lagging 不淘汰)。proofs = 主角名, 正文检查时另加书名本身
# (fork 见 js/validate_relaxed.mjs)。
TEST_BOOK_TRUTH = {
    "斗破苍穹": (1648, ["萧炎", "药老"]),
    "凡人修仙传": (2448, ["韩立"]),
    "庆余年": (762, ["范闲", "五竹"]),
    "缠珑": (57, ["余蓝今", "慕容羡"]),
    "知否知否应是绿肥红瘦": (228, ["盛明兰", "顾廷烨"]),
    "庶女攻略": (306, ["罗十一娘", "徐令宜"]),
    "甄嬛传": (127, ["甄嬛", "玄凌"]),
    "三生三世十里桃花": (61, ["白浅", "夜华"]),
}
# 女频/言情源在最终 150 里的最低占比 (用户 2026-11-13 要求不低于 30 个)。
FEMALE_MIN_COUNT = 30
# 搜书抽查通过线 (2026-11-13 从严): 5 本中至少 3 本通过才算“搜得全”。
# 最终选取只收 pass>=3 或未测试 (pass=None, 中性) 的源, 不足 150 也不放宽。
SEARCH_PASS_MIN = 3
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
    # 12yue.de5.net 书源合集 (2026-11-16 用户提供: 该站 cy/书源.json 列出的
    # 7 个 yuedu520/yuedu Legado 合集, 与 yck/aoaostar 并列的第三合集)。
    # 每个合集双通道: gcore.jsdelivr.net (站方原链) + cdn.jsdelivr.net (备用)。
    {"name": "12yue 综合 2159 书源",
     "urls": ["https://gcore.jsdelivr.net/gh/yuedu520/yuedu/250422.json",
              "https://cdn.jsdelivr.net/gh/yuedu520/yuedu/250422.json"]},
    {"name": "12yue 番茄小说书源",
     "urls": ["https://gcore.jsdelivr.net/gh/yuedu520/yuedu/250410.json",
              "https://cdn.jsdelivr.net/gh/yuedu520/yuedu/250410.json"]},
    {"name": "12yue 69书源(起点)",
     "urls": ["https://gcore.jsdelivr.net/gh/yuedu520/yuedu/250412.json",
              "https://cdn.jsdelivr.net/gh/yuedu520/yuedu/250412.json"]},
    {"name": "12yue 超级精品 997 书源",
     "urls": ["https://gcore.jsdelivr.net/gh/yuedu520/yuedu/250415.json",
              "https://cdn.jsdelivr.net/gh/yuedu520/yuedu/250415.json"]},
    {"name": "12yue 三五中文网书源",
     "urls": ["https://gcore.jsdelivr.net/gh/yuedu520/yuedu/250416.json",
              "https://cdn.jsdelivr.net/gh/yuedu520/yuedu/250416.json"]},
    {"name": "12yue 漫画 83 书源",
     "urls": ["https://gcore.jsdelivr.net/gh/yuedu520/yuedu/250424.json",
              "https://cdn.jsdelivr.net/gh/yuedu520/yuedu/250424.json"]},
    {"name": "12yue 听书 49 书源",
     "urls": ["https://gcore.jsdelivr.net/gh/yuedu520/yuedu/2504241.json",
              "https://cdn.jsdelivr.net/gh/yuedu520/yuedu/2504241.json"]},
]


def _explore_titles(item):
    """从 exploreUrl 里提标题 (对方 filters.mjs 的 exploreTitles, 两遍正则):
    带引号键 ['"]title['"] 与无引号键 title: 两种写法都提, 去重后空格连接。
    无 exploreUrl 返回 ''。"""
    url = item.get("exploreUrl") or ""
    if not isinstance(url, str) or not url:
        return ""
    titles = []
    for pat in (r"""['"]title['"]\s*:\s*['"]([^'"]+)['"]""",
                r"""title\s*:\s*['"]([^'"]+)['"]"""):
        titles += re.findall(pat, url)
    seen = set()
    out = []
    for t in titles:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return " ".join(out)


def _adult_haystack(item):
    """受限内容判定 haystack: 名 + 组 + 备注 + URL +
    exploreUrl 标题, 全部小写。"""
    parts = [item.get("bookSourceName", ""), item.get("bookSourceGroup", ""),
             item.get("bookSourceComment", ""), item.get("bookSourceUrl", ""),
             _explore_titles(item)]
    # 字段可能是非字符串 (个别源的 name/url 不是 str): 先转 str 再 join,
    # 否则 TypeError 会杀死整轮书源过滤 (2026-11-16 循环根因修复)。
    return " ".join(str(p) for p in parts if p).lower()


def _is_adult_or_non_novel(item):
    if NOVEL_ADULT_RE.search(_adult_haystack(item)):
        return True
    hay = (f"{item.get('bookSourceName', '')} {item.get('bookSourceGroup', '')} "
           f"{item.get('bookSourceUrl', '')}").lower()
    return any(k in hay for k in NON_NOVEL_KEYWORDS)


def _completeness(item):
    """规则齐全度 0-11 (对方 completeness, 2026-11-13 并入):
    ruleSearch[bookList,name,author,bookUrl] 4 + ruleBookInfo[name,author,tocUrl] 3
    + ruleToc[chapterList,chapterName,chapterUrl] 3 + ruleContent[content] 1。
    另要求 searchUrl 或 exploreUrl 至少其一 (无搜索入口的源不入池)。
    旧版 0-6 只计大类, 新版按字段计, 排序更细。"""
    score = 0
    rs = item.get("ruleSearch") or {}
    if isinstance(rs, dict):
        for k in ("bookList", "name", "author", "bookUrl"):
            if rs.get(k):
                score += 1
    elif rs:
        score += 1
    bi = item.get("ruleBookInfo") or {}
    if isinstance(bi, dict):
        for k in ("name", "author", "tocUrl"):
            if bi.get(k):
                score += 1
    elif bi:
        score += 1
    tc = item.get("ruleToc") or {}
    if isinstance(tc, dict):
        for k in ("chapterList", "chapterName", "chapterUrl"):
            if tc.get(k):
                score += 1
    elif tc:
        score += 1
    rc = item.get("ruleContent") or {}
    if isinstance(rc, dict):
        if rc.get("content"):
            score += 1
    elif rc:
        score += 1
    return score


def _has_search_entry(item):
    """有搜索入口: searchUrl 或 exploreUrl 至少其一 (对方 completeness 门槛)。"""
    return bool(item.get("searchUrl") or item.get("exploreUrl"))


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
    tpl = (item.get("searchUrl") or "").strip() if isinstance(item.get("searchUrl"), str) else ""
    if not tpl:
        return None, "invalid", "empty_searchUrl"
    # (1) 先剥 @js: 尾巴, 再拆配置, 否则 POST 配置解析失败
    tpl = _strip_js_tail(tpl)
    if not tpl or not isinstance(tpl, str):
        return None, "invalid", "empty_after_js_strip"
    base = str(item.get("bookSourceUrl") or "").split("#")[0].strip()
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
            config = json.loads(tpl[idx + 1:], strict=False)
        except Exception:
            try:
                config = json.loads(tpl[idx + 1:].replace("'", '"'), strict=False)
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


def _search_test_one_detailed_impl(item, book_title, author):
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
        # 修复 (2026-11-13 诊断): searchUrl 通篇是 @js: 代码、剥掉后为空, 说明该源的
        # 搜索只能由 JS 引擎执行, 纯 HTTP 无法复现——与其它 JS 模板同理, 应记跳过
        # (未测试), 而不是判 0 分。之前误判为 fail:parse_empty_after_js_strip,
        # 会拉低通过率 (抽样 40 个源里 1 个因此得 0/5)。
        if parse_reason in ("empty_after_js_strip",):
            return None, f"skipped:js_only_searchUrl"
        return False, f"fail:parse_{parse_reason}"
    method, url, body, charset = parsed
    if method == "POST" and _is_post_blocked_env():
        # 本机出口拦截 POST (httpbin POST 也超时), 不是源站坏: 跳过, 不判 0 分
        return None, "skipped:env_post_blocked"
    try:
        if method == "POST":
            r = requests.post(url, headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
                              data=body.encode("utf-8") if body else None, timeout=BOOK_SEARCH_TIMEOUT,
                              proxies=_TEST_PROXIES)
        else:
            r = requests.get(url, headers=HEADERS, timeout=BOOK_SEARCH_TIMEOUT, proxies=_TEST_PROXIES)
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


def _search_test_one_detailed(item, book_title, author):
    """单本书搜索实测外壳: 任何未预料的内部异常都记为跳过 (不判 0 分),
    绝不让一个子任务的异常杀死整轮抓取 (2026-11-16 循环根因修复:
    旧版 fut.result() 会把 _parse_search_request_full 等处的异常抛回主线程,
    书源阶段一崩整轮就没写 status.json, 调度器每小时重开一轮)。"""
    try:
        return _search_test_one_detailed_impl(item, book_title, author)
    except Exception as e:  # noqa: BLE001
        return None, f"skipped:internal_{type(e).__name__}:{str(e)[:100]}"


def _search_test_one(item, book_title, author):
    """单本书搜索实测: 200 且正文含书名或作者才算通过 (兼容旧调用, 跳过返回 None)。
    按 charset 解码 (很多站是 GBK, requests 默认按 utf-8 解会成乱码导致误判),
    POST 模板按 POST 发, 相对路径按 bookSourceUrl 解析。"""
    result, _reason = _search_test_one_detailed(item, book_title, author)
    return result


# ------------------------------------------------------------------ 并入他方项目 (2026-11-13)
# 以下函数为本次合并新增: 宽松 JSON、种子清单加载、aoaostar 合集、Node 四步校验
# 驱动、TVBox 真实带宽/DoH/地理判定。对方 Node 侧代码在 js/ 目录 (MIT, 见首行注释)。

def strip_json_noise(text):
    """去 BOM、// 与 /* */ 注释、尾逗号 (对方 http.mjs stripJsonNoise 的 Python 版)。
    字符串内的 // 不动。失败时原样返回, 由调用方 json.loads 报错处理。"""
    if not isinstance(text, str):
        return text
    t = text.replace("\ufeff", "").strip()
    if "//" in t or "/*" in t:
        out = []
        in_str = False
        esc = False
        i = 0
        n = len(t)
        while i < n:
            c = t[i]
            nxt = t[i + 1] if i + 1 < n else ""
            if in_str:
                out.append(c)
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                i += 1
                continue
            if c == '"':
                in_str = True
                out.append(c)
                i += 1
                continue
            if c == "/" and nxt == "/":
                while i < n and t[i] != "\n":
                    i += 1
                out.append("\n")
                continue
            if c == "/" and nxt == "*":
                i += 2
                while i < n and not (t[i] == "*" and i + 1 < n and t[i + 1] == "/"):
                    i += 1
                i += 2
                continue
            out.append(c)
            i += 1
        t = "".join(out)
    t = re.sub(r",(\s*[}\]])", r"\1", t)
    return t


def _project_file(name):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def _load_seed_cfgs():
    """读 seed-cfgs.txt (对方 165 条, 已过滤成 158 条: 去掉与固定清单完全相同
    的文件、可疑域名、重复 extra)。GitHub raw/jsDelivr 转 TVBOX_SOURCES 形;
    其余直连 URL 记 direct_url。返回 (github_sources, direct_urls)。"""
    path = _project_file("seed-cfgs.txt")
    gh_sources, direct = [], []
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except Exception:
        return gh_sources, direct
    for raw in lines:
        u = raw.strip()
        if not u or u.startswith("#"):
            continue
        m = re.search(r"raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)", u)
        m2 = None
        if not m:
            m2 = re.search(r"cdn\.jsdelivr\.net/gh/([^/]+)/([^/@]+)@([^/]+)/(.+)", u)
        if m:
            owner, repo, branch, pth = m.group(1), m.group(2), m.group(3), m.group(4).split("?")[0]
        elif m2:
            owner, repo, branch, pth = m2.group(1), m2.group(2), m2.group(3), m2.group(4).split("?")[0]
        else:
            direct.append(u)
            continue
        gh_sources.append({"name": f"seed {owner}/{repo} {pth}",
                           "owner": owner, "repo": repo, "branch": branch, "path": pth})
    return gh_sources, direct


def _load_seed_bases(data_dir):
    """读 seed-bases.txt (549 条去重后 API base) + data/tvbox/rotation.json 轮换:
    每轮取 200 条窗口, offset 每轮前进 200 (对方 rotation 机制, seedCap=200)。
    返回 (window_bases, total, offset)。"""
    path = _project_file("seed-bases.txt")
    try:
        bases = [l.strip() for l in open(path, encoding="utf-8")
                 if l.strip() and not l.strip().startswith("#")]
    except Exception:
        return [], 0, 0
    seen, uniq = set(), []
    for b in bases:
        k = b.lower().rstrip("/")
        if k not in seen:
            seen.add(k)
            uniq.append(b)
    rot_path = os.path.join(data_dir, "tvbox", "rotation.json")
    offset = 0
    try:
        with open(rot_path, encoding="utf-8") as f:
            offset = int(json.load(f).get("offset", 0))
    except Exception:
        offset = 0
    if not uniq:
        return [], 0, 0
    start = offset % len(uniq)
    window = (uniq[start:] + uniq[:start])[:200]
    try:
        os.makedirs(os.path.dirname(rot_path), exist_ok=True)
        with open(rot_path, "w", encoding="utf-8") as f:
            json.dump({"offset": offset + 200, "at": now_iso()}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return window, len(uniq), offset


AOA_BASE = "https://legado.aoaostar.com"
AOA_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}
# aoaostar 女频专包 (首页 bundle 名 -> hash, 2026-11-13 实测核对):
# 关耳女频 86 条 = e3e5d620。其它 bundle: 全量 b778fe6b / XIU2精品 71e56d4f /
# 破冰 4dc410d1 / shidahuilang e29e19ee (同步失败) / 三舞313 2a1f129b / 开源阅读软件 3bb7b751。
AOA_FEMALE_BUNDLE_HASH = "e3e5d620"


def _fetch_aoa_sources(progress=None):
    """抓 aoaostar 合集 (对方 fetchAoaSources 做法):
    首页 GET -> 正则提 legado://import/bookSource 链接的 hash (只取书源合集,
    不取订阅源/净化规则/主题/TTS) -> 并发 4 拉各合集 JSON。
    返回 [{name, hash, url, count, items, error}] (失败的 bundle 记 count=0)。
    首页失败返回 []。bundle 名与 hash 按文档顺序配对 (页面结构: 每 bundle 块
    先出现名字、后出现 访问直链/一键导入 两个同 hash 链接); 配对不上时名字记 hash。"""
    try:
        r = requests.get(AOA_BASE + "/", headers=AOA_HEADERS, timeout=25)
        if r.status_code != 200:
            _log(progress, f"[aoaostar] 首页 HTTP {r.status_code}, 跳过第二合集")
            return []
        html = r.text
    except Exception as e:  # noqa: BLE001
        _log(progress, f"[aoaostar] 首页抓取失败: {e}, 跳过第二合集")
        return []
    hashes = []
    for m in re.finditer(r"legado://import/bookSource\?src=https://legado\.aoaostar\.com/sources/([a-f0-9]+)\.json", html):
        if m.group(1) not in hashes:
            hashes.append(m.group(1))
    # bundle 名: 对每个 hash, 从其首次出现处向前找最近的 bundle 名
    # (页面结构: 每 bundle 块先出现名字、后出现 访问直链/一键导入 两个同 hash 链接)。
    _ui = {"书源", "订阅源", "净化规则", "阅读排版", "主题", "在线朗读引擎",
           "点击展开", "访问直链", "一键导入", "[访问网站]暂无", "访问网站"}
    bundles = []
    for h in hashes:
        idx = html.find(f"sources/{h}.json")
        name = h
        if idx > 0:
            # idx 落在 <a href="...sources/<hash>.json"> 的属性里: 先退到上一个
            # 完整标签的结尾, 再剥标签取可见文本, 否则会残留半截 <a href="...
            cut = html.rfind(">", 0, idx)
            before = re.sub(r"<[^>]+>", "\n", html[:cut + 1]) if cut > 0 else ""
            for line in reversed(before.splitlines()):
                line = line.strip().rstrip("🔥").strip()
                if not line or line in _ui:
                    continue
                if line.startswith(("上一次同步状态", "更新时间", "同步时间")):
                    continue
                name = line
                break
        bundles.append({"name": name, "hash": h, "url": f"{AOA_BASE}/sources/{h}.json"})

    def _fetch_bundle(b):
        # 2026-11-17 内存修复 (书源入口 OOM): 之前这里下载+全量解析每个
        # bundle JSON (3,907 条) 并把全部 items 留在内存, 但调用方只用
        # count/name/hash/url, 主循环还会重新拉一遍 —— 纯浪费, 且 4 并发
        # 大 JSON 解析 (+strip_json_noise 逐字复制) 是书源入口 OOM 的主因。
        # 现只数 bookSourceUrl 出现次数 (每条源恰好一个该键), 不解析、
        # 不保留 items; 响应流式读、20MB 上限, 超限按失败处理。
        try:
            rr = requests.get(b["url"], headers=AOA_HEADERS, timeout=30, stream=True)
            if rr.status_code != 200:
                return {**b, "count": 0, "items": [], "error": f"HTTP {rr.status_code}"}
            buf = bytearray()
            try:
                for chunk in rr.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    buf.extend(chunk)
                    if len(buf) > MAX_FETCH_BYTES:
                        return {**b, "count": 0, "items": [],
                                "error": f"响应过大 (>{MAX_FETCH_BYTES // 1024 // 1024}MB), 跳过"}
            finally:
                rr.close()
            count = bytes(buf).decode("utf-8", errors="replace").count('"bookSourceUrl"')
            return {**b, "count": count, "items": [], "error": ""}
        except Exception as e:  # noqa: BLE001
            return {**b, "count": 0, "items": [], "error": f"{type(e).__name__}: {e}"}

    results = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(_fetch_bundle, b): b for b in bundles}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as e:  # noqa: BLE001
                # 单个包抛异常只记该包 0 条, 不杀整轮 (2026-11-16 循环根因修复)。
                _b = futs[fut]
                results.append({**_b, "count": 0, "items": [],
                                "error": f"internal_{type(e).__name__}: {e}"})
    order = {b["hash"]: i for i, b in enumerate(bundles)}
    results.sort(key=lambda x: order.get(x["hash"], 999))
    _log(progress, "[aoaostar] 合集: " + ", ".join(
        f"{r['name']} {r['count']} 条" for r in results))
    return results


_MULTI_PART_TLD = re.compile(r"^(com|net|org|gov|edu|co|ac|or|ne|go|in|me|tv|cc|info|biz)\.(cn|hk|tw|mo|uk|jp|kr|sg|us|io|me|tv)$", re.IGNORECASE)


def _registrable_domain(hostname):
    """注册域 (对方 registrableDomain): IP 直返; 多段 TLD 命中取末三段, 否则末两段。
    www./m./api. 前缀因此归一, 同一站的多镜像归为一个域。"""
    h = (hostname or "").lower()
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", h):
        return h
    parts = [p for p in h.split(".") if p]
    if len(parts) <= 2:
        return h
    last2 = ".".join(parts[-2:])
    if _MULTI_PART_TLD.match(last2):
        return ".".join(parts[-3:])
    return last2


def _node_bin():
    import shutil
    return shutil.which("node")


def _run_node_validation(domains, deadline_s=1500, concurrency=24, progress=None):
    """调 Node 驱动 (js/validate_books.js) 做四步校验。
    domains: [{domain, mirrors:[source...], books:[{title,author,truthChapters,proofs}]}]。
    返回 (results_by_domain, stats, error)。Node 不可用或驱动失败返回
    (None, {}, error), 调用方回退到 Python 搜索抽查。
    驱动内部: hostAlive 门 + 镜像轮换 + 超时预算内收尾 (到期未开的域记 skipped-deadline)。"""
    node = _node_bin()
    if not node:
        return None, {}, "node_not_found"
    job = {"proxy": _PROXY_URL or None, "concurrency": concurrency,
           "deadlineEpochMs": int((time.time() + deadline_s) * 1000),
           "domains": domains}
    base = os.path.dirname(os.path.abspath(__file__))
    tmpdir = os.path.join(base, ".validate_tmp")
    job_path = os.path.join(tmpdir, "job.json")
    out_path = os.path.join(tmpdir, "out.json")
    try:
        os.makedirs(tmpdir, exist_ok=True)
        with open(job_path, "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        # 任务文件写不出去 (只读文件系统/磁盘满) 也只算 Node 不可用, 回退
        # Python 抽查, 绝不让整轮死在这里 (2026-11-16 循环根因修复)。
        return None, {}, f"{type(e).__name__}: {e}"
    driver = os.path.join(base, "js", "validate_books.js")
    try:
        import subprocess
        r = subprocess.run([node, driver, job_path, out_path],
                           capture_output=True, text=True,
                           timeout=deadline_s + 180)
        if r.returncode != 0:
            return None, {}, f"node_exit_{r.returncode}: {(r.stderr or r.stdout or '')[:300]}"
        data = json.load(open(out_path, encoding="utf-8"))
        return data.get("results", []), data.get("stats", {}), ""
    except Exception as e:  # noqa: BLE001
        return None, {}, f"{type(e).__name__}: {e}"


# ---- TVBox 真实带宽 + 大陆可达性 (对方 crawl-tvbox.mjs 方法, Python 实现) ----
# 注意: 这是 DNS 层面判定 + 单分片带宽抽查, 不是真从大陆测速。
# README 与 meta.json 里如实写明, 不要把 region/score 说成大陆实测。
DOH_RESOLVERS = [
    ("https://dns.alidns.com/resolve", True),    # 阿里 (大陆)
    ("https://doh.pub/dns-query", True),         # 腾讯 (大陆)
    ("https://cloudflare-dns.com/dns-query", False),
    ("https://dns.google/resolve", False),
]
_CF_RE = re.compile(r"cloudflare", re.IGNORECASE)
_CN_AS_RE = re.compile(r"chinanet|china telecom|china unicom|china mobile|alibaba|aliyun|tencent|huawei|ucloud|baidu|kingsoft|cnservers|wangsu|chinacache|baishan|zenlayer|lightlayer|21vianet|drpeng", re.IGNORECASE)
_NEAR_AS_RE = re.compile(r"cogent|gtt|ntt|pccw|hkt|hkbn|new century|telstra|kddi|softbank|kt corp|sk broadband|singtel|starhub|viettel", re.IGNORECASE)


def _region_class(geo):
    """对方 regionClass: 返回 (code, bonus)。geo 为 None -> ('??', 0)。
    BLOCKED -40 / SUSPECT -14 / CF 20 / CN 52(+6 大陆运营商) / HK 34(+6) /
    TW/MO 26 / SG/JP/KR/MY/TH/VN 18(+4 邻近运营商) / 其它 5。"""
    if not geo:
        return "??", 0
    if geo.get("blockedInCn"):
        return "BLOCKED", -40
    if geo.get("suspectedBlocked"):
        return "SUSPECT", -14
    who = f"{geo.get('isp', '')} {geo.get('as', '')}"
    if _CF_RE.search(who):
        return "CF", 20
    cc = geo.get("countryCode") or ""
    if cc == "CN":
        return "CN", 58 if _CN_AS_RE.search(who) else 52
    if cc == "HK":
        return "HK", 40 if _CN_AS_RE.search(who) else 34
    if cc in ("TW", "MO"):
        return cc, 26
    if cc in ("SG", "JP", "KR", "MY", "TH", "VN"):
        return cc, 22 if _NEAR_AS_RE.search(who) else 18
    return cc or "??", 5


def _doh_resolve(hosts, progress=None):
    """DoH 并行问 4 个解析器 (对方 resolveHosts)。
    返回 {host: {cnIp, foreignIp, cnAnswered, foreignAnswered, cnPoisoned,
                 blocked, suspected, ip}}。IP 直连主机跳过 DoH。
    污染判定 (仅 foreignAnswered>0 时有意义, 否则只是死域名):
    cnPoisoned>=2 且无大陆应答 -> blocked; 否则 cnPoisoned>=1 -> suspected。"""
    results = {}
    for h in hosts:
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", h or ""):
            results[h] = {"cnIp": None, "foreignIp": None, "cnAnswered": 0,
                          "foreignAnswered": 0, "cnPoisoned": 0, "blocked": False,
                          "suspected": False, "ip": h}
    todo = [h for h in hosts if h not in results]

    def _query(resolver_url, host):
        try:
            r = requests.get(resolver_url, params={"name": host, "type": "A"},
                             headers={"Accept": "application/dns-json"}, timeout=12)
            if r.status_code != 200:
                return None
            return r.json()
        except Exception:  # noqa: BLE001
            return None

    def _resolve_one(host):
        cn_ip = foreign_ip = None
        cn_ans = foreign_ans = poisoned = 0
        for url, is_cn in DOH_RESOLVERS:
            j = _query(url, host)
            if not j or not isinstance(j, dict):
                continue
            answers = j.get("Answer") or []
            if not isinstance(answers, list):
                answers = []
            ips = [a.get("data") for a in answers
                   if isinstance(a, dict) and a.get("type") == 1 and a.get("data") and a.get("data") != "0.0.0.0"]
            if ips:
                if is_cn:
                    cn_ip = cn_ip or ips[0]
                    cn_ans += 1
                else:
                    foreign_ip = foreign_ip or ips[0]
                    foreign_ans += 1
            elif is_cn and (j.get("Status") == 3
                            or any(isinstance(a, dict) and a.get("data") == "0.0.0.0" for a in answers)):
                poisoned += 1
        blocked = foreign_ans > 0 and poisoned >= 2 and not cn_ans
        suspected = foreign_ans > 0 and not blocked and poisoned >= 1
        return {"cnIp": cn_ip, "foreignIp": foreign_ip, "cnAnswered": cn_ans,
                "foreignAnswered": foreign_ans, "cnPoisoned": poisoned,
                "blocked": blocked, "suspected": suspected,
                "ip": cn_ip or foreign_ip}

    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(_resolve_one, h): h for h in todo}
        for fut in as_completed(futs):
            try:
                results[futs[fut]] = fut.result()
            except Exception:  # noqa: BLE001
                # DoH 单个 host 解析失败只记该 host 未知, 不杀整轮
                # (2026-11-16 循环根因修复)。
                results[futs[fut]] = {"cnIp": None, "foreignIp": None, "cnAnswered": 0,
                                      "foreignAnswered": 0, "cnPoisoned": 0,
                                      "blocked": False, "suspected": False, "ip": None}
    return results


def _geo_lookup(ips, progress=None):
    """ip-api 批量查归属地 (对方 geoLookup): 每批 100 个, 批间 1300ms, 超时 20s。
    返回 {ip: {countryCode, isp, as, ...}}。失败的批次整批无记录 (仅作排名提示);
    429 时整批丢弃不重试 (免费版 45 请求/分钟限流)。"""
    out = {}
    ips = [ip for ip in dict.fromkeys(ips) if ip]
    for i in range(0, len(ips), 100):
        batch = ips[i:i + 100]
        try:
            r = requests.post("http://ip-api.com/batch?fields=status,countryCode,isp,as,query",
                              json=[{"query": ip} for ip in batch],
                              headers={"Content-Type": "application/json"}, timeout=20)
            if r.status_code != 200:
                _log(progress, f"[geo] ip-api 批次 HTTP {r.status_code}, 该批无地理记录")
                continue
            for rec in r.json():
                if isinstance(rec, dict) and rec.get("status") == "success" and rec.get("query"):
                    out[rec["query"]] = rec
        except Exception as e:  # noqa: BLE001
            _log(progress, f"[geo] ip-api 批次失败: {e}")
        if i + 100 < len(ips):
            time.sleep(1.3)
    return out


def _m3u8_candidates(play_url, limit=4):
    """从 vod_play_url 提候选 m3u8 列表 (按 $$$ 分组、# 分集、$ 取第 2 段),
    优先 .m3u8, 不足时补 http(s) URL。最多 limit 个。"""
    cands = []
    if not play_url:
        return cands
    for group in str(play_url).split("$$$"):
        for ep in group.split("#"):
            parts = ep.split("$")
            url = (parts[1] if len(parts) > 1 else parts[0]).strip()
            if re.search(r"\.m3u8(\?|$)", url, re.IGNORECASE) and url not in cands:
                cands.append(url)
            if len(cands) >= limit:
                return cands
    if not cands:
        for group in str(play_url).split("$$$"):
            for ep in group.split("#"):
                parts = ep.split("$")
                url = (parts[1] if len(parts) > 1 else parts[0]).strip()
                if url.startswith(("http://", "https://")) and url not in cands:
                    cands.append(url)
                if len(cands) >= limit:
                    return cands
    return cands


def _m3u8_of(play_url):
    """从 vod_play_url 取 m3u8 (对方 m3u8Of): 按 $$$ 分组、# 分集、$ 取第 2 段;
    优先第一个 .m3u8 URL, 否则首个 http(s) URL。"""
    cands = _m3u8_candidates(play_url, limit=1)
    return cands[0] if cands else None


def _measure_throughput(play_url, timeout=8):
    """真实带宽 (对方 measureThroughput): m3u8 -> (master 再跳一层) -> 首个
    .ts/.m4s 分片, 下载 2MB 或 5 秒先到者停, 算 kbps。
    有效门槛 bytes>=32768 且 secs>=0.15, 否则 None。"""
    def _fetch_m3u8(url):
        # 浏览器 UA: 部分视频 CDN 拦非浏览器 UA (实测 dytt 的 m3u8 对
        # TVBox-Legado-Fetcher/1.0 返回 403, 浏览器 UA 也不一定过, 如实记录)。
        r = requests.get(url, headers={"User-Agent": BROWSER_UA},
                         timeout=min(timeout, 8), stream=True, proxies=_TEST_PROXIES)
        if r.status_code != 200:
            return None
        body = b""
        for chunk in r.iter_content(chunk_size=65536):
            body += chunk
            if len(body) >= 2 * 1024 * 1024:
                break
        return body.decode("utf-8", errors="ignore")
    try:
        text = _fetch_m3u8(play_url)
        if text is None:
            return None
    except Exception:  # noqa: BLE001
        return None
    media_url = play_url
    if "#EXTINF:" not in text:
        m = re.search(r"^(?!#)(\S+\.m3u8\S*)$", text, re.IGNORECASE | re.MULTILINE)
        if not m:
            return None
        media_url = urljoin(play_url.rsplit("/", 1)[0] + "/", m.group(1))
        try:
            text = _fetch_m3u8(media_url)
            if text is None:
                return None
        except Exception:  # noqa: BLE001
            return None
    seg_base = media_url.rsplit("/", 1)[0] + "/"
    seg = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if re.search(r"\.(ts|m4s|mp4|aac)(\?|$)", line, re.IGNORECASE):
            seg = line
            break
    if not seg:
        return None
    seg_url = seg if seg.startswith(("http://", "https://")) else urljoin(seg_base, seg)
    t0 = time.monotonic()
    nbytes = 0
    try:
        with requests.get(seg_url, headers={"User-Agent": BROWSER_UA},
                          timeout=min(timeout, 8), stream=True,
                          proxies=_TEST_PROXIES) as r:
            if r.status_code != 200:
                return None
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    nbytes += len(chunk)
                if nbytes >= 2 * 1024 * 1024:
                    break
                if time.monotonic() - t0 >= 5:
                    break
    except Exception:  # noqa: BLE001
        return None
    secs = time.monotonic() - t0
    if nbytes < 32768 or secs < 0.15:
        return None
    return {"kbps": round((nbytes * 8) / secs / 1000), "bytes": nbytes, "secs": round(secs, 2)}


def _probe_tvbox_play(api):
    """取一站的可播 m3u8 (对方 probeOne 的播放部分):
    列表 ac=videolist&pg=1 -> 首条 vod_play_url (或详情 ac=detail&ids= 取),
    解析 m3u8, 校验 #EXTM3U 并取最优码率/分辨率。
    返回 {alive, listMs, total, sample, bitrateKbps, resolution, play:{url,from,m3u8}}
    或 None (列表都拉不到)。"""
    base = (api or "").strip()
    if not base.startswith(("http://", "https://")):
        return None
    sep = "&" if "?" in base else "?"
    t0 = time.monotonic()
    items = None
    total = 0
    for u in (base + sep + "ac=videolist&pg=1", base + "/?ac=videolist&pg=1"):
        try:
            r = requests.get(u, headers=HEADERS, timeout=8, proxies=_TEST_PROXIES)
            if r.status_code != 200:
                continue
            j = r.json()
            lst = j.get("list") or j.get("data") or (j if isinstance(j, list) else None)
            if isinstance(lst, list) and lst:
                items = lst
                total = int(j.get("total") or j.get("total_count") or len(lst))
                break
        except Exception:  # noqa: BLE001
            continue
    if not items:
        return None
    list_ms = int((time.monotonic() - t0) * 1000)
    # 首条可能是非 dict (个别站 list 里混字符串): 先判类型再取字段,
    # 否则 AttributeError 会从 _measure_tvbox_extras 的 fut.result() 抛回,
    # 杀死整轮 TVBox 抓取 (2026-11-16 循环根因修复)。
    _first = items[0] if isinstance(items[0], dict) else {}
    sample = str(_first.get("vod_name") or "")
    play_url = None
    play_from = ""
    for it in items[:4]:
        if not isinstance(it, dict):
            continue
        pu = it.get("vod_play_url") or ""
        if pu:
            play_url = pu
            play_from = str(it.get("vod_play_from") or "")
            break
    if not play_url:
        vid = _first.get("vod_id") or _first.get("id")
        if vid:
            for tmpl in (f"ac=detail&ids={vid}", f"ac=videolist&ids={vid}"):
                try:
                    r = requests.get(base + sep + tmpl, headers=HEADERS, timeout=8,
                                     proxies=_TEST_PROXIES)
                    if r.status_code != 200:
                        continue
                    j = r.json()
                    lst = j.get("list") or []
                    if lst and isinstance(lst[0], dict):
                        play_url = lst[0].get("vod_play_url") or ""
                        play_from = str(lst[0].get("vod_play_from") or "")
                        if play_url:
                            break
                except Exception:  # noqa: BLE001
                    continue
    if not play_url:
        return {"alive": True, "listMs": list_ms, "total": total, "sample": sample,
                "bitrateKbps": 0, "resolution": "", "play": None}
    # 候选 m3u8 逐个试 (不同集/不同 CDN, 一个 403/超时不代表全站不可播);
    # 用浏览器 UA (部分 CDN 拦非浏览器 UA)。首个 200 且 #EXTM3U 的胜出。
    cands = _m3u8_candidates(play_url, limit=4)
    if not cands:
        return {"alive": True, "listMs": list_ms, "total": total, "sample": sample,
                "bitrateKbps": 0, "resolution": "",
                "play": {"url": play_url, "from": play_from, "m3u8": False}}
    _ua = {"User-Agent": BROWSER_UA}
    for m3u8 in cands:
        try:
            r = requests.get(m3u8, headers=_ua, timeout=10, proxies=_TEST_PROXIES)
            if r.status_code != 200 or not r.text.lstrip().startswith("#EXTM3U"):
                continue
            text = r.text
            best_bw = 0
            best_res = ""
            best_w = 0
            for m in re.finditer(r"#EXT-X-STREAM-INF[^\n]*BANDWIDTH=(\d+)(?:[^\n]*RESOLUTION=(\d+)x(\d+))?", text):
                bw = int(m.group(1))
                if bw > best_bw:
                    best_bw = bw
                if m.group(2) and int(m.group(2)) > best_w:
                    best_w = int(m.group(2))
                    best_res = f"{m.group(2)}x{m.group(3)}"
            return {"alive": True, "listMs": list_ms, "total": total, "sample": sample,
                    "bitrateKbps": round(best_bw / 1000) if best_bw else 0,
                    "resolution": best_res,
                    "play": {"url": m3u8, "from": play_from, "m3u8": True}}
        except Exception:  # noqa: BLE001
            continue
    return {"alive": True, "listMs": list_ms, "total": total, "sample": sample,
            "bitrateKbps": 0, "resolution": "",
            "play": {"url": cands[0], "from": play_from, "m3u8": False}}


def _score_rec_tvbox(rec):
    """对方 scoreRec (逐项累加, 四舍五入到 0.1):
    地域分 + 片库规模 min(26, log10(max(total,10))*5) + 码率 min(16, bitrateKbps/220)
    + 分辨率 (>=2160:+8, >=1080:+5, >=720:+2) + 实测带宽
    (min(22, log2(max(kbps,100)/100)*7); <1200:-12; <600:再-18; 无:-6)
    + 播放 (m3u8:+7, 非 m3u8:+2, 无:-14) + 接口延迟 max(0, 8-listMs/300)
    + 配置内排名 max(0, 5-cfgRank/60)。"""
    import math
    s = 0.0
    s += rec.get("region_bonus", 0)
    total = rec.get("total") or 0
    s += min(26, math.log10(max(total, 10)) * 5.0)
    br = rec.get("bitrate_kbps") or 0
    if br:
        s += min(16, br / 220)
    res = rec.get("resolution") or ""
    try:
        h = int(res.split("x")[1]) if "x" in res else 0
    except Exception:  # noqa: BLE001
        h = 0
    if h >= 2160:
        s += 8
    elif h >= 1080:
        s += 5
    elif h >= 720:
        s += 2
    tp = rec.get("throughput_kbps")
    if tp:
        s += min(22, math.log2(max(tp, 100) / 100) * 7)
        if tp < 1200:
            s -= 12
        if tp < 600:
            s -= 18
    else:
        s -= 6
    play = rec.get("play") or {}
    if play.get("m3u8"):
        s += 7
    elif play:
        s += 2
    else:
        s -= 14
    lm = rec.get("list_ms")
    if lm is not None:
        s += max(0, 8 - lm / 300)
    rank = rec.get("cfg_rank")
    if rank is not None:
        s += max(0, 5 - rank / 60)
    return round(s * 10) / 10


def _measure_tvbox_extras(records, site_by_key, progress=None):
    """对入选站 (records 为 speed_records) 补真实带宽 + 大陆可达性:
    probe 播放 -> throughput -> DoH (按 api_host 去重) -> ip-api 地理 ->
    region/score。BLOCKED 的站标记 cn_reachability='blocked' (调用方剔除)。
    返回 {key: extras_dict}。失败的项记 None 字段, 不抛错。"""
    extras = {}

    def _one(rec):
        site = site_by_key.get(rec["key"]) or {}
        api = site.get("api") or rec.get("api") or ""
        out = {"throughput_kbps": None, "throughput_bytes": None, "throughput_secs": None,
               "bitrate_kbps": 0, "resolution": "", "play": None, "list_ms": None,
               "total": 0, "sample": rec.get("sample") or "", "cfg_rank": None}
        probe = _probe_tvbox_play(api)
        if not probe:
            return rec["key"], out
        out.update({"bitrate_kbps": probe.get("bitrateKbps") or 0,
                    "resolution": probe.get("resolution") or "",
                    "play": probe.get("play"), "list_ms": probe.get("listMs"),
                    "total": probe.get("total") or 0,
                    "sample": probe.get("sample") or out["sample"]})
        play = probe.get("play") or {}
        if play.get("m3u8") and play.get("url"):
            tp = _measure_throughput(play["url"])
            if tp:
                out.update({"throughput_kbps": tp["kbps"], "throughput_bytes": tp["bytes"],
                            "throughput_secs": tp["secs"]})
        return rec["key"], out

    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(_one, r): r for r in records}
        for fut in as_completed(futs):
            try:
                k, v = fut.result()
            except Exception:  # noqa: BLE001
                # 单站带宽/DoH 补测失败只记该站无数据, 不杀整轮
                # (2026-11-16 循环根因修复)。
                _r = futs[fut]
                k = _r["key"]
                v = {"throughput_kbps": None, "throughput_bytes": None,
                     "throughput_secs": None, "bitrate_kbps": 0, "resolution": "",
                     "play": None, "list_ms": None, "total": 0,
                     "sample": _r.get("sample") or "", "cfg_rank": None}
            extras[k] = v
    hosts = []
    for rec in records:
        h = (rec.get("api_host") or "").split(":")[0]
        if h and h not in hosts:
            hosts.append(h)
    doh = _doh_resolve(hosts, progress=progress) if hosts else {}
    ips = sorted({d.get("ip") for d in doh.values() if d.get("ip")})
    geo = _geo_lookup(ips, progress=progress) if ips else {}
    for rec in records:
        h = (rec.get("api_host") or "").split(":")[0]
        d = doh.get(h) or {}
        e = extras.get(rec["key"], {})
        ip = d.get("ip")
        g = dict(geo.get(ip) or {}) if ip else {}
        if d.get("blocked"):
            g["blockedInCn"] = True
        elif d.get("suspected"):
            g["suspectedBlocked"] = True
        code, bonus = _region_class(g if (g or d.get("blocked") or d.get("suspected")) else None)
        e.update({"cn_reachability": ("blocked" if d.get("blocked") else
                                      "suspected" if d.get("suspected") else
                                      "ok" if d.get("foreignAnswered") else "unknown"),
                  "region": code, "region_bonus": bonus,
                  "region_src": "api",
                  "doh_cn_poisoned": d.get("cnPoisoned", 0),
                  "doh_cn_answered": d.get("cnAnswered", 0),
                  "doh_foreign_answered": d.get("foreignAnswered", 0),
                  "geo_ip": ip, "geo_country": g.get("countryCode"),
                  "geo_isp": g.get("isp"), "geo_as": g.get("as")})
        e["score"] = _score_rec_tvbox(e)
        extras[rec["key"]] = e
    return extras


def _books_for_source(it):
    """该源的测试书: 女频/言情源用女频 5 本, 其余用混合 5 本 (3 男 + 2 女)。
    返回 [{title, author, truthChapters, proofs}]。"""
    books = TEST_BOOKS_FEMALE if it.get("_niche_tag") == "言情/女频" else TEST_BOOKS
    out = []
    for title, author in books:
        ch, proofs = TEST_BOOK_TRUTH.get(title, (None, []))
        out.append({"title": title, "author": author,
                    "truthChapters": ch, "proofs": proofs})
    return out


def _domain_of_source(it):
    """源的注册域 (同站多镜像归一, 用于去重与镜像分组)。"""
    try:
        host = urlparse((it.get("bookSourceUrl") or "").split("##")[0]).hostname or ""
    except Exception:  # noqa: BLE001
        host = ""
    return _registrable_domain(host)


def fetch_books(data_dir, progress=None, extra_sources=None, discovery=None):
    # 书源阶段入口立即打一行日志 (2026-11-16 补): aoaostar 首页抓取本身
    # 最长静默 25 秒 (timeout), 此前 TVBox 一结束、书源第一行日志未出时
    # 看起来像"根本不走书源搜索", 先让入口可见。
    _log(progress, "[书源] 进入书源阶段: 先拉第二合集 aoaostar, 再抓上游")
    _log_mem(progress, "进入书源阶段")
    # extra_sources 为发现阶段找到的新候选上游 (与固定清单走同一套流程)
    sources = list(LEGADO_SOURCES) + list(extra_sources or [])
    # ---- 第二书源合集 legado.aoaostar.com (2026-11-13 并入, 与 yck 并列) ----
    # 每个 bundle 一个上游条目 (urls=[合集 JSON 直链]); 女频专包"关耳女频"
    # (hash e3e5d620, 86 条) 重点拉取, 其源打 _aoa_female 标记, 进女频池优先。
    # 合集入口本身抛异常也只跳过合集, 绝不让整轮死在这里
    # (2026-11-16 循环根因修复)。
    try:
        _aoa_bundles = _fetch_aoa_sources(progress=progress)
    except Exception as e:  # noqa: BLE001
        _log(progress, f"[书源] aoaostar 合集抓取失败, 跳过: {type(e).__name__}: {e}")
        _aoa_bundles = []
    _aoa_by_name = {}
    for _b in _aoa_bundles:
        _entry = {"name": f"aoaostar {_b['name']}", "urls": [_b["url"]],
                  "aoa_bundle": _b["name"], "aoa_hash": _b["hash"],
                  "aoa_female": _b["hash"] == AOA_FEMALE_BUNDLE_HASH}
        sources.append(_entry)
        _aoa_by_name[_entry["name"]] = _entry
    _aoa_total = sum(_b["count"] for _b in _aoa_bundles)
    if _aoa_bundles:
        _log(progress, f"[书源] aoaostar 第二合集 {_aoa_total} 条 "
                       f"({len(_aoa_bundles)} 个 bundle, 女频专包 "
                       f"{next((b['count'] for b in _aoa_bundles if b['hash'] == AOA_FEMALE_BUNDLE_HASH), 0)} 条)")
    _log_mem(progress, "aoaostar 合集拉完")
    out_dir = os.path.join(data_dir, "books")
    os.makedirs(out_dir, exist_ok=True)
    seen_urls = set()
    candidates = []  # 通过基础过滤的全部候选
    per_source = []
    n_invalid = n_non_text = n_adult = 0

    _emit(progress, 6, 0.0, "开始抓取书源", 0, len(sources),
          log=f"[书源] 开始抓取上游 (固定 {len(LEGADO_SOURCES)} 个 + 发现 {len(sources) - len(LEGADO_SOURCES)} 个)")
    for _si, src in enumerate(sources):
        if _deadline_hit():
            _log(progress, "[书源] 超时预算, 停止继续抓上游, 用已抓到的收尾")
            break
        # 2026-11-17 内存防线: 每抓一个上游前查 RSS, 超过 MEM_LIMIT_MB
        # (默认 420MB, 免费版上限 512MB) 即收尾写 partial —— 宁可少抓
        # 几个源, 也不被平台 OOM 杀掉 (重启会清空无持久盘的 data/ 从头再来)。
        _rss_now = _rss_mb()
        if _rss_now is not None and _rss_now >= MEM_LIMIT_MB:
            _log(progress, f"[内存] 书源抓取 RSS {_rss_now} MB 已达上限 "
                           f"{MEM_LIMIT_MB} MB, 停止继续抓上游, 用已抓到的收尾")
            break
        _emit(progress, 6, (_si / max(1, len(sources))) * 0.6,
              f"正在抓 {src['name']}（第 {_si + 1}/{len(sources)} 个书源上游）",
              _si, len(sources))
        _log(progress, f"[书源] 正在抓 {src['name']}")
        text, used, _ct = fetch_text(src["urls"], progress=progress, label=src["name"])
        if text is None:
            per_source.append({"name": src["name"], "ok": False, "used_url": None,
                               "kept": 0, "error": "全部通道失败"})
            continue
        # 2026-11-16: 响应根本不是 JSON (HTML 错误页等) 直接跳过, 不走解析报错。
        if not _json_head_ok(text):
            _fail_count("非JSON")
            per_source.append({"name": src["name"], "ok": False, "used_url": used,
                               "kept": 0, "error": "响应不是 JSON（可能已删库/需登录）"})
            continue
        try:
            data = json.loads(text, strict=False)
        except Exception as e:  # noqa: BLE001
            _fail_count("JSON解析失败")
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
            # aoaostar 合集来源打标记 (curated 优先排序; 女频专包另打 _aoa_female)
            if src.get("aoa_bundle"):
                item["_aoa_bundle"] = src["aoa_bundle"]
                item["_curated"] = True
                if src.get("aoa_female"):
                    item["_aoa_female"] = True
            candidates.append(item)
            kept_here += 1
        per_source.append({"name": src["name"], "ok": True, "used_url": used,
                           "total": len(data), "kept": kept_here})
        _log(progress, f"  OK {src['name']}: 共 {len(data)} 条, 本次新增 {kept_here} 条")
    _atomic_write_json(os.path.join(out_dir, "checkpoint_upstreams.json"),
                       {"per_source": per_source,
                        "candidates_after_basic_filter": len(candidates)})
    _log(progress, f"[书源] 基础过滤后候选 {len(candidates)} 个, 开始规则/登录墙筛选")
    _emit(progress, 6, 0.7, f"规则/登录墙筛选：候选 {len(candidates)} 个",
          None, None)

    # ---- 规则齐全 + 登录/付费墙筛选 (2026-11-13 并入他方 completeness 方法) ----
    # 入池门槛: 三大规则齐全 + 有搜索入口 (searchUrl 或 exploreUrl) +
    # 11 字段齐全度 >= 8。注意未照搬对方"最多缺 1 个 (>=10)" 的线: 实测当前
    # 150 个在用源里 ruleBookInfo.tocUrl 缺 71 个、name/author 各缺约 30 个,
    # 但这些字段缺失并不影响使用 (校验时 tocUrl 默认取 bookUrl, 详情名/作者
    # 取搜索结果), >=10 会误删 32/150 个正常源。>=8 只剔字段残缺最多的源。
    n_rule_incomplete = n_no_search = n_login_paywall = 0
    usable = []
    for item in candidates:
        if not item.get("ruleSearch") or not item.get("ruleContent") or not item.get("ruleToc"):
            n_rule_incomplete += 1
            continue
        if not _has_search_entry(item):
            n_no_search += 1
            continue
        if _completeness(item) < 8:
            n_rule_incomplete += 1
            continue
        hay = f"{item.get('bookSourceComment','')} {item.get('bookSourceGroup','')} {item.get('bookSourceName','')}"
        if any(k in hay for k in LOGIN_PAYWALL_KEYWORDS):
            n_login_paywall += 1
            continue
        usable.append(item)
    _log(progress, f"[书源] 规则齐全且无登录墙 {len(usable)} 个 (剔除规则不全 {n_rule_incomplete} / 无搜索 {n_no_search} / 登录付费墙 {n_login_paywall})")

    # ---- 头部池: 70% 热门 + 30% 偏门, 各按齐全度排序, 共 400 个 ----
    # 2026-11-13 女频缺口修正: 头部池另留 40 个女频/言情名额 (aoa 女频专包
    # 优先), 否则按下载量/齐全度排序会把女频源埋没 (对方报告明确写了这一点)。
    for it in usable:
        it["_completeness"] = _completeness(it)
        cat, tag = classify_book(it)
        it["_category"] = cat
        it["_niche_tag"] = tag
    usable.sort(key=lambda x: -x["_completeness"])
    popular_pool = [x for x in usable if x["_category"] == "热门"]
    niche_pool = [x for x in usable if x["_category"] == "偏门"]
    female_pool = sorted(
        [x for x in usable if x["_niche_tag"] == "言情/女频"],
        key=lambda x: (not x.get("_aoa_female"), -x["_completeness"]))
    head_pool = popular_pool[:260] + niche_pool[:100] + female_pool[:40]
    _seen_hp, _hp = set(), []
    for x in head_pool:
        if x["bookSourceUrl"] not in _seen_hp:
            _seen_hp.add(x["bookSourceUrl"])
            _hp.append(x)
    head_pool = _hp
    _emit(progress, 6, 0.85, f"头部抽查池 {len(head_pool)} 个（热门/偏门），开始官网检查",
          None, None)
    _log(progress, f"[书源] 头部抽查池 {len(head_pool)} 个 (热门 {min(280,len(popular_pool))} / 偏门 {min(120,len(niche_pool))})")

    # ---- 官网可打开检查 (10 并发) ----
    _log(progress, "[书源] 检查头部池官网是否可打开 (10 并发, 超时 5s)")
    reachable = {}
    _emit(progress, 7, 0.0, f"官网检查开始，共 {len(head_pool)} 个", 0, len(head_pool))
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(_check_url_reachable, it["bookSourceUrl"]): it for it in head_pool}
        _done_r = 0
        _ok_r = 0
        for fut in as_completed(futs):
            it = futs[fut]
            try:
                _ok = fut.result()
            except Exception:  # noqa: BLE001
                _ok = False
            reachable[it["bookSourceUrl"]] = _ok
            _done_r += 1
            if _ok:
                _ok_r += 1
            if _done_r % 10 == 0 or _done_r == len(head_pool):
                _emit(progress, 7, (_done_r / max(1, len(head_pool))) * 0.2,
                      f"官网检查 {_done_r}/{len(head_pool)}，可打开 {_ok_r} 个",
                      _done_r, len(head_pool))
    n_unreachable = sum(1 for it in head_pool if not reachable.get(it["bookSourceUrl"]))
    _log(progress, f"[书源] 官网可打开 {len(head_pool)-n_unreachable}/{len(head_pool)}, 打不开 {n_unreachable} 个")

    # ---- 四步校验: 取头部池前 200 个 (热门 120 + 偏门 40 + 女频 40), 每源 5 本书 ----
    # 2026-11-13 并入他方四步校验: Node 驱动 (js/validate_books.js) 跑
    # 搜索->详情->目录->正文全流程, @js: 规则进沙箱执行、POST 按配置发,
    # 正文含主角名/书名才算通过 (搜得到但看不成判内容不符)。
    # 三态: True 通过 / False 失败 / None 跳过 (反爬/HTTP异常/规则问题, 不判 0 分)。
    # search_pass 为 None 表示 5 本全跳过。Node 不可用时回退到旧 Python 搜索抽查。
    head_sorted = sorted(head_pool, key=lambda x: (not reachable.get(x["bookSourceUrl"]), -x["_completeness"]))
    test_pop = [x for x in head_sorted if x["_category"] == "热门"][:120]
    test_niche = [x for x in head_sorted if x["_category"] == "偏门"][:40]
    test_female = [x for x in head_sorted if x["_niche_tag"] == "言情/女频"][:40]
    test_pool = test_pop + test_niche + test_female
    # 去重: 先按 URL, 再按注册域 (同站多镜像只留排名最高的 1 个进实测池;
    # 其余镜像作为该域的备用镜像进 Node 驱动)。
    _seen_u, _tp = set(), []
    for x in test_pool:
        if x["bookSourceUrl"] not in _seen_u:
            _seen_u.add(x["bookSourceUrl"])
            _tp.append(x)
    test_pool = _tp
    _seen_d, _tp2 = set(), []
    for x in test_pool:
        d = _domain_of_source(x)
        if d and d in _seen_d:
            continue
        _seen_d.add(d)
        _tp2.append(x)
    test_pool = _tp2[:NOVEL_VALIDATE_MAX]
    _log(progress, f"[书源] 四步校验 {len(test_pool)} 个源 × 5 本书 "
                   f"(热门 {sum(1 for x in test_pool if x['_category'] == '热门')} / "
                   f"偏门 {sum(1 for x in test_pool if x['_category'] == '偏门')} / "
                   f"女频 {sum(1 for x in test_pool if x['_niche_tag'] == '言情/女频')})")
    _post_blocked = _is_post_blocked_env()
    search_pass = {}  # url -> {"pass": int|None, "tested": int, "skipped": int, "details": {title: True|False|None}, "reasons": {title: str}, "content_bad": int, ...}
    _node_stats = {}
    _node_error = ""
    _validate_via = "node4step"

    def _build_node_domains(pool, books_override=None):
        # 按注册域分组; 镜像从 head_pool 补 (同域、searchUrl 优先, 最多 3 个)。
        by_domain = {}
        for it in pool:
            d = _domain_of_source(it) or it["bookSourceUrl"]
            by_domain.setdefault(d, {"domain": d, "primary": it, "mirrors": [it]})
        for it in head_pool:
            d = _domain_of_source(it) or it["bookSourceUrl"]
            if d in by_domain and it not in by_domain[d]["mirrors"]:
                by_domain[d]["mirrors"].append(it)
        domains = []
        for d, g in by_domain.items():
            mirrors = sorted(g["mirrors"],
                             key=lambda x: (not bool(x.get("searchUrl")), -x["_completeness"]))[:3]
            books = None
            if books_override:
                books = books_override.get(g["primary"]["bookSourceUrl"])
            if books is None:
                books = _books_for_source(g["primary"])
            domains.append({"domain": d, "mirrors": mirrors, "books": books})
        return domains

    def _apply_node_results(results):
        for r in results or []:
            prim = next((it for it in test_pool
                         if _domain_of_source(it) == r.get("domain")), None)
            if prim is None:
                continue
            url = prim["bookSourceUrl"]
            d = search_pass.setdefault(url, {"pass": 0, "tested": 0, "skipped": 0,
                                             "details": {}, "reasons": {},
                                             "content_bad": 0, "toc_lagging": 0,
                                             "chapter_ratios": {},
                                             "test_books_used": [b["title"] for b in _books_for_source(prim)],
                                             "via": "node4step",
                                             "domain": r.get("domain", ""),
                                             "representative": r.get("representativeUrl", "")})
            for title, pb in (r.get("perBook") or {}).items():
                oc = pb.get("outcome")
                d["details"][title] = True if oc == "pass" else (False if oc == "fail" else None)
                d["reasons"][title] = pb.get("reason") or ("pass" if oc == "pass" else "")
                if oc == "pass":
                    d["pass"] += 1
                    d["tested"] += 1
                elif oc == "fail":
                    d["tested"] += 1
                else:
                    d["skipped"] += 1
                if pb.get("tocLagging"):
                    d["toc_lagging"] += 1
                if pb.get("chapterRatio") is not None:
                    d["chapter_ratios"][title] = pb.get("chapterRatio")
            d["content_bad"] = r.get("contentBad", 0)
            if d["tested"] == 0:
                d["pass"] = None

    # (4) 先跑 Node 沙箱四步校验 (有 Node 时); 不可用则 Python 回退
    # Node 校验入口本身抛异常 (如 _build_node_domains 构造失败) 也只记
    # error 走 Python 回退, 绝不杀整轮 (2026-11-16 循环根因修复)。
    try:
        _node_domains = _build_node_domains(test_pool)
        _node_deadline_s = 1500  # Node 四步校验预算 25 分钟 (总预算 60 分钟内)
        _node_results, _node_stats, _node_error = _run_node_validation(
            _node_domains, deadline_s=_node_deadline_s, concurrency=24, progress=progress)
    except Exception as e:  # noqa: BLE001
        _node_results, _node_stats = None, {}
        _node_error = f"internal_{type(e).__name__}: {e}"
    if _node_results is not None:
        _apply_node_results(_node_results)
        # 代表源替换: 若某域胜出的是备用镜像, 实测池里用胜出的镜像对象替换 primary
        # (否则最终 150 会收一个实测时已死的 primary)。
        _hp_by_url = {it["bookSourceUrl"]: it for it in head_pool}
        _swap = {}
        for r in _node_results:
            rep = r.get("representativeUrl") or ""
            prim = next((it for it in test_pool
                         if _domain_of_source(it) == r.get("domain")), None)
            if prim is not None and rep and rep != prim["bookSourceUrl"] \
                    and rep in _hp_by_url:
                _swap[prim["bookSourceUrl"]] = rep
        if _swap:
            test_pool = [_hp_by_url.get(_swap.get(it["bookSourceUrl"]), it)
                         for it in test_pool]
            for old_url, rep_url in _swap.items():
                if old_url in search_pass and rep_url not in search_pass:
                    search_pass[rep_url] = search_pass.pop(old_url)
            _log(progress, f"[书源] 代表源替换 {len(_swap)} 个域 (备用镜像胜出)")
        _log(progress, f"[书源] Node 四步校验完成: {_node_stats}")
    else:
        _validate_via = "python-fallback"
        _log(progress, f"[书源] Node 四步校验不可用 ({_node_error}), 回退 Python 搜索抽查 "
                       f"(超时 {BOOK_SEARCH_TIMEOUT}s, @js:/POST 源将标记未测试)")
        _log(progress, f"[书源] POST 环境检测: {'本机出口拦截 POST, POST 源将标记为未测试' if _post_blocked else 'POST 正常, 可实测 POST 源'}")
        _emit(progress, 7, 0.2, f"搜书抽查开始：{len(test_pool)} 个源 × 5 本书",
              0, len(test_pool))
        with ThreadPoolExecutor(max_workers=8) as ex:
            fut_map = {}
            for it in test_pool:
                for _b in _books_for_source(it):
                    fut = ex.submit(_search_test_one_detailed, it, _b["title"], _b["author"])
                    fut_map[fut] = (it["bookSourceUrl"], _b["title"])
            _res_n = 0
            _src_done = 0
            for fut in as_completed(fut_map):
                url, title = fut_map[fut]
                try:
                    ok, reason = fut.result()
                except Exception as e:  # noqa: BLE001
                    # 单本书抽查抛异常只记该本跳过, 不杀整轮
                    # (2026-11-16 循环根因修复: 旧版这里直接抛回主线程)。
                    ok, reason = None, f"skipped:internal_{type(e).__name__}:{str(e)[:100]}"
                d = search_pass.setdefault(url, {"pass": 0, "tested": 0, "skipped": 0,
                                                "details": {}, "reasons": {},
                                                "content_bad": 0, "toc_lagging": 0,
                                                "chapter_ratios": {},
                                                "test_books_used": [],
                                                "via": "python-fallback"})
                d["details"][title] = ok
                d["reasons"][title] = reason
                if ok is None:
                    d["skipped"] += 1
                else:
                    d["tested"] += 1
                    if ok:
                        d["pass"] += 1
                _res_n += 1
                if _deadline_hit():
                    _log(progress, "[书源] 搜书抽查超时预算, 剩余用已测完的收尾")
                    ex.shutdown(wait=False, cancel_futures=True)
                    break
                if _res_n % 60 == 0 or _res_n == len(fut_map):
                    _src_done = sum(1 for v in search_pass.values() if len(v["details"]) == 5)
                    _ge3 = sum(1 for v in search_pass.values()
                               if v["tested"] > 0 and v["pass"] >= SEARCH_PASS_MIN)
                    _emit(progress, 7, 0.2 + (_src_done / max(1, len(test_pool))) * 0.7,
                          f"搜书抽查 {_src_done}/{len(test_pool)} 源，{SEARCH_PASS_MIN}/5 以上通过 {_ge3} 个",
                          _src_done, len(test_pool))
        for v in search_pass.values():
            if v["tested"] == 0:
                v["pass"] = None
    for v in search_pass.values():
        if v["tested"] == 0:
            v["pass"] = None
    # 分布只统计至少测了 1 本的源; 全跳过的源单独计 skipped, 不进 0-3 分布
    dist = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
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
    _log(progress, f"[书源] 校验分布 (已测 {len(test_pool)-n_skipped_sources} 源): "
                   f"5/5 通过 {dist[5]} / 4/5 通过 {dist[4]} / 3/5 通过 {dist[3]} / "
                   f"2/5 通过 {dist[2]} / 1/5 通过 {dist[1]} / 0/5 通过 {dist[0]} / "
                   f"全跳过 {n_skipped_sources} 源 (部分跳过 {n_partial_skipped} 源)")
    _atomic_write_json(os.path.join(out_dir, "checkpoint_search.json"),
                       {"distribution": dist, "tested": len(test_pool) - n_skipped_sources,
                        "skipped_sources": n_skipped_sources,
                        "validate_via": _validate_via, "node_stats": _node_stats})

    # ---- 第二轮复测: 3/5 或 4/5 通过的源, 把没通过的本再测一次, 确认是否偶发失败 ----
    # 只复测 tested==5 且 pass 在 3-4 的源, 跳过的项不复测 (复测也测不了)。
    # Node 路径: 对复测目标再建一个小 job (只含失败的本); Python 回退路径: 旧逻辑。
    retry_targets = []
    for it in test_pool:
        sp = search_pass.get(it["bookSourceUrl"], {"pass": 0, "tested": 0, "details": {}})
        if sp.get("pass") in (3, 4) and sp.get("tested") == 5:
            for _b in _books_for_source(it):
                if sp["details"].get(_b["title"]) is False:
                    retry_targets.append((it, _b["title"], _b["author"]))
    if retry_targets:
        _emit(progress, 7, 0.9, f"复测开始：{len(retry_targets)} 次（3/5 或 4/5 通过源的失败项）",
              0, len(retry_targets),
              log=f"[书源] 第二轮复测 {len(retry_targets)} 次 (3/5 或 4/5 通过源的失败项)")
        if _validate_via == "node4step":
            _retry_by_url = {}
            for it, t, a in retry_targets:
                _retry_by_url.setdefault(it["bookSourceUrl"], []).append(
                    {"title": t, "author": a,
                     "truthChapters": TEST_BOOK_TRUTH.get(t, (None, []))[0],
                     "proofs": TEST_BOOK_TRUTH.get(t, (None, []))[1]})
            _retry_pool = [it for it in test_pool
                           if it["bookSourceUrl"] in _retry_by_url]
            try:
                _retry_domains = _build_node_domains(_retry_pool, books_override=_retry_by_url)
                _r_results, _r_stats, _r_err = _run_node_validation(
                    _retry_domains, deadline_s=600, concurrency=12, progress=progress)
            except Exception as e:  # noqa: BLE001
                _r_results, _r_err = None, f"internal_{type(e).__name__}: {e}"
            if _r_results is not None:
                _done_rt = 0
                for r in _r_results:
                    prim = next((it for it in test_pool
                                 if _domain_of_source(it) == r.get("domain")), None)
                    if prim is None:
                        continue
                    url = prim["bookSourceUrl"]
                    sp = search_pass.get(url)
                    if not sp:
                        continue
                    for title, pb in (r.get("perBook") or {}).items():
                        if pb.get("outcome") == "pass" and sp["details"].get(title) is False:
                            sp["details"][title] = True
                            sp["reasons"][title] = "pass(retry)"
                            sp["pass"] += 1
                        _done_rt += 1
                _log(progress, f"[书源] Node 复测完成: {_r_stats}")
            else:
                _log(progress, f"[书源] Node 复测失败 ({_r_err}), 跳过复测")
        else:
            with ThreadPoolExecutor(max_workers=8) as ex:
                fut_map = {ex.submit(_search_test_one_detailed, it, t, a): (it["bookSourceUrl"], t)
                           for it, t, a in retry_targets}
                _done_rt = 0
                for fut in as_completed(fut_map):
                    url, title = fut_map[fut]
                    try:
                        ok, reason = fut.result()
                    except Exception:  # noqa: BLE001
                        continue  # 单本复测抛异常只跳过该本, 不杀整轮
                    if ok is True:
                        search_pass[url]["details"][title] = True
                        search_pass[url]["reasons"][title] = reason
                        search_pass[url]["pass"] += 1
                    _done_rt += 1
                    _emit(progress, 7, 0.9 + (_done_rt / max(1, len(retry_targets))) * 0.1,
                          f"复测 {_done_rt}/{len(retry_targets)}", _done_rt, len(retry_targets))
    else:
        _emit(progress, 7, 1.0, "无需复测，搜书抽查完成", len(test_pool), len(test_pool))
        dist2 = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        for v in search_pass.values():
            if v["tested"] > 0:
                dist2[v["pass"]] += 1
        _log(progress, f"[书源] 复测后分布: 5/5 通过 {dist2[5]} / 4/5 通过 {dist2[4]} / 3/5 通过 {dist2[3]} / 2/5 通过 {dist2[2]} / 1/5 通过 {dist2[1]} / 0/5 通过 {dist2[0]}")
        dist = dist2

    # 复测后统一重算分布 (复测可能把 3/5 抬到 4/5, 不能用复测前的分布)
    dist = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for v in search_pass.values():
        if v["tested"] > 0:
            dist[v["pass"]] += 1

    # ---- 最终选取: 搜索通过率 > 齐全度 > 官网可打开, 105 热门 + 45 偏门 = 150 ----
    # 从严 (2026-11-13): 只收 pass>=3/5 或未测试 (pass=None, 中性) 的源;
    # 0-2/5 的源不收, 不足 150 也不放宽, 数量如实记
    def _book_rank(it):
        sp = search_pass.get(it["bookSourceUrl"], {"pass": None})
        eff = sp["pass"] if sp["pass"] is not None else 1
        return (-eff, -it["_completeness"], not reachable.get(it["bookSourceUrl"], False))
    def _book_ok_for_keep(it):
        sp = search_pass.get(it["bookSourceUrl"], {"pass": None})
        pp = sp["pass"]
        # 2026-11-13 女频缺口修正: 四步校验里正文系统性不符的源不进最终 150
        # (content_bad>=2: 至少两本书正文太短/占位/非散文/内容不符/截断,
        # 即"搜得到但看不成"; content_bad==1 可能是单本书问题, 只记信号不淘汰)。
        if sp.get("content_bad", 0) >= 2:
            return False
        return pp is None or pp >= SEARCH_PASS_MIN
    test_pool_sorted = sorted(test_pool, key=_book_rank)
    pop_final = [x for x in test_pool_sorted
                 if x["_category"] == "热门" and _book_ok_for_keep(x)][:105]
    niche_final = [x for x in test_pool_sorted
                   if x["_category"] == "偏门" and _book_ok_for_keep(x)][:45]
    kept = pop_final + niche_final
    # 不足 150 时, 从头部池其余源里按同样标准 (pass>=3 或未测试) 补, 不够就少收;
    # 补录必须分 category (先偏门补到 45, 再热门补到 105, 最后按排名补满 150),
    # 否则齐全度排序会把偏门挤没 (2026-11-13 全量实测曾出现热门 139 / 偏门 11)
    if len(kept) < MAX_BOOK_SOURCES:
        _rest = [x for x in head_pool if x not in kept and _book_ok_for_keep(x)]
        _rest.sort(key=_book_rank)
        _need_niche = 45 - sum(1 for x in kept if x["_category"] == "偏门")
        if _need_niche > 0:
            _add_n = [x for x in _rest if x["_category"] == "偏门"][:_need_niche]
            kept += _add_n
            _rest = [x for x in _rest if x not in _add_n]
        _need_pop = 105 - sum(1 for x in kept if x["_category"] == "热门")
        if _need_pop > 0:
            _add_p = [x for x in _rest if x["_category"] == "热门"][:_need_pop]
            kept += _add_p
            _rest = [x for x in _rest if x not in _add_p]
        kept += _rest[:MAX_BOOK_SOURCES - len(kept)]
    # ---- 女频/言情配额 (2026-11-13 用户要求): 最终 150 里女频/言情源不低于 30 个 ----
    # 从头部池按同样标准 (pass>=3 或未测试、content_bad<2) 补, 替换排名最低的
    # 非女频源 (总数保持 150 不变); 同域已在 kept 里的不重复收。
    _female_kept_n = sum(1 for x in kept if x.get("_niche_tag") == "言情/女频")
    _female_quota_note = ""
    if _female_kept_n < FEMALE_MIN_COUNT:
        _kept_domains = {_domain_of_source(x) for x in kept}
        _female_cands = [x for x in head_pool
                         if x.get("_niche_tag") == "言情/女频"]
        _female_cands = [x for x in _female_cands
                         if x not in kept and _book_ok_for_keep(x)
                         and _domain_of_source(x) not in _kept_domains]
        _female_cands.sort(key=_book_rank)
        _need_f = FEMALE_MIN_COUNT - _female_kept_n
        _add_f = _female_cands[:_need_f]
        if _add_f:
            _non_f = [x for x in kept if x.get("_niche_tag") != "言情/女频"]
            _non_f.sort(key=_book_rank)
            _drop = _non_f[-len(_add_f):] if len(_non_f) >= len(_add_f) else _non_f
            kept = [x for x in kept if x not in _drop] + _add_f
            _female_quota_note = (f"女频配额: 原 {_female_kept_n} 个不足 {FEMALE_MIN_COUNT} 个, "
                                  f"补 {_add_f and len(_add_f)} 个、替换 {len(_drop)} 个非女频源。")
            _log(progress, f"[书源] {_female_quota_note}")
        else:
            _female_quota_note = (f"女频配额: 原 {_female_kept_n} 个不足 {FEMALE_MIN_COUNT} 个, "
                                  f"头部池无更多符合标准的女频源可补, 如实缺收。")
            _log(progress, f"[书源] {_female_quota_note}")
    # 正文系统性不符拦掉数: 须在 threshold_note 引用前算好 (search_pass 在校验段已建好,
    # 空池时为 {}、结果为 0; 原先写在 4147 行、threshold_note 在 4109 行引用, 空池
    # 或 node 路径下会报 UnboundLocalError, 2026-11-13 全量跑因此崩溃)。
    _content_bad_excluded = sum(
        1 for v in search_pass.values() if v.get("content_bad", 0) >= 2)
    threshold_note = ""
    ge3 = dist[3] + dist[4] + dist[5]
    ge4 = dist[4] + dist[5]
    if _post_blocked:
        threshold_note = (f"本机出口拦截 POST (httpbin.org/post 也超时, GET 正常), "
                          f"POST 源无法在本机实测, {n_skipped_sources} 个源 5 本全跳过、"
                          f"{n_partial_skipped} 个源部分跳过, 均标记为 search_pass=null、未计入 0/5, "
                          f"不要把它们当成源本身坏; 需在能 POST 的正常环境 (Render/VPS) 重跑 fetcher.py "
                          f"才能得到 POST 源的真实通过率。")
    if ge3 < MAX_BOOK_SOURCES:
        threshold_note += (f"搜索 3/5 以上通过的 (已测源中) 只有 {ge3} 个 (4/5 以上 {ge4} 个), "
                           f"不足 {MAX_BOOK_SOURCES}, 已按从严标准只收 {len(kept)} 个, "
                           "0-2/5 的源不再补录; 详见 kept_sources 的 search_pass/tested/skipped 字段; "
                           "未测试源 (search_pass=null, 如 POST 环境拦截) 按中性保留, 请导入后自行搜书验证, "
                           "不要只看数量。0/5 通过不一定等于源本身坏: 部分站是 GBK 编码/反爬/小站无此书 "
                           "(如笔趣阁 search.php 返回'搜索没有结果'是真无结果, 999wx 404 是真失效), "
                           "抽查方法见 README'筛选标准'。")
    if _validate_via == "node4step":
        threshold_note += (f"本轮用 Node 四步校验 (搜索->详情->目录->正文, @js: 进沙箱、POST 按配置发); "
                           f"正文系统性不符 (content_bad>=2) 拦掉 {_content_bad_excluded} 个源, 不进最终 150; "
                           "章节比 <0.8 只记信号不淘汰; 女频源用女频题库 (5 本全女频, 同样过 3 本)。")
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
            "content_bad": sp.get("content_bad", 0),
            "toc_lagging": sp.get("toc_lagging", 0),
            "chapter_ratios": sp.get("chapter_ratios", {}),
            "test_books_used": sp.get("test_books_used", []),
            "validate_via": sp.get("via", _validate_via),
            "aoa_bundle": it.get("_aoa_bundle", ""),
            "completeness": it["_completeness"],
            "url_reachable": reachable.get(it["bookSourceUrl"]),
        })
    niche_kept = [r for r in kept_records if r["category"] == "偏门"]
    # 女频可读验收 (用户 2026-11-13 验收标准): 最终 150 里有几个源能正常看
    # 《缠珑》这类晋江/潇湘女频书 = 该书四步全过 (含正文检查) 的源数, 如实统计。
    _female_readable = {}
    for _title, _author in TEST_BOOKS_FEMALE:
        _female_readable[_title] = sum(
            1 for r in kept_records
            if r.get("search_details", {}).get(_title) is True)
    _female_kept_count = sum(1 for r in kept_records if r["niche_tag"] == "言情/女频")

    _emit(progress, 8, 0.0, f"写文件：正在写 books.json（{len(out_items)} 个书源）",
          None, None)
    with open(os.path.join(out_dir, "books.json"), "w", encoding="utf-8") as f:
        json.dump(out_items, f, ensure_ascii=False, indent=2)
    _psb_by_name = {q.get("name"): q for q in per_source}
    _discovered_book_entries = []
    for _src in (extra_sources or []):
        _q = _psb_by_name.get(_src.get("name")) or {}
        _discovered_book_entries.append({
            "name": _src.get("name"),
            "discovery_source": _src.get("discovery_source"),
            "ok": bool(_q.get("ok")),
            "kept": _q.get("kept", 0),
            "passed_screening": bool(_q.get("ok") and _q.get("kept", 0) > 0),
        })
    meta = {
        "generated_at": now_iso(),
        "book_count": len(out_items),
        "target": f"{MIN_BOOK_SOURCES}-{MAX_BOOK_SOURCES} (目标 {MAX_BOOK_SOURCES}, 约 70% 热门 + 30% 偏门)",
        "popular_count": sum(1 for r in kept_records if r["category"] == "热门"),
        "niche_count": sum(1 for r in kept_records if r["category"] == "偏门"),
        # 女频/言情验收 (2026-11-13 用户要求: 最终 150 里不低于 30 个)
        "female_count": _female_kept_count,
        "female_min_required": FEMALE_MIN_COUNT,
        "female_quota_note": _female_quota_note,
        # 每本女频书四步全过 (含正文检查) 的源数, 如实统计, 不报虚数。
        # 《缠珑》为用户点名的验收书: 能正常看的源数见 chanlong_readable。
        "female_books_readable": _female_readable,
        "chanlong_readable": _female_readable.get("缠珑", 0),
        # 正文系统性不符被拦掉的源数 (content_bad>=2, 不进最终 150)
        "content_bad_excluded": _content_bad_excluded,
        # 校验方式: node4step (Node 四步校验) 或 python-fallback (旧搜索抽查)
        "validate_via": _validate_via,
        "node_stats": _node_stats,
        "node_error": _node_error,
        "aoaostar": {"bundles": [{"name": b["name"], "hash": b["hash"],
                                  "count": b["count"], "error": b.get("error", "")}
                                 for b in _aoa_bundles],
                     "total": _aoa_total,
                     "female_bundle": "关耳女频",
                     "female_bundle_hash": AOA_FEMALE_BUNDLE_HASH},
        "total_candidates_after_basic_filter": len(candidates),
        "usable_after_rule_filter": len(usable),
        "head_pool_size": len(head_pool),
        "search_tested": len(test_pool),
        "search_coverage": f"{len(test_pool)}/{len(candidates)} 候选源做了 5 本书搜索抽查 (头部池), 其余未逐个搜索",
        "search_pass_bar": f">={SEARCH_PASS_MIN}/5",
        "search_pass_distribution": dist,
        "search_pass_distribution_note": "分布只统计至少测了 1 本的源; 全跳过的源不进 0-5 分布, 见 search_skipped_sources",
        "search_pass_ge3": ge3,
        "search_pass_ge4": ge4,
        "strict_no_relax": True,
        "discovery": {k: v for k, v in (discovery or {}).items()
                      if k in ("enabled", "skipped_reason", "elapsed_s",
                               "github_rate_limited")},
        "discovered_upstreams": _discovered_book_entries,
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
        "test_books_note": ("混合题库 (一般源): 3 本男频 + 2 本女频 "
                            "(斗破苍穹/凡人修仙传/庆余年 + 缠珑/知否知否应是绿肥红瘦), "
                            "5 本过 3 本即通过; 女频/言情源用女频题库 (见 test_books_female), "
                            "同样 5 本过 3 本; 每源用的题库见 kept_sources 的 test_books_used。"),
        "test_books_female": [t for t, _ in TEST_BOOKS_FEMALE],
        "test_books_truth_note": ("真值章节数: 《缠珑》57 章取自他方项目 test-books.json "
                                  "(晋江抓取快照); 其余为公开常见数值 (约数, 仅作 0.8 章节比值 "
                                  "的补充信号, toc-lagging 不淘汰)。"),
        "adult_keywords": ADULT_KEYWORDS,
        "sources": per_source,
        "kept_sources": kept_records,
        "niche_kept": niche_kept,
        "notes": [
            "只保留 bookSourceType==0 的文字小说源; 受限内容/短剧/有声/漫画按关键词表过滤, 单字'黄'不用作过滤词以免误删正常小说。",
            "大陆速度未实测, 书籍全不全无法仅凭清单判断, 导入后请自行搜热门书、打开一章验证。",
            "搜索抽查只覆盖头部池, 未覆盖的候选源未逐个搜索, 覆盖率见 search_coverage。",
            "修复 C: searchUrl 尾部 @js: 先剥掉再解析 (否则 POST 配置解析失败被误判成 GET); 开头 {{cookie...}} 剥掉后解析剩余 URL, 开头 {{url=...}} 跳过不判 0 分; POST 源若因本机出口拦截 POST 而无法实测, 标记为 search_pass=null, 不要当成源本身坏, 需在正常环境重测。0/5 源不删除, 如实保留。",
        ],
    }
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    _emit(progress, 8, 0.5, "写文件：books.json / meta.json 已写",
          None, None)
    _log(progress, f"[书源] 完成: {len(out_items)} 个书源 (热门 {meta['popular_count']} / 偏门 {meta['niche_count']}, 搜索 3/5 以上通过 {meta['search_pass_ge3']} 个, 全跳过 {n_skipped_sources} 个)")
    return meta


# ---------------------------------------------------------------- 总入口
def fetch_all(data_dir, progress=None, interval_days=7):
    _LAST_PERCENT[0] = 0.0  # 新一轮抓取, percent 从 0 重新开始
    _LAST_STAGE_IDX[0] = 0  # 阶段切换内存日志从头计
    _fail_reset()  # 新一轮失败统计从头计
    _dead_hosts_init(data_dir)  # 读死域名缓存 (上一轮的 24 小时跳过继续有效)
    _ensure_writable_dir(data_dir)
    # 总预算 (默认 60 分钟, FETCH_BUDGET_MINUTES 可改): 从这里起算, 各阶段循环里
    # 检查, 超时即收尾。已抓到的数据靠阶段检查点落盘, 不会丢失。
    _DEADLINE[0] = time.time() + FETCH_BUDGET_MINUTES * 60
    _log(progress, f"[总预算] 本轮抓取总预算 {FETCH_BUDGET_MINUTES} 分钟, 超时即收尾 "
                   "(已抓到的数据已落盘, 不会丢失)")
    _log_mem(progress, "本轮开始")
    # ---- 循环根因修复 (2026-11-16) ----
    # 旧版 fetch_all 全程没有 try/except: 任一阶段 (发现/TVBox/书源) 抛异常,
    # 都会跳过 status.json 的写出, 于是 _is_expired() 永远为真, 调度器每小时
    # 重开一轮, 每轮又在同一处抛掉 —— 日志上就是"一直在抓", 像个死循环,
    # 而实际上每一轮都在书源阶段 (通常是 _search_test_one_detailed /
    # _parse_search_request_full 的异常经 fut.result() 抛回主线程) 提前死掉。
    # 现在每个阶段单独包住: 失败只记 error, 其余阶段照常跑; 收尾一定写
    # status.json (last_update=now, 附 error/partial), 调度器 7 天内不再重开
    # 失败的轮次 (失败后想重试请点手动更新); 前端据此显示失败阶段与原因,
    # 不再只显示"正在抓取"。
    errors = {}
    discovery = {}
    try:
        # 第 0 阶段: 自动发现新源 (GitHub 多关键词多页 + 免费搜索引擎 + yck 聚合索引)
        discovery = discover_upstreams(progress=progress) or {}
    except Exception as e:  # noqa: BLE001
        errors["discovery"] = f"{type(e).__name__}: {e}"
        _log(progress, f"[发现] 阶段失败, 跳过新源发现: {errors['discovery']}")
        discovery = {}
    _log_mem(progress, "发现阶段结束")
    tv_meta = {"site_count": 0}
    try:
        tv_meta = fetch_tvbox(data_dir, progress=progress,
                              extra_sources=discovery.get("tvbox"), discovery=discovery) or {"site_count": 0}
    except Exception as e:  # noqa: BLE001
        errors["tvbox"] = f"{type(e).__name__}: {e}"
        _log(progress, f"[TVBox] 阶段失败: {errors['tvbox']}")
        tv_meta = {"site_count": 0}
    _log_mem(progress, "TVBox 阶段结束")
    books_meta = {"book_count": 0}
    try:
        books_meta = fetch_books(data_dir, progress=progress,
                                 extra_sources=discovery.get("books"), discovery=discovery) or {"book_count": 0}
    except Exception as e:  # noqa: BLE001
        errors["books"] = f"{type(e).__name__}: {e}"
        _log(progress, f"[书源] 阶段失败: {errors['books']}")
        books_meta = {"book_count": 0}
    _log_mem(progress, "书源阶段结束")
    # ---- 收尾: 无论哪阶段失败都写 status.json ----
    _emit(progress, 8, 0.8, "写文件：正在写 status.json", None, None)
    status = {
        "last_update": now_iso(),
        "next_update": (datetime.now(timezone.utc) + timedelta(days=interval_days)).isoformat().replace("+00:00", "Z"),
        "interval_days": interval_days,
        "tvbox_sites": tv_meta.get("site_count", 0),
        "book_sources": books_meta.get("book_count", 0),
    }
    if errors:
        status["error"] = errors
        status["partial"] = True
    try:
        with open(os.path.join(data_dir, "status.json"), "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        _log(progress, f"[完成] status.json 写出失败: {type(e).__name__}: {e}")
    if errors:
        _fail_txt = "; ".join(f"{k}: {v}" for k, v in errors.items())
        _emit(progress, 8, 1.0,
              f"抓取完成（部分失败：{', '.join(errors)}）: TVBox {tv_meta.get('site_count', 0)} 站 / 书源 {books_meta.get('book_count', 0)} 个",
              None, None,
              log=f"[完成] 部分失败: {_fail_txt}")
    else:
        _emit(progress, 8, 1.0,
              f"抓取完成：TVBox {tv_meta.get('site_count', 0)} 站 / 书源 {books_meta.get('book_count', 0)} 个",
              None, None,
              log=f"[完成] TVBox {tv_meta.get('site_count', 0)} 站 / 书源 {books_meta.get('book_count', 0)} 个")
    _log(progress, _fail_summary_text())
    try:
        _dead_hosts_save(force=True)  # 死域名缓存落盘, 下一轮继续有效
    except Exception:  # noqa: BLE001
        pass
    _log_mem(progress, "本轮结束")
    return tv_meta, books_meta


if __name__ == "__main__":
    # Vercel 上默认写 /tmp (只读文件系统, 项目内 data/ 写不进去);
    # 本地/Render/VPS 保持默认 data。显式传参或设 DATA_DIR 时优先用指定的。
    _default_dir = "/tmp/tvbox-legado-data" if os.environ.get("VERCEL") == "1" else "data"
    data_dir = os.environ.get("DATA_DIR", _default_dir)
    if len(sys.argv) > 1:
        data_dir = sys.argv[1]
    interval = int(os.environ.get("UPDATE_INTERVAL_DAYS", "7"))
    t0 = time.time()
    tv_meta, books_meta = fetch_all(data_dir, interval_days=interval)
    print(f"完成: TVBox {tv_meta['site_count']} 站, 书源 {books_meta['book_count']} 个, 用时 {time.time()-t0:.1f}s")
