#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地验证: 抓取状态持久化 / 刷新不重跑 / 重启标记中断 / 进度条与日志时间戳。

只用桩 (缓慢的假 fetcher) 测流程, 不跑 20-60 分钟真实全量抓取。
2026-11-16 规则 (用户纠正后): 启动时数据缺失或已过期就自动从 0 抓一次;
data/ 是抓取输出, 不是输入。
用法: python3 tests/test_progress_persistence.py
要求: 项目根目录下运行; 依赖 Flask + requests(系统已有)。
"""
import http.cookiejar
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 18081
BASE = f"http://127.0.0.1:{PORT}"
PASSWORD = "testpw123"

LAUNCHER = r'''
import os, sys, threading, time
sys.path.insert(0, r"{proj}")
import fetcher

STAGES = fetcher.STAGE_NAMES

def fake_fetch_all(data_dir, progress=None, interval_days=7):
    def emit(idx, pct, cur, done=None, total=None):
        if progress:
            progress({"type": "progress", "stage": STAGES[idx], "stage_index": idx + 1,
                      "stage_total": 8, "percent": pct, "current": cur,
                      "done": done, "total": total})
    def log(msg):
        if progress:
            progress(msg)
    log("桩: 发现新源开始")
    emit(0, 2, "桩: 正在搜索新源", 1, 10)
    time.sleep(2)
    log("桩: 发现 3 个候选源")
    emit(1, 12, "桩: 正在抓取 TVBox 上游", 5, 20)
    time.sleep(2)
    # 静默 8 秒: 不发日志也不发进度, 用于触发"卡住"提示 (STALL_AFTER_SECONDS=5)
    time.sleep(8)
    log("桩: 静默期结束, 继续测速")
    emit(2, 25, "桩: TVBox 测速第 1 轮", 10, 20)
    time.sleep(2)
    log("桩: 写文件")
    emit(7, 95, "桩: 正在写文件")
    time.sleep(1)
    return ({"site_count": 120}, {"book_count": 150})

fetcher.fetch_all = fake_fetch_all
import app
threading.Thread(target=app._scheduler_loop, daemon=True).start()
app.app.run(host="127.0.0.1", port=int(os.environ["PORT"]))
'''


def start_server(data_dir, proj_dir=None):
    """起一个服务子进程。proj_dir 缺省为真实项目目录; 场景 5 传入不含 data/
    的项目副本, 这样仓库快照也不存在, _data_source() 才会返回 'none'。"""
    proj_dir = proj_dir or PROJECT
    env = dict(os.environ, DATA_DIR=data_dir, PORT=str(PORT),
               ACCESS_PASSWORD=PASSWORD, STALL_AFTER_SECONDS="5",
               PYTHONUNBUFFERED="1")
    os.makedirs(data_dir, exist_ok=True)
    launcher = os.path.join(data_dir, "_launcher.py")
    with open(launcher, "w", encoding="utf-8") as f:
        f.write(LAUNCHER.replace("{proj}", proj_dir))
    proc = subprocess.Popen(
        [sys.executable, launcher], cwd=PROJECT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return proc


def wait_up(proc, timeout=20):
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj))
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("服务启动失败:\n" + proc.stdout.read())
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


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise AssertionError(f"{name}: {detail}")


def seed_data(data_dir, expired=True):
    """在 data_dir 里放一份假的已有数据 (tvbox.json + books.json + 过期的 status.json)。"""
    import datetime
    os.makedirs(os.path.join(data_dir, "tvbox"), exist_ok=True)
    os.makedirs(os.path.join(data_dir, "books"), exist_ok=True)
    json.dump({"sites": [{"key": "x"}]}, open(os.path.join(data_dir, "tvbox", "tvbox.json"), "w"))
    json.dump([{"bookSourceName": "y"}], open(os.path.join(data_dir, "books", "books.json"), "w"))
    last = (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=10 if expired else 0)).isoformat().replace("+00:00", "Z")
    json.dump({"last_update": last, "next_update": last, "interval_days": 7,
               "tvbox_sites": 1, "book_sources": 1},
              open(os.path.join(data_dir, "status.json"), "w"))


def main():
    tmp = tempfile.mkdtemp(prefix="tvbox-persist-test-")
    print("测试目录:", tmp)

    # ---------- 场景 0: 用真实 data/ 验证每小时检查不会触发 ----------
    # (用户这次 last_update 2026-11-13, 7 天间隔, 到 11-20 才过期)
    print("场景 0: 真实 data/ 下每小时检查不应触发")
    chk = subprocess.run(
        [sys.executable, "-c",
         "import app; print('expired=', app._is_expired()); "
         "print('source=', app._data_source())"],
        cwd=PROJECT, env=dict(os.environ, DATA_DIR="data"),
        capture_output=True, text=True, timeout=60)
    print("  " + chk.stdout.strip().replace("\n", "\n  "))
    check("真实数据未过期 (_is_expired False)", "expired= False" in chk.stdout, chk.stdout + chk.stderr)
    check("真实数据来源 live", "source= live" in chk.stdout, chk.stdout + chk.stderr)

    data_dir = os.path.join(tmp, "data")

    # ---------- 场景 1: 已有数据 (已过期) 启动, 自动抓一次 ----------
    # 2026-11-16 修正 (用户纠正后推翻旧规则): 启动时数据缺失或已过期就
    # 自动从 0 抓一次。桩 fetcher 约 15 秒跑完, 先等它启动, 再等它跑完,
    # 否则场景 2 的手动 POST 会撞上 409。
    print("场景 1: 已有过期数据启动, 自动抓一次 (2026-11-16 新规则)")
    seed_data(data_dir, expired=True)
    proc = start_server(data_dir)
    try:
        opener = wait_up(proc)
        _, j = api(opener, "/api/login", "POST", {"password": PASSWORD})
        check("登录成功", j.get("ok") is True, str(j))
        boot_started = None
        for _ in range(20):
            _, st = api(opener, "/api/update/status")
            if st.get("running"):
                boot_started = st.get("started_at")
                break
            time.sleep(0.5)
        check("启动时自动抓取 (running=True)", bool(boot_started),
              f"最后状态: {st}")
        check("启动时自动抓取 (started_at 已设)", bool(boot_started))
        for _ in range(40):
            _, st = api(opener, "/api/update/status")
            if not st.get("running") and st.get("done"):
                break
            time.sleep(0.5)
        check("启动自动抓取跑完 (done=True)", st.get("done") is True
              and st.get("running") is False, str(st.get("running")))

        # ---------- 场景 2: 手动开始抓取, 刷新页面不重跑 ----------
        print("场景 2: 手动抓取中刷新页面, 不重跑")
        code, j = api(opener, "/api/update", "POST")
        check("POST /api/update 接受", code == 200 and j.get("ok") is True, f"{code} {j}")
        time.sleep(1)
        _, st1 = api(opener, "/api/update/status")
        started1 = st1.get("started_at")
        run1 = st1.get("run_id")
        pct1 = st1.get("percent") or 0
        check("抓取已开始", st1.get("running") is True, str(st1.get("running")))
        check("本轮有 run_id", bool(run1), str(run1))
        # 模拟多次刷新首页 (GET / 不调 /api/update): 只发刷新时会发的 GET
        for _ in range(3):
            with opener.open(BASE + "/", timeout=5) as r:
                html = r.read().decode()
            api(opener, "/api/status")
            api(opener, "/api/files")
            time.sleep(0.5)
        _, st2 = api(opener, "/api/update/status")
        check("刷新后仍是同一轮 (started_at 不变)", st2.get("started_at") == started1,
              f"{st1.get('started_at')} -> {st2.get('started_at')}")
        check("刷新后仍是同一轮 (run_id 不变)", st2.get("run_id") == run1,
              f"{run1} -> {st2.get('run_id')}")
        check("刷新后百分比不回跳 (单调不减)", (st2.get("percent") or 0) >= pct1,
              f"{pct1} -> {st2.get('percent')}")
        check("刷新后仍在跑 (running=True)", st2.get("running") is True)
        code, j = api(opener, "/api/update", "POST")
        check("重复点击返回 409", code == 409, f"{code} {j}")
        # 进度条与日志时间戳
        check("日志带时间戳", bool(st2.get("logs")) and all(
            isinstance(e, dict) and e.get("ts") and e.get("msg") for e in st2["logs"]),
            str(st2.get("logs"))[:200])
        check("进度字段齐全", st2.get("stage_index") in (1, 2) and st2.get("percent", 0) > 0,
              str({k: st2.get(k) for k in ("stage", "stage_index", "percent", "current")}))
        check("前端有新进度条元素", all(x in html for x in
              ("progPct", "progSteps", "stallBox", "interruptBox", "progRun", "STAGE_NAMES")),
              "缺元素")
        # 状态文件已落盘
        sf = os.path.join(data_dir, "update_state.json")
        check("状态文件已落盘", os.path.exists(sf))
        if os.path.exists(sf):
            saved = json.load(open(sf, encoding="utf-8"))
            check("落盘状态 running=True", saved.get("running") is True)

        # ---------- 场景 3: 静默期触发"卡住"提示 ----------
        print("场景 3: 超过 5 秒无进展, 应报 stalled")
        stalled_seen = False
        for _ in range(10):
            _, st = api(opener, "/api/update/status")
            if st.get("stalled"):
                stalled_seen = True
                break
            time.sleep(1)
        check("静默期 stalled=True", stalled_seen, f"最后状态: stalled={st.get('stalled')}")
        check("stalled_seconds >= 5", (st.get("stalled_seconds") or 0) >= 5,
              str(st.get("stalled_seconds")))

        # ---------- 场景 4: 抓取中途杀掉服务再重启 ----------
        # 2026-11-16 新规则: 重启后旧轮标记 interrupted (从落盘状态可查),
        # 但启动时数据仍过期, 所以会开始一轮全新的 boot 自动抓取 (新 run_id),
        # 不是悄悄续跑旧轮。
        print("场景 4: 抓取中途杀服务再重启, 旧轮标 interrupted, 新一轮 boot 自动抓")
        _, st_before = api(opener, "/api/update/status")
        started_before = st_before.get("started_at")
        run_before = st_before.get("run_id")
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        proc = start_server(data_dir)
        # interrupted 在 import 时就落盘 (早于 HTTP 服务就绪), 先读磁盘状态
        sf = os.path.join(data_dir, "update_state.json")
        saved = {}
        for _ in range(50):
            if os.path.exists(sf):
                try:
                    saved = json.load(open(sf, encoding="utf-8"))
                except Exception:
                    saved = {}
                if saved.get("interrupted"):
                    break
            time.sleep(0.2)
        check("重启后 interrupted=True (落盘状态)", saved.get("interrupted") is True,
              str(saved.get("interrupted")))
        check("中断原因写明了时间与进度", bool(saved.get("interrupted_reason"))
              and "重启中断" in saved["interrupted_reason"],
              str(saved.get("interrupted_reason"))[:120])
        check("中断日志已追加", any("重启中断" in (e.get("msg") or "")
              for e in saved.get("logs", []) if isinstance(e, dict)))
        opener = wait_up(proc)
        _, j = api(opener, "/api/login", "POST", {"password": PASSWORD})
        check("重启后登录成功", j.get("ok") is True)
        # 等 boot 自动抓取启动: 新 started_at、新 run_id, 不是续跑旧轮
        new_started = None
        for _ in range(24):
            _, st = api(opener, "/api/update/status")
            if st.get("running") and st.get("started_at") != started_before:
                new_started = st.get("started_at")
                break
            time.sleep(0.5)
        check("重启后启动新一轮自动抓取 (running=True)", bool(new_started),
              f"最后状态: {st}")
        check("新轮 run_id 与旧轮不同", st.get("run_id") != run_before,
              f"{run_before} -> {st.get('run_id')}")
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=10)

    # ---------- 场景 5: 完全无数据启动, 自动抓一次 ----------
    # 2026-11-16 修正 (用户纠正后推翻旧规则): 启动时数据缺失就自动从 0 抓,
    # data/ 是抓取输出不是输入, 不传 data/ 不影响流程。注意: _data_source()
    # 会回退到仓库 data/ 快照, 所以必须用一份不含 data/ 的项目副本跑,
    # 否则来源是 snapshot 而不是 none。
    print("场景 5: 完全无数据启动, 自动抓一次 (2026-11-16 新规则)")
    import shutil
    proj_copy = os.path.join(tmp, "proj-nodata")
    shutil.copytree(PROJECT, proj_copy, ignore=shutil.ignore_patterns(
        "data", ".git", "__pycache__", "*.pyc", "tests", "research",
        "rerun-20261114.log"))
    data_dir2 = os.path.join(tmp, "data2")
    proc = start_server(data_dir2, proj_dir=proj_copy)
    try:
        opener = wait_up(proc)
        _, j = api(opener, "/api/login", "POST", {"password": PASSWORD})
        check("登录成功", j.get("ok") is True)
        boot_started = None
        for _ in range(24):  # 调度器 3 秒后检查, 桩 fetcher 约 15 秒跑完
            _, st = api(opener, "/api/update/status")
            if st.get("running"):
                boot_started = st.get("started_at")
                break
            time.sleep(0.5)
        check("无数据时启动自动抓取 (running=True)", bool(boot_started),
              f"最后状态: {st}")
        check("无数据时 started_at 已设", bool(boot_started), str(st.get("started_at")))
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=10)

    print("\n全部通过。测试目录保留在:", tmp)


if __name__ == "__main__":
    main()
