#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地验证: 抓取循环断开 / 解析失败四类修复 / 内存日志。

只用桩与本地伪造, 不跑 20-60 分钟真实全量抓取。
用法: python3 tests/test_fetch_loop_fixes.py
要求: 项目根目录下运行; 依赖 Flask + requests(系统已有)。
2026-11-16 规则 (用户纠正后): 启动时数据缺失或已过期就自动从 0 抓一次;
data/ 是抓取输出, 不是输入。注意本文件的桩 fetch_all 不写 status.json,
所以每小时检查会重复触发 —— “只抓一次”不在此验, 在
tests/test_fetch_loop_v2.py 里用真 fetch_all + 写 status.json 的桩阶段验。
覆盖 (对应任务书):
  ① 启动时完全无数据 -> 自动抓一次 (子进程, 桩 fetch_all 带调用记录)
  ② 数据存在但过期 -> 启动时自动抓 (每小时检查的单独隔离已无意义:
     过期数据启动就抓, 等不到每小时检查; 每小时检查本身只在数据过期时才抓)
  ③ 含控制字符的 JSON 用 strict=False 能解析; 非 JSON (HTML) 直接跳过
  ④ 重复 seed 只抓一次 (上游去重)
  ⑤ 死域名缓存: 3 次失败后第二轮跳过已知死 host; 24 小时后放行; 404 与
     “网络不可达”不计入死域名
  ⑥ 内存日志行正常输出 (RSS + 超阈值警告)
