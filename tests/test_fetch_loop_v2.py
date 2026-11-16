#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地验证: 2026-11-16 循环根因修复 (第二轮, 推翻上一轮诊断后重查)。

上一轮诊断 ("重启→清空→重抓") 是错的, 用户 2026-11-16 16:17 纠正:
data/ 是抓取输出不是输入, 服务开了保活、这几天没有重启过。
真正机制 (单进程内、无重启): fetch_all 任一阶段抛异常就跳过 status.json
写出 -> _is_expired() 永远为真 -> 调度器每小时重开一轮 -> 每轮又在同一处
抛掉。最可能的死点是书源阶段 Python 回退的搜书抽查/复测轮
(_search_test_one_detailed 抛异常经 fut.result() 抛回主线程)。

只用桩, 不跑 20-60 分钟真实全量抓取。
用法: python3 tests/test_fetch_loop_v2.py
要求: 项目根目录下运行; 依赖 Flask + requests(系统已有)。
覆盖:
  ① _search_test_one_detailed 抛异常 -> 整轮不崩、该源记跳过、status.json 仍写出
  ② Node 校验不可用/超时 -> 走 Python 回退且不崩
  ③ 某阶段抛异常 -> 收尾仍写 status.json 带 error, 一小时后调度器不重开
  ④ 启动无数据 -> 自动抓一次 (且只抓一次)
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
PORT = 18083
BASE = f"http://127.0.0.1:{PORT}"
PASSWORD = "testpw123"

import fetcher  # noqa: E402


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(f"{name}: {detail}")


def test_search_throws():
    """① 搜书校验抛异常, 整轮不崩、该源记跳过、status.json 仍写出。"""
    print("① _search_test_one_detailed 抛异常, 整轮不崩")
    item = {"bookSourceName": "坏源", "bookSourceUrl": "http://bad.example.com",
            "searchUrl": "http://bad.example.com/search?q={{key}}"}
    orig_impl = fetcher._search_test_one_detailed_impl
    fetcher._search_test_one_detailed_impl = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("boom in parse"))
    try:
        ok, reason = fetcher._search_test_one_detailed(item, "斗破苍穹", "天蚕土豆")
        check("抛异常只记跳过 (不判 0 分)", ok is None and reason.startswith("skipped:internal_RuntimeError"),
              f"ok={ok} reason={reason}")
        # 整轮: 假 TVBox 阶段里也调一次会抛的校验, 假书源阶段正常返回
        orig_discover = fetcher.discover_upstreams
        orig_tv = fetcher.fetch_tvbox
        orig_books = fetcher.fetch_books

        def fake_tv(data_dir, progress=None, extra_sources=None, discovery=None):
            fetcher._search_test_one_detailed(item, "斗破苍穹", "天蚕土豆")
            return {"site_count": 5}

        fetcher.discover_upstreams = lambda progress=None: {"tvbox": [], "books": []}
        fetcher.fetch_tvbox = fake_tv
        fetcher.fetch_books = lambda data_dir, progress=None, extra_sources=None, discovery=None: {"book_count": 3}
        tmp = tempfile.mkdtemp(prefix="v2-search-")
        try:
            tv_meta, books_meta = fetcher.fetch_all(tmp)
            check("整轮正常返回", tv_meta.get("site_count") == 5 and books_meta.get("book_count") == 3,
                  f"{tv_meta} {books_meta}")
            st_path = os.path.join(tmp, "status.json")
            check("status.json 仍写出", os.path.exists(st_path))
            st = json.load(open(st_path, encoding="utf-8"))
            check("status.json 无 error", "error" not in st, str(st))
            check("status.json last_update 已设", bool(st.get("last_update")))
        finally:
            fetcher.discover_upstreams = orig_discover
            fetcher.fetch_tvbox = orig_tv
            fetcher.fetch_books = orig_books
    finally:
        fetcher._search_test_one_detailed_impl = orig_impl