"""
import http.cookiejar
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)
PORT = 18082
BASE = f"http://127.0.0.1:{PORT}"
PASSWORD = "testpw123"

import fetcher  # noqa: E402


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(f"{name}: {detail}")


class Collector:
    """收 progress 回调发来的字符串日志与进度 dict。"""

    def __init__(self):
        self.logs = []
        self.progress = []

    def __call__(self, msg):
        if isinstance(msg, dict):
            self.progress.append(msg)
        else:
            self.logs.append(str(msg))


# ---------------- A. 进程内单元测试 (不联网) ----------------

def test_control_char_json():
    print("③ 含控制字符的 JSON 能解析 / 非 JSON 直接跳过")
    fetcher._fail_reset()
    bad = '{"sites": [{"key": "a", "api": "http://example.com/api", "type": 1, "name": "x\x01y"}]}'
    orig = fetcher.fetch_text
    fetcher.fetch_text = lambda urls, progress=None, label="": (bad, urls[0], "application/json")
    try:
        obj, used = fetcher._fetch_one_tvbox_json(["http://x/y.json"])
    finally:
        fetcher.fetch_text = orig
    check("控制字符 JSON 解析成功", isinstance(obj, dict) and obj.get("sites"), str(obj)[:120])
    sites, _, _, _, _, _ = fetcher._parse_tvbox_obj(obj, "http://x/")
    check("解析出 1 个站", len(sites) == 1 and sites[0]["key"] == "a", str(sites))

    html = "<!DOCTYPE html><html><head><title>404 Not Found</title></head></html>"
    fetcher.fetch_text = lambda urls, progress=None, label="": (html, urls[0], "text/html; charset=utf-8")
    try:
        obj2, used2 = fetcher._fetch_one_tvbox_json(["https://agit.ai/a/b.json"])
    finally:
        fetcher.fetch_text = orig
    check("HTML 响应直接跳过 (返回 None)", obj2 is None and used2 is None)
    check("非 JSON 计入失败分类", fetcher._FAIL["by_kind"].get("非JSON", 0) >= 1,
          str(fetcher._FAIL["by_kind"]))


def test_dedup_sources():
    print("④ 重复 seed 只抓一次")
    srcs = [
        {"name": "seed direct https://cccimg.com/down.php/33c22b45.json", "direct_url": "https://cccimg.com/down.php/33c22b45.json"},
        {"name": "seed direct HTTPS://CCCIMG.COM/down.php/33c22b45.json/", "direct_url": "HTTPS://CCCIMG.COM/down.php/33c22b45.json/"},
        {"name": "seed direct https://other.com/a.json", "direct_url": "https://other.com/a.json"},
        {"name": "seed a/b c.json", "owner": "a", "repo": "b", "branch": "main", "path": "c.json"},
        {"name": "seed a/b c.json 2", "owner": "A", "repo": "B", "branch": "main", "path": "c.json"},
        {"name": "seed a/b d.json", "owner": "a", "repo": "b", "branch": "main", "path": "d.json"},
    ]
    col = Collector()
    out, dropped = fetcher._dedup_sources(srcs, progress=col)
    check("6 个上游去重后剩 4 个", len(out) == 4, f"剩 {len(out)} 个")
    check("丢掉 2 个重复", dropped == 2, f"丢掉 {dropped} 个")
    check("重复上游有日志", any("跳过重复上游" in m for m in col.logs), str(col.logs))


def test_dead_host_cache():
    print("⑤ 死域名缓存")
    tmp = tempfile.mkdtemp(prefix="dead-host-test-")
    fetcher._dead_hosts_init(tmp)
    fetcher._fail_reset()
    host = "pandown.pro"
    check("初始不是死域名", not fetcher._dead_host_is_dead(host))
    fetcher._dead_host_fail(host)
    fetcher._dead_host_fail(host)
    check("2 次失败还不是死域名", not fetcher._dead_host_is_dead(host))
    fetcher._dead_host_fail(host)
    check("3 次失败后是死域名", fetcher._dead_host_is_dead(host))
    check("缓存文件已落盘", os.path.exists(os.path.join(tmp, "dead_hosts.json")))
    # 成功一次即复活
    fetcher._dead_host_ok(host)
    check("成功后清掉记录", not fetcher._dead_host_is_dead(host))
    # 过期放行: 重新致死, 再把期限改到过去
    for _ in range(3):
        fetcher._dead_host_fail(host)
    check("再次致死", fetcher._dead_host_is_dead(host))
    rec = fetcher._DEAD_HOSTS[host]
    import datetime
    rec["dead_until"] = (datetime.datetime.now(datetime.timezone.utc)
                         - datetime.timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    check("24 小时过期后放行", not fetcher._dead_host_is_dead(host))

    # fetch_text 层面: 同一 host 连超时 3 次后, 第 4 次不再发请求
    calls = {"n": 0}

    class FakeResp:
        def __init__(self, status=200, text="{}", ctype="application/json"):
            self.status_code = status
            self.text = text
            self.headers = {"Content-Type": ctype}

    class FakeTimeout(Exception):
        pass

    orig_get = fetcher.requests.get

    def fake_get(url, **kw):
        calls["n"] += 1
        raise fetcher.requests.exceptions.ConnectTimeout(f"timed out {url}")

    fetcher.requests.get = fake_get
    # 注意: 用全新目录初始化, 避免上一段的死域名记录 (落盘有 30 秒节流) 干扰。
    tmp2 = tempfile.mkdtemp(prefix="dead-host-fetch-test-")
    fetcher._dead_hosts_init(tmp2)
    fetcher._fail_reset()
    col = Collector()
    try:
        for _ in range(3):
            t, u, c = fetcher.fetch_text(["http://pandown.pro/tvbox/tvbox.json"],
                                         progress=col)
            check("超时返回 (None, None, None)", (t, u, c) == (None, None, None))
        check("3 次都发了请求", calls["n"] == 3, f"发了 {calls['n']} 次")
        before = calls["n"]
        t, u, c = fetcher.fetch_text(["http://pandown.pro/tvbox/tvbox.json"], progress=col)
        check("第 4 次直接跳过死域名 (不再发请求)", calls["n"] == before,
              f"请求数 {before} -> {calls['n']}")
        check("跳过死域名有日志", any("跳过死域名 pandown.pro" in m for m in col.logs),
              str(col.logs))
        check("失败汇总含连接超时", fetcher._FAIL["by_kind"].get("连接超时", 0) == 3,
              str(fetcher._FAIL["by_kind"]))
    finally:
        fetcher.requests.get = orig_get

    # 404 不计入死域名
    def fake_404(url, **kw):
        return FakeResp(status=404, text="not found", ctype="text/html")

    fetcher.requests.get = fake_404
    tmp3 = tempfile.mkdtemp(prefix="dead-host-404-test-")
    fetcher._dead_hosts_init(tmp3)
    fetcher._fail_reset()
    try:
        for _ in range(3):
            fetcher.fetch_text(["http://example.com/nope.json"], progress=None)
        check("404 三次也不进死域名", not fetcher._dead_host_is_dead("example.com"))
        check("404 按 HTTP 404 计数", fetcher._FAIL["by_kind"].get("HTTP 404", 0) == 3,
              str(fetcher._FAIL["by_kind"]))
    finally:
        fetcher.requests.get = orig_get

    # “网络不可达”是本机出口问题, 不计入死域名
    def fake_unreach(url, **kw):
        raise fetcher.requests.exceptions.ConnectionError(
            "HTTPSConnectionPool: Network is unreachable")

    fetcher.requests.get = fake_unreach
    tmp4 = tempfile.mkdtemp(prefix="dead-host-unreach-test-")
    fetcher._dead_hosts_init(tmp4)
    fetcher._fail_reset()
    try:
        for _ in range(3):
            fetcher.fetch_text(["http://ghproxy.net/x.json"], progress=None)
        check("网络不可达三次也不进死域名", not fetcher._dead_host_is_dead("ghproxy.net"))
        check("网络不可达单独计数", fetcher._FAIL["by_kind"].get("网络不可达", 0) == 3,
              str(fetcher._FAIL["by_kind"]))
    finally:
        fetcher.requests.get = orig_get


def test_mem_log():
    print("⑥ 内存日志行正常输出")
    rss = fetcher._rss_mb()
    check("RSS 能读到 (>0)", isinstance(rss, (int, float)) and rss > 0, str(rss))
    col = Collector()
    fetcher._log_mem(col, "测试位置")
    check("内存行格式 [内存] ... RSS x MB", any(m.startswith("[内存] 测试位置 RSS ") and "MB" in m for m in col.logs),
          str(col.logs))
    # 超阈值警告: 把阈值临时设 0, 必然触发
    old_warn = fetcher.MEM_WARN_MB
    fetcher.MEM_WARN_MB = 0
    try:
        col2 = Collector()
        fetcher._log_mem(col2, "测试警告")
        check("超阈值打 [内存警告] 行", any(m.startswith("[内存警告]") for m in col2.logs),
              str(col2.logs))
    finally:
        fetcher.MEM_WARN_MB = old_warn
    # 阶段切换打内存行
    fetcher._LAST_STAGE_IDX[0] = 0
    col3 = Collector()
    fetcher._emit(col3, 1, 0.0, "阶段一")
    fetcher._emit(col3, 2, 0.0, "阶段二")
    check("阶段切换打了 2 行内存日志",
          sum(1 for m in col3.logs if m.startswith("[内存]")) == 2, str(col3.logs))


def test_classify():
    print("失败分类映射")
    c = fetcher._classify_fetch_error
    check("ConnectTimeout -> 连接超时", c("ConnectTimeout: timed out") == "连接超时")
    check("NameResolutionError -> DNS解析失败",
          c("NameResolutionError: Failed to resolve 'x'") == "DNS解析失败")
    check("证书过期 -> SSL证书",
          c("SSLError: certificate has expired") == "SSL证书")
    check("HTTP 状态 -> HTTP 423", c("", status=423) == "HTTP 423")
    check("汇总文本格式", "失败" in fetcher._fail_summary_text())


# ---------------- B. 子进程调度器测试 (桩 fetch_all) ----------------

LAUNCHER = r'''
import os, sys, threading, time
sys.path.insert(0, r"{proj}")
import fetcher

CALLS_FILE = os.environ.get("FETCH_CALLS_FILE", "")

def fake_fetch_all(data_dir, progress=None, interval_days=7):
    if CALLS_FILE:
        with open(CALLS_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.time()}\n")
    if progress:
        progress({"type": "progress", "stage": "发现新源", "stage_index": 1,
                  "stage_total": 8, "percent": 5, "current": "桩: 发现",
                  "done": 1, "total": 10})
        progress("桩: 正在抓取")
    time.sleep(2)
    return ({"site_count": 120}, {"book_count": 150})

fetcher.fetch_all = fake_fetch_all
import app
threading.Thread(target=app._scheduler_loop, daemon=True).start()
app.app.run(host="127.0.0.1", port=int(os.environ["PORT"]))
'''


def start_server(data_dir, proj_dir, calls_file, extra_env=None):
    env = dict(os.environ, DATA_DIR=data_dir, PORT=str(PORT),
               ACCESS_PASSWORD=PASSWORD, STALL_AFTER_SECONDS="5",
               SCHEDULER_CHECK_SECONDS="5", FETCH_CALLS_FILE=calls_file,
               PYTHONUNBUFFERED="1")
    if extra_env:
        env.update(extra_env)
    os.makedirs(data_dir, exist_ok=True)
    launcher = os.path.join(data_dir, "_launcher.py")
    with open(launcher, "w", encoding="utf-8") as f:
        f.write(LAUNCHER.replace("{proj}", proj_dir))
    log_path = os.path.join(data_dir, "server.log")
    logf = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, launcher], cwd=PROJECT, env=env,
        stdout=logf, stderr=subprocess.STDOUT, text=True)
    return proc, log_path


def wait_up(proc, timeout=20):
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("服务启动失败")
        try:
            with opener.open(BASE + "/", timeout=2) as r:
                if r.status == 200:
                    return opener
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("服务未在规定时间内启动")


def api(opener, path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def seed_expired_data(data_dir):
    import datetime
    os.makedirs(os.path.join(data_dir, "tvbox"), exist_ok=True)
    os.makedirs(os.path.join(data_dir, "books"), exist_ok=True)
    json.dump({"sites": [{"key": "x"}]},
              open(os.path.join(data_dir, "tvbox", "tvbox.json"), "w"))
    json.dump([{"bookSourceName": "y"}],
              open(os.path.join(data_dir, "books", "books.json"), "w"))
    last = (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=10)).isoformat().replace("+00:00", "Z")
    json.dump({"last_update": last, "next_update": last, "interval_days": 7,
               "tvbox_sites": 1, "book_sources": 1},
              open(os.path.join(data_dir, "status.json"), "w"))


def stop(proc):
    if proc.poll() is None:
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)


def main():
    tmp = tempfile.mkdtemp(prefix="tvbox-loop-fix-test-")
    print("测试目录:", tmp)
    test_control_char_json()
    test_dedup_sources()
    test_dead_host_cache()
    test_mem_log()
    test_classify()

    # ---------- 场景 ①: 完全无数据启动, 自动抓一次 ----------
    # 2026-11-16 新规则: 启动时数据缺失就自动从 0 抓 (data/ 是输出不是输入)。
    # 注意: 本桩不写 status.json, 所以 5 秒间隔的每小时检查还会再触发,
    # 这里只断言“启动自动抓了”, “只抓一次”见 test_fetch_loop_v2.py。
    print("① 完全无数据启动, 自动抓一次 (2026-11-16 新规则)")
    proj_copy = os.path.join(tmp, "proj-nodata")
    shutil.copytree(PROJECT, proj_copy, ignore=shutil.ignore_patterns(
        "data", ".git", "__pycache__", "*.pyc", "tests", "research",
        "rerun-20261114.log"))
    data_dir1 = os.path.join(tmp, "data1")
    calls1 = os.path.join(tmp, "calls1.txt")
    t0 = time.time()
    proc, log1 = start_server(data_dir1, proj_copy, calls1)
    try:
        opener = wait_up(proc)
        _, j = api(opener, "/api/login", "POST", {"password": PASSWORD})
        check("登录成功", j.get("ok") is True, str(j))
        first_call = None
        for _ in range(20):
            if os.path.exists(calls1):
                first_call = float(open(calls1).read().strip().splitlines()[0])
                break
            time.sleep(0.5)
        check("启动自动抓取了", first_call is not None)
        if first_call is not None:
            check("首次调用在启动 8 秒内 (调度器 3 秒后检查)",
                  first_call - t0 < 8, f"首次调用在 {first_call - t0:.1f}s")
        _, st = api(opener, "/api/update/status")
        check("started_at 已设 (已开始过)", bool(st.get("started_at")),
              str(st.get("started_at")))
        time.sleep(0.5)
        log_text = open(log1, encoding="utf-8", errors="ignore").read()
        check("启动日志有 boot id 行", "[boot " in log_text, log_text[-500:])
        check("启动日志写明数据状态 none", "数据状态: none" in log_text, log_text[-500:])
        check("调度器日志写明开始首次抓取", "开始首次抓取" in log_text, log_text[-500:])
        # 手动触发仍可用 (桩每 5 秒还会被调度器再触发, 等空闲窗口再点)
        code, j = None, None
        for _ in range(20):
            _, st_now = api(opener, "/api/update/status")
            if not st_now.get("running"):
                code, j = api(opener, "/api/update", "POST")
                if code == 200:
                    break
            time.sleep(0.5)
        check("手动更新仍可触发", code == 200 and j.get("ok") is True, f"{code} {j}")
        time.sleep(3)
        check("手动触发后桩被调用", os.path.exists(calls1))
    finally:
        stop(proc)

    # ---------- 场景 ②: 数据存在但过期, 启动时自动抓 ----------
    # 2026-11-16 新规则: 过期数据启动就抓, 等不到每小时检查; 每小时检查
    # 本身只在数据过期时才抓 (见 _scheduler_loop), 与启动抓是同一条件。
    print("② 数据存在但过期, 启动时自动抓")
    data_dir2 = os.path.join(tmp, "data2")
    calls2 = os.path.join(tmp, "calls2.txt")
    seed_expired_data(data_dir2)
    t0 = time.time()
    proc, log2 = start_server(data_dir2, PROJECT, calls2)
    try:
        opener = wait_up(proc)
        _, j = api(opener, "/api/login", "POST", {"password": PASSWORD})
        check("登录成功", j.get("ok") is True)
        first_call = None
        for _ in range(20):
            if os.path.exists(calls2):
                first_call = float(open(calls2).read().strip().splitlines()[0])
                break
            time.sleep(0.5)
        check("启动自动抓取了", first_call is not None)
        if first_call is not None:
            check("首次调用在启动 8 秒内 (不是等每小时检查)",
                  first_call - t0 < 8, f"首次调用在 {first_call - t0:.1f}s")
        log_text = open(log2, encoding="utf-8", errors="ignore").read()
        check("启动日志写明数据状态 live", "数据状态: live" in log_text, log_text[-500:])
        check("调度器日志写明开始首次抓取", "开始首次抓取" in log_text, log_text[-500:])
    finally:
        stop(proc)

    print("\n全部通过。测试目录保留在:", tmp)


if __name__ == "__main__":
    main()