def test_node_unavailable():
    """② Node 校验不可用/超时, 走 Python 回退且不崩。"""
    print("② Node 不可用/超时, Python 回退不崩")
    orig_bin = fetcher._node_bin
    try:
        fetcher._node_bin = lambda: None
        res, stats, err = fetcher._run_node_validation([], 60)
        check("无 Node 返回 node_not_found (不抛异常)",
              res is None and err == "node_not_found", f"err={err}")
        # 超时: 伪造一个 node, 让 subprocess.run 抛 TimeoutExpired
        import subprocess as _sp
        fetcher._node_bin = lambda: "/usr/bin/node"
        orig_run = _sp.run

        def _timeout(*a, **k):
            raise _sp.TimeoutExpired(cmd=a[0] if a else "node", timeout=60)

        _sp.run = _timeout
        try:
            res2, _, err2 = fetcher._run_node_validation([{"domain": "x.example.com"}], 60)
            # 超时走统一 except Exception 分支, 返回 (None, {}, "TimeoutExpired: ..."),
            # 调用方见 results 为 None 即回退 Python 抽查, 不崩。
            check("Node 超时返回 (None, {}, TimeoutExpired) (不抛异常)",
                  res2 is None and err2.startswith("TimeoutExpired:"), f"err={err2}")
        finally:
            _sp.run = orig_run
    finally:
        fetcher._node_bin = orig_bin


def test_stage_failure_writes_status():
    """③ 某阶段抛异常, 收尾仍写 status.json 带 error, 调度器一小时后不重开。"""
    print("③ 阶段抛异常, 收尾仍写 status.json 带 error")
    orig_discover = fetcher.discover_upstreams
    orig_tv = fetcher.fetch_tvbox
    orig_books = fetcher.fetch_books
    tmp = tempfile.mkdtemp(prefix="v2-stage-fail-")
    fetcher.discover_upstreams = lambda progress=None: {"tvbox": [], "books": []}
    fetcher.fetch_tvbox = lambda data_dir, progress=None, extra_sources=None, discovery=None: {"site_count": 120}

    def _boom(data_dir, progress=None, extra_sources=None, discovery=None):
        raise RuntimeError("books stage boom")

    fetcher.fetch_books = _boom
    try:
        tv_meta, books_meta = fetcher.fetch_all(tmp, interval_days=7)
        check("整轮返回 (TVBox 阶段的成果保留)",
              tv_meta.get("site_count") == 120 and books_meta.get("book_count") == 0,
              f"{tv_meta} {books_meta}")
        st_path = os.path.join(tmp, "status.json")
        check("status.json 仍写出", os.path.exists(st_path))
        st = json.load(open(st_path, encoding="utf-8"))
        check("status.json 带 error.books",
              isinstance(st.get("error"), dict) and "books stage boom" in st["error"].get("books", ""),
              str(st.get("error")))
        check("status.json partial=True", st.get("partial") is True)
        check("status.json last_update=now", bool(st.get("last_update")))
        check("status.json tvbox_sites=120", st.get("tvbox_sites") == 120)
        # 调度器一小时后不重开: _is_expired() 读刚写的 status.json, 7 天内为假
        os.environ["DATA_DIR"] = tmp
        os.environ["ACCESS_PASSWORD"] = PASSWORD
        import app
        check("一小时后 _is_expired() 为假 (不重开)", app._is_expired() is False,
              f"last_update={st.get('last_update')}")
        check("app 读到的状态带 error", bool(app._read_status().get("error")))
    finally:
        fetcher.discover_upstreams = orig_discover
        fetcher.fetch_tvbox = orig_tv
        fetcher.fetch_books = orig_books


# ---------------- ④ 子进程: 启动无数据自动抓一次 ----------------

LAUNCHER = r'''
import os, sys, threading, time, json
from datetime import datetime, timezone, timedelta
sys.path.insert(0, r"{proj}")
import fetcher

CALLS_FILE = os.environ.get("FETCH_CALLS_FILE", "")

def fake_fetch_all(data_dir, progress=None, interval_days=7):
    # 像真 fetch_all 一样写 status.json (last_update=now), 这样调度器
    # 一小时后不应再重开; 只记录调用次数供断言。
    if CALLS_FILE:
        with open(CALLS_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.time()}\n")
    if progress:
        progress({"type": "progress", "stage": "发现新源", "stage_index": 1,
                  "stage_total": 8, "percent": 5, "current": "桩: 发现",
                  "done": 1, "total": 10})
        progress("桩: 正在抓取")
    time.sleep(2)
    now = datetime.now(timezone.utc)
    status = {"last_update": now.isoformat().replace("+00:00", "Z"),
              "next_update": (now + timedelta(days=interval_days)).isoformat().replace("+00:00", "Z"),
              "interval_days": interval_days, "tvbox_sites": 120, "book_sources": 150}
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    return ({"site_count": 120}, {"book_count": 150})

fetcher.fetch_all = fake_fetch_all
import app
threading.Thread(target=app._scheduler_loop, daemon=True).start()
app.app.run(host="127.0.0.1", port=int(os.environ["PORT"]))
'''


def test_boot_autofetch_once():
    """④ 启动无数据自动抓一次 (且只抓一次: 写了 status.json 后不再重开)。"""
    print("④ 启动无数据, 自动抓一次")
    tmp = tempfile.mkdtemp(prefix="v2-boot-")
    proj_copy = os.path.join(tmp, "proj-nodata")
    shutil.copytree(PROJECT, proj_copy, ignore=shutil.ignore_patterns(
        "data", ".git", "__pycache__", "*.pyc", "tests", "research",
        "rerun-20261114.log"))
    data_dir = os.path.join(tmp, "data")
    calls = os.path.join(tmp, "calls.txt")
    os.makedirs(data_dir, exist_ok=True)
    launcher = os.path.join(data_dir, "_launcher.py")
    with open(launcher, "w", encoding="utf-8") as f:
        f.write(LAUNCHER.replace("{proj}", proj_copy))
    env = dict(os.environ, DATA_DIR=data_dir, PORT=str(PORT),
               ACCESS_PASSWORD=PASSWORD, STALL_AFTER_SECONDS="5",
               SCHEDULER_CHECK_SECONDS="5", FETCH_CALLS_FILE=calls,
               PYTHONUNBUFFERED="1")
    log_path = os.path.join(data_dir, "server.log")
    logf = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, launcher], cwd=PROJECT, env=env,
        stdout=logf, stderr=subprocess.STDOUT, text=True)
    try:
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        deadline = time.time() + 20
        up = False
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("服务启动失败")
            try:
                with opener.open(BASE + "/", timeout=2) as r:
                    if r.status == 200:
                        up = True
                        break
            except Exception:
                time.sleep(0.3)
        check("服务启动", up)
        first = None
        for _ in range(20):
            if os.path.exists(calls):
                first = float(open(calls).read().strip().splitlines()[0])
                break
            time.sleep(0.5)
        check("启动自动抓取了", first is not None)
        time.sleep(12)  # 两个 5 秒间隔检查窗口: 写了 status.json, 不应再抓
        lines = open(calls).read().strip().splitlines() if os.path.exists(calls) else []
        check("12 秒后仍只抓了一次", len(lines) == 1, f"调用 {len(lines)} 次: {lines}")
        st_path = os.path.join(data_dir, "status.json")
        check("status.json 已写出", os.path.exists(st_path))
        if os.path.exists(st_path):
            st = json.load(open(st_path, encoding="utf-8"))
            check("status.json last_update 已设", bool(st.get("last_update")))
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=10)


def main():
    test_search_throws()
    test_node_unavailable()
    test_stage_failure_writes_status()
    test_boot_autofetch_once()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
