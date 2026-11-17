#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TVBox + Legado 源聚合器 Web 入口 (Flask)
环境变量:
  ACCESS_PASSWORD      访问密码, 默认 change-me (未改会打印警告)
  UPDATE_INTERVAL_DAYS 定时抓取间隔天数, 默认 7
  PORT                 监听端口, 默认 8000
  DATA_DIR             数据目录, 默认 data; 在 Vercel 上默认改为 /tmp/tvbox-legado-data
                       (Vercel 部署后的文件系统只读, 只有 /tmp 可写, 见 README"部署到 Vercel")
  SECRET_KEY           会话密钥, 不设则每次启动随机生成
  VERCEL_FETCH_ENABLED 在 Vercel 上允许手动/定时抓取, 默认 0 (不允许, 见下)
                       Vercel Serverless 函数有执行时长上限 (免费版约 10-60 秒),
                       全量抓取要 4-7 分钟, 一次请求内跑不完, 且后台线程在函数
                       返回后会被冻结, 所以默认关闭, 只读仓库自带的 data/ 快照。
                       如确需在 Vercel 上抓取, 设为 1 自担风险 (大概率超时中断)。
启动: python app.py   (或 gunicorn / docker)
"""
import hmac
import io
import json
import os
import secrets
import shutil
import threading
import time
import zipfile
from datetime import datetime, timezone, timedelta

from flask import (Flask, abort, jsonify, render_template, request,
                   send_file, session)

import fetcher

ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "change-me")
UPDATE_INTERVAL_DAYS = int(os.environ.get("UPDATE_INTERVAL_DAYS", "7") or 7)
PORT = int(os.environ.get("PORT", "8000") or 8000)

# --- Vercel 适配: 只读文件系统 ---
# Vercel 会自动设置 VERCEL=1。部署后的文件系统除 /tmp 外全部只读,
# 旧代码默认 DATA_DIR="data" (项目目录内), 一抓取就报
# [Errno 30] Read-only file system: 'data'。
# 修法: 在 Vercel 上默认写 /tmp; 其它平台 (本地/Render/VPS/Docker) 保持 data 不变。
# DATA_DIR 环境变量显式设置时永远优先, 不会被覆盖。
IS_VERCEL = os.environ.get("VERCEL") == "1" or bool(os.environ.get("VERCEL_ENV"))
VERCEL_FETCH_ENABLED = os.environ.get("VERCEL_FETCH_ENABLED", "0") == "1"
if os.environ.get("DATA_DIR"):
    DATA_DIR = os.environ["DATA_DIR"]
elif IS_VERCEL:
    DATA_DIR = "/tmp/tvbox-legado-data"
else:
    DATA_DIR = "data"

# 仓库自带的 data/ 快照 (只读): 历史遗留的回退路径, 仅当仓库里恰好有
# data/ 文件时才生效。按 2026-11-16 的规则, data/ 是抓取输出、永远不进
# 仓库, 所以 Render 上这条路是惰性的 (仓库里没有 data/); 启动时数据缺失
# 就自动从 0 抓, 不靠快照顶上。本地开发时若仓库 data/ 里有旧数据, 读路径
# 会回退到它, 直到新一轮抓完覆盖。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BUNDLED_DATA_DIR = os.path.join(BASE_DIR, "data")

if ACCESS_PASSWORD == "change-me":
    print("⚠️  警告: ACCESS_PASSWORD 未修改, 仍为默认值 change-me, 请在环境变量中改成强密码!",
          flush=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
# 会话有效期 (对方 config sessionHours=12; 环境变量 SESSION_HOURS 可覆盖)
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "12") or 12)
app.permanent_session_lifetime = timedelta(hours=SESSION_HOURS)

# ---------------- 数据目录: 建目录 + 快照播种 + 只读回退 ----------------
SNAPSHOT_FILES = ["tvbox/tvbox.json", "tvbox/tvbox_b.json", "tvbox/meta.json",
                  "tvbox/failures.json",
                  "books/books.json", "books/meta.json", "status.json"]


def _ensure_data_dir():
    """确保 DATA_DIR 可写, 并用仓库 data/ 快照补齐缺失文件。
    只补缺失的文件, 绝不覆盖已抓到的新数据。/tmp 被清空 (Vercel 冷启动)
    后靠这一步恢复。任一步失败只打日志, 不让启动崩掉 (读路径会回退到快照)。
    注意: 按 2026-11-16 的规则 data/ 永远不进仓库, Render 上仓库里没有
    data/, 这一步实际只建目录, 不播种任何东西; 启动自动从 0 抓才是正路。"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(os.path.join(DATA_DIR, "tvbox"), exist_ok=True)
        os.makedirs(os.path.join(DATA_DIR, "books"), exist_ok=True)
    except OSError as e:
        print(f"⚠️  数据目录 {DATA_DIR} 无法创建 (只读?): {e}, 将只读仓库快照", flush=True)
        return
    if os.path.abspath(DATA_DIR) == os.path.abspath(BUNDLED_DATA_DIR):
        return  # 本地/Render: DATA_DIR 就是仓库 data/, 无需播种
    for rel in SNAPSHOT_FILES:
        dst = os.path.join(DATA_DIR, rel)
        if os.path.exists(dst):
            continue
        src = os.path.join(BUNDLED_DATA_DIR, rel)
        if not os.path.exists(src):
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            print(f"[data] 快照播种: {rel} -> {DATA_DIR}", flush=True)
        except OSError as e:
            print(f"⚠️  快照播种失败 {rel}: {e}", flush=True)


def _resolve(rel):
    """读文件路径: 优先 DATA_DIR (新抓的数据), 没有则回退到仓库快照。"""
    p = os.path.join(DATA_DIR, rel)
    if os.path.exists(p):
        return p
    b = os.path.join(BUNDLED_DATA_DIR, rel)
    if os.path.exists(b):
        return b
    return p


def _data_source():
    """当前数据来源: live (DATA_DIR 有新数据) / snapshot (只有仓库快照) / none。"""
    if os.path.exists(os.path.join(DATA_DIR, "tvbox", "tvbox.json")):
        return "live"
    if os.path.exists(os.path.join(BUNDLED_DATA_DIR, "tvbox", "tvbox.json")):
        return "snapshot"
    return "none"


# 启动时 (import 时) 就播种一次: Vercel 上 api/index.py 只 import app,
# 不走 __main__, 所以播种不能只放在 __main__ 里。
_ensure_data_dir()

# ---------------- 抓取进度 (内存 + 磁盘持久化) ----------------
# 结构化进度字段 (2026-11-13 详细进度): stage/stage_index/stage_total 为
# 当前阶段 (共 8 个), percent 0-100 单调递增, current 为当前在干什么,
# done_count/total_count 为当前阶段已完成/总数, elapsed_seconds 为已用秒,
# last_log_at 为最后一条日志时间。注意 "done" 仍是布尔值 (抓取是否结束),
# 所以阶段计数用 done_count/total_count, 避免覆盖它。
# 2026-11-16 持久化 (刷新/重启不再重跑): 状态同时写到
# DATA_DIR/update_state.json; 启动时读回, 上次 running 的进程若已死,
# 标记 interrupted, 绝不悄悄重开。日志每条带时间戳 {"ts": ISO, "msg": str}。
# last_activity_at 为最后一次日志或进度变化的时间, 供前端判断"卡住"。
_update_lock = threading.Lock()
_update_state = {
    "running": False, "done": False, "started_at": None, "finished_at": None,
    "logs": [], "tvbox_sites": None, "book_sources": None, "error": None,
    "stage": None, "stage_index": 0, "stage_total": 8, "percent": 0,
    "current": None, "done_count": None, "total_count": None,
    "elapsed_seconds": 0, "last_log_at": None,
    "interrupted": False, "interrupted_at": None, "interrupted_reason": None,
    "last_activity_at": None,
    "run_id": None,  # 本轮抓取唯一标识 (刷新前后对一下即知是不是同一轮)
}
_STATE_FILE = "update_state.json"
_STATE_SAVE_INTERVAL_S = 2.0  # 最多每 2 秒落盘一次
_last_state_save = [0.0]
# 卡住判定阈值 (秒): 抓取进行中但超过这么久无日志也无进度变化即视为卡住。
# 环境变量 STALL_AFTER_SECONDS 可覆盖 (默认 300, 测试时可设小)。
STALL_AFTER_SECONDS = int(os.environ.get("STALL_AFTER_SECONDS", "300") or 300)
# 调度器每小时检查一次的间隔 (秒): 环境变量 SCHEDULER_CHECK_SECONDS 可覆盖
# (默认 3600; 仅供测试时缩短, 正常部署不要改)。
SCHEDULER_CHECK_SECONDS = int(os.environ.get("SCHEDULER_CHECK_SECONDS", "3600") or 3600)


def _elapsed_seconds():
    """从 started_at 算已用秒 (抓取中/已结束都可用)。"""
    started = _update_state.get("started_at")
    if not started:
        return 0
    try:
        dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
    except Exception:
        return 0
    end = _update_state.get("finished_at")
    if end and not _update_state.get("running"):
        try:
            dt_end = datetime.fromisoformat(end.replace("Z", "+00:00"))
            return max(0, int((dt_end - dt).total_seconds()))
        except Exception:
            pass
    return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _state_file_path():
    return os.path.join(DATA_DIR, _STATE_FILE)


def _save_update_state(force=False):
    """把 _update_state 落盘到 DATA_DIR/update_state.json (原子写)。
    节流: 最多每 2 秒写一次, 除非 force。写失败 (如只读文件系统) 只打日志,
    不影响抓取本身。"""
    now_mono = time.monotonic()
    if not force and now_mono - _last_state_save[0] < _STATE_SAVE_INTERVAL_S:
        return
    _last_state_save[0] = now_mono
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = _state_file_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_update_state, f, ensure_ascii=False)
        os.replace(tmp, _state_file_path())
    except OSError as e:
        print(f"⚠️  抓取状态落盘失败: {e}", flush=True)


def _load_update_state():
    """启动时读回上次的抓取状态。
    若上次标记为 running, 说明进程在抓取中途死了 (服务重启/休眠被杀),
    标记 interrupted 并写明中断时间与进度, 绝不悄悄重开;
    其它状态 (完成/出错) 原样恢复, 供前端展示。"""
    try:
        with open(_state_file_path(), encoding="utf-8") as f:
            saved = json.load(f)
    except Exception:
        return
    if not isinstance(saved, dict):
        return
    # 只接受已知字段, 防止旧文件/脏数据污染
    for k in list(_update_state.keys()):
        if k in saved:
            _update_state[k] = saved[k]
    # 兼容: 旧版日志是裸字符串, 统一转成带时间戳的对象
    logs = _update_state.get("logs") or []
    fixed = []
    for entry in logs:
        if isinstance(entry, dict) and "msg" in entry:
            fixed.append(entry)
        else:
            fixed.append({"ts": _update_state.get("last_log_at") or _now_iso(),
                          "msg": str(entry)})
    _update_state["logs"] = fixed[-300:]
    if _update_state.get("running"):
        started = _update_state.get("started_at") or "未知时间"
        pct = _update_state.get("percent") or 0
        stage = _update_state.get("stage") or "未知阶段"
        _update_state["running"] = False
        _update_state["done"] = False
        _update_state["interrupted"] = True
        _update_state["interrupted_at"] = _now_iso()
        _update_state["interrupted_reason"] = (
            f"上次抓取 ({_update_state.get('run_id') or '未知轮次'}) 在 "
            f"{_update_state['interrupted_at']} 被服务重启中断, "
            f"未完成 (开始于 {started}, 进度 {pct}%, 阶段: {stage})。"
            f"数据未丢失, 请点「手动更新抓取」重新开始。")
        _update_state["finished_at"] = _update_state["interrupted_at"]
        _update_state["logs"].append(
            {"ts": _now_iso(), "msg": _update_state["interrupted_reason"]})
        _update_state["logs"] = _update_state["logs"][-300:]
        _update_state["last_log_at"] = _now_iso()
        _update_state["last_activity_at"] = _update_state["last_log_at"]
        print(f"[state] { _update_state['interrupted_reason']}", flush=True)
    _save_update_state(force=True)


# 启动时 (import 时) 读回上次状态: Vercel 上 api/index.py 只 import app,
# 不走 __main__, 所以恢复不能只放在 __main__ 里。
_load_update_state()

# 进程启动标识 (2026-11-16 循环诊断): 每次进程启动生成一个 boot id,
# 启动日志行带数据状态, Render 日志里一眼能看到重启 (及重启后是否重抓)。
# 重启原因目前未确认 (应用日志里没有 OOM 字样; 候选: 内存、健康检查超时、
# 平台回收), 靠这行 + 抓取中的内存日志下次定位。
BOOT_ID = secrets.token_hex(4)


def _boot_log_line():
    """启动时打一行带 boot id 的日志: 数据状态 + 上次更新时间。
    在 _read_status/_data_source 定义之后调一次 (python app.py 只跑一次,
    不重复打印)。"""
    st = _read_status()
    print(f"[boot {BOOT_ID}] 服务启动 数据状态: {_data_source()} "
          f"上次更新: {st.get('last_update') or '无'} "
          f"下次更新: {st.get('next_update') or '无'}", flush=True)


def _status_path():
    return _resolve("status.json")


def _read_status():
    try:
        with open(_status_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_update": None, "next_update": None,
                "interval_days": UPDATE_INTERVAL_DAYS,
                "tvbox_sites": None, "book_sources": None}


def _is_expired():
    st = _read_status()
    last = st.get("last_update")
    if not last:
        return True
    try:
        dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - dt > timedelta(days=UPDATE_INTERVAL_DAYS)
    except Exception:
        return True


def _data_missing():
    return not (os.path.exists(_resolve(os.path.join("tvbox", "tvbox.json")))
                and os.path.exists(_resolve(os.path.join("books", "books.json"))))


# 启动日志行 (import 时打一次; 放在 _read_status/_data_source 定义之后)。
_boot_log_line()


def _run_fetch():
    """后台抓取 (只能一个实例在跑), 更新 _update_state。"""
    with _update_lock:
        if _update_state["running"]:
            return False
        _update_state.update(running=True, done=False, logs=[], error=None,
                             started_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                             finished_at=None, tvbox_sites=None, book_sources=None,
                             stage="开始抓取", stage_index=0, stage_total=8, percent=0,
                             current="正在启动抓取…", done_count=None, total_count=None,
                             elapsed_seconds=0, last_log_at=None,
                             interrupted=False, interrupted_at=None,
                             interrupted_reason=None, last_activity_at=None,
                             run_id=("run-" + datetime.now(timezone.utc).strftime("%m%d-%H%M%S")
                                     + "-" + secrets.token_hex(3)))
        _save_update_state(force=True)
    def _progress(msg):
        # 同时接受字符串日志和结构化进度 dict (fetcher._emit 发 dict, _log 发 str)
        # fetcher.py 的 _log/_emit 契约不动: 这里只在 app.py 侧给日志加时间戳。
        now = _now_iso()
        if isinstance(msg, dict) and msg.get("type") == "progress":
            _update_state.update(
                stage=msg.get("stage"), stage_index=msg.get("stage_index", 0),
                stage_total=msg.get("stage_total", 8),
                percent=msg.get("percent", _update_state["percent"]),
                current=msg.get("current"),
                done_count=msg.get("done"), total_count=msg.get("total"))
            _update_state["last_activity_at"] = now
            _update_state["elapsed_seconds"] = _elapsed_seconds()
            _save_update_state()
            return
        _update_state["logs"].append({"ts": now, "msg": str(msg)})
        # 日志只留最近 300 行
        if len(_update_state["logs"]) > 300:
            _update_state["logs"] = _update_state["logs"][-300:]
        _update_state["last_log_at"] = now
        _update_state["last_activity_at"] = now
        _update_state["elapsed_seconds"] = _elapsed_seconds()
        _save_update_state()
    try:
        tv_meta, books_meta = fetcher.fetch_all(DATA_DIR, progress=_progress,
                                                interval_days=UPDATE_INTERVAL_DAYS)
        _update_state["tvbox_sites"] = tv_meta.get("site_count")
        _update_state["book_sources"] = books_meta.get("book_count")
        # 2026-11-16 循环根因修复: fetch_all 现在阶段失败也收尾写 status.json
        # (带 error/partial), 这里把失败透传到前端, 别只显示"正在抓取"/"完成"。
        _st = _read_status()
        if _st.get("error"):
            _err_txt = "; ".join(f"{k}: {v}" for k, v in _st["error"].items())
            _update_state["error"] = f"部分阶段失败: {_err_txt}"
            _update_state["stage"] = "部分失败"
            _update_state["current"] = (
                f"抓取完成（部分失败：{', '.join(_st['error'])}）: "
                f"TVBox {_update_state['tvbox_sites']} 站 / 书源 {_update_state['book_sources']} 个")
            _progress(f"抓取部分失败: {_err_txt}")
    except Exception as e:  # noqa: BLE001
        _update_state["error"] = str(e)
        _progress(f"抓取出错: {e}")
    finally:
        _update_state["running"] = False
        _update_state["done"] = True
        _update_state["finished_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _update_state["elapsed_seconds"] = _elapsed_seconds()
        if not _update_state["error"]:
            # 成功完成: percent 置 100 (fetcher 正常结束时已发 100, 这里是兜底)
            _update_state["percent"] = 100
            _update_state["stage"] = "完成"
            _update_state["current"] = (
                f"抓取完成：TVBox {_update_state['tvbox_sites']} 站 / "
                f"书源 {_update_state['book_sources']} 个")
        _save_update_state(force=True)
    return True


def _scheduler_loop():
    """后台线程: 启动时数据缺失或已过期就自动抓一次 (从 0 开始抓是正常流程:
    data/ 是抓取输出, 不是输入; 不传 data/ 不影响流程), 之后每小时检查一次,
    到期再抓。
    2026-11-16 循环根因 (已修正): 旧版 fetch_all 没有 try/except, 任一阶段
    抛异常都会跳过 status.json 的写出, 调度器每小时重开一轮, 每轮又在同一处
    抛掉 —— 日志上就是"一直在抓", 像个死循环, 实际每轮都在书源阶段提前死掉。
    现已修: 阶段失败只记 error, 收尾必写 status.json (last_update=now),
    调度器 7 天内不再重开失败的轮次 (失败后想重试请点手动更新)。
    上一轮“数据完全缺失时永不自动抓 + 把 data/ 快照传进仓库”的诊断与建议
    是错的, 2026-11-16 用户纠正后已推翻: data/ 是输出、永远不进仓库;
    且用户确认服务开了保活、这几天没有重启过, “重启→清空→重抓”的循环
    解释不成立。真正的循环机制是上面说的“阶段抛异常→没写 status.json→
    每小时重开”, 详见 README“抓取循环”一节。
    Vercel 上默认不启动 (Serverless 函数返回后后台线程会被冻结,
    定时抓取不可靠; 且全量抓取 4-7 分钟远超函数时长上限)。
    如确需在 Vercel 上抓取, 设 VERCEL_FETCH_ENABLED=1 自担超时风险。"""
    if IS_VERCEL and not VERCEL_FETCH_ENABLED:
        print("[scheduler] Vercel 环境: 定时抓取已关闭 (Serverless 函数返回后后台"
              "线程会被冻结, 定时抓取不可靠)。更新数据请在 Render/VPS 上运行。",
              flush=True)
        return
    # 等 Flask 启动完成再抓, 避免阻塞首个请求
    time.sleep(3)
    if _data_source() == "none" or _is_expired():
        print("[scheduler] 数据缺失或已过期, 开始首次抓取", flush=True)
        _run_fetch()
    while True:
        time.sleep(SCHEDULER_CHECK_SECONDS)  # 默认每小时检查一次是否到期
        if _is_expired() and not _update_state["running"]:
            print("[scheduler] 到达更新间隔, 开始定时抓取", flush=True)
            _run_fetch()


# ---------------- 页面与 API ----------------
def _authed():
    return session.get("authed") is True


@app.route("/")
def index():
    return render_template("index.html")


# 登录限流 (2026-11-13 并入他方口径: 对方有登录限流, 我方原来没有):
# 同一 IP 10 分钟内失败 5 次即锁 10 分钟 (429); 成功登录清零。
# 内存字典, 进程重启即清零 (单进程部署够用; 多进程部署需改共享存储)。
_LOGIN_FAILS = {}  # ip -> [失败次数, 首次失败时间戳]
_LOGIN_MAX_FAILS = 5
_LOGIN_WINDOW_S = 600


def _login_locked(ip):
    rec = _LOGIN_FAILS.get(ip)
    if not rec:
        return False
    n, first = rec
    if n >= _LOGIN_MAX_FAILS and time.time() - first < _LOGIN_WINDOW_S:
        return True
    if time.time() - first >= _LOGIN_WINDOW_S:
        _LOGIN_FAILS.pop(ip, None)
    return False


def _login_fail(ip):
    rec = _LOGIN_FAILS.get(ip)
    if rec and time.time() - rec[1] < _LOGIN_WINDOW_S:
        _LOGIN_FAILS[ip] = [rec[0] + 1, rec[1]]
    else:
        _LOGIN_FAILS[ip] = [1, time.time()]


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    ip = request.remote_addr or "unknown"
    if _login_locked(ip):
        return jsonify({"ok": False, "error": "尝试过多, 请 10 分钟后再试"}), 429
    # 常量时间比较 (防时序攻击; 原来是 == 直接比较)
    pw = data.get("password") or ""
    if isinstance(pw, str) and hmac.compare_digest(pw, ACCESS_PASSWORD):
        _LOGIN_FAILS.pop(ip, None)
        session["authed"] = True
        session.permanent = True
        return jsonify({"ok": True})
    _login_fail(ip)
    return jsonify({"ok": False, "error": "密码错误"}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop("authed", None)
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    if not _authed():
        return jsonify({"ok": False, "error": "未登录"}), 401
    st = _read_status()
    # 数量以实际文件为准, status.json 为辅
    def _count(path, key):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            return len(d[key]) if isinstance(d, dict) else len(d)
        except Exception:
            return None
    tv_count = _count(_resolve(os.path.join("tvbox", "tvbox.json")), "sites")
    adult_count = _count(_resolve(os.path.join("tvbox", "tvbox_b.json")), "sites")
    book_count = None
    try:
        with open(_resolve(os.path.join("books", "books.json")), encoding="utf-8") as f:
            book_count = len(json.load(f))
    except Exception:
        pass
    return jsonify({
        "ok": True,
        "last_update": st.get("last_update"),
        "next_update": st.get("next_update"),
        "interval_days": st.get("interval_days", UPDATE_INTERVAL_DAYS),
        "tvbox_sites": tv_count if tv_count is not None else st.get("tvbox_sites"),
        "adult_sites": adult_count,
        "book_sources": book_count if book_count is not None else st.get("book_sources"),
        "updating": _update_state["running"],
        # 最近一轮若有阶段失败, status.json 带 error/partial, 前端可据此提示
        "fetch_error": st.get("error"),
        "partial": st.get("partial", False),
        # Vercel 相关: 前端据此显示提示 / 禁用手动更新按钮
        "vercel": IS_VERCEL,
        "data_source": _data_source(),  # live | snapshot | none
        "fetch_enabled": (not IS_VERCEL) or VERCEL_FETCH_ENABLED,
    })


@app.route("/api/files")
def api_files():
    if not _authed():
        return jsonify({"ok": False, "error": "未登录"}), 401
    files = []
    for rel in ["tvbox/tvbox.json", "tvbox/tvbox_b.json", "tvbox/meta.json",
                "books/books.json", "books/meta.json", "status.json"]:
        p = _resolve(rel)
        if os.path.exists(p):
            s = os.stat(p)
            files.append({"name": rel, "size": s.st_size,
                          "mtime": datetime.fromtimestamp(s.st_mtime, timezone.utc)
                                   .isoformat().replace("+00:00", "Z")})
    # 依赖包: data/tvbox/assets/ 下的 spider jar / type-3 api/ext 文件
    # (只读 DATA_DIR 里的, 仓库快照里没有; 按文件名排序)
    assets_dir = os.path.join(DATA_DIR, "tvbox", "assets")
    if os.path.isdir(assets_dir):
        for fn in sorted(os.listdir(assets_dir)):
            fp = os.path.join(assets_dir, fn)
            if os.path.isfile(fp) and not fn.endswith(".tmp"):
                st = os.stat(fp)
                files.append({"name": f"tvbox/assets/{fn}", "size": st.st_size,
                              "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc)
                                       .isoformat().replace("+00:00", "Z")})
    return jsonify({"ok": True, "files": files})


ALLOWED_DOWNLOADS = {
    "tvbox.json": os.path.join("tvbox", "tvbox.json"),
    "tvbox-b.json": os.path.join("tvbox", "tvbox_b.json"),
    "tvbox-meta.json": os.path.join("tvbox", "meta.json"),
    "books.json": os.path.join("books", "books.json"),
    "books-meta.json": os.path.join("books", "meta.json"),
    "status.json": "status.json",
}


@app.route("/api/download/<name>")
def api_download(name):
    if not _authed():
        return jsonify({"ok": False, "error": "未登录"}), 401
    if name.startswith("assets-"):
        # 依赖包单文件下载: assets-<文件名>, 只允许 assets 目录内的文件
        fn = os.path.basename(name[len("assets-"):])
        if not fn or fn in (".", ".."):
            abort(404)
        p = os.path.join(DATA_DIR, "tvbox", "assets", fn)
        if not os.path.isfile(p):
            abort(404)
        return send_file(p, as_attachment=True, download_name=fn,
                         mimetype="application/octet-stream")
    rel = ALLOWED_DOWNLOADS.get(name)
    if not rel:
        abort(404)
    p = _resolve(rel)
    if not os.path.exists(p):
        abort(404)
    return send_file(p, as_attachment=True, download_name=name,
                     mimetype="application/json")


@app.route("/api/download-all")
def api_download_all():
    if not _authed():
        return jsonify({"ok": False, "error": "未登录"}), 401
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # 优先 DATA_DIR 的新数据, 没有则用仓库快照 (Vercel 冷启动 / /tmp 被清空时)
        for rel in ["tvbox/tvbox.json", "tvbox/tvbox_b.json", "tvbox/meta.json",
                    "tvbox/failures.json",
                    "books/books.json", "books/meta.json", "status.json"]:
            full = _resolve(rel)
            if os.path.exists(full):
                z.write(full, rel)
        # 依赖包: tvbox/assets/ 整个文件夹打进去 (spider jar + type-3 api/ext);
        # 检查点 checkpoint_*.json 是内部中间结果, 不打进去
        assets_dir = os.path.join(DATA_DIR, "tvbox", "assets")
        if os.path.isdir(assets_dir):
            for fn in sorted(os.listdir(assets_dir)):
                fp = os.path.join(assets_dir, fn)
                if os.path.isfile(fp) and not fn.endswith(".tmp"):
                    z.write(fp, f"tvbox/assets/{fn}")
    buf.seek(0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return send_file(buf, as_attachment=True,
                     download_name=f"tvbox-legado-sources-{stamp}.zip",
                     mimetype="application/zip")


def _zip_assets(z, prefix):
    """把 DATA_DIR/tvbox/assets/ 下的依赖包打进 zip (prefix 为 zip 内路径前缀)。
    检查点 checkpoint_*.json 与 .tmp 不打进去, 与 download-all 一致。"""
    assets_dir = os.path.join(DATA_DIR, "tvbox", "assets")
    if not os.path.isdir(assets_dir):
        return
    for fn in sorted(os.listdir(assets_dir)):
        fp = os.path.join(assets_dir, fn)
        if os.path.isfile(fp) and not fn.endswith(".tmp"):
            z.write(fp, f"{prefix}{fn}")


# 文件夹打包下载 (2026-11-17 用户要求: 文件列表按文件夹展示,
# 点文件夹就把该文件夹打成压缩包下载)。只允许这三个固定文件夹,
# 其余路径一律 404, 不接受用户传入的任意路径。
DOWNLOAD_DIRS = {
    "tvbox": "tvbox",
    "books": "books",
    "tvbox/assets": "tvbox-assets",
}


@app.route("/api/download-dir/<path:dirname>")
def api_download_dir(dirname):
    if not _authed():
        return jsonify({"ok": False, "error": "未登录"}), 401
    if dirname not in DOWNLOAD_DIRS:
        abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if dirname == "tvbox":
            # TVBox 文件夹: json 放 zip 根目录, 依赖包放 assets/ 子目录
            for rel in ["tvbox/tvbox.json", "tvbox/tvbox_b.json",
                        "tvbox/meta.json", "tvbox/failures.json"]:
                full = _resolve(rel)
                if os.path.exists(full):
                    z.write(full, os.path.basename(rel))
            _zip_assets(z, "assets/")
        elif dirname == "books":
            for rel in ["books/books.json", "books/meta.json"]:
                full = _resolve(rel)
                if os.path.exists(full):
                    z.write(full, os.path.basename(rel))
        elif dirname == "tvbox/assets":
            # 依赖包文件夹: jar 等文件直接放 zip 根目录
            _zip_assets(z, "")
    buf.seek(0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return send_file(buf, as_attachment=True,
                     download_name=f"{DOWNLOAD_DIRS[dirname]}-{stamp}.zip",
                     mimetype="application/zip")


@app.route("/api/update", methods=["POST"])
def api_update():
    if not _authed():
        return jsonify({"ok": False, "error": "未登录"}), 401
    if IS_VERCEL and not VERCEL_FETCH_ENABLED:
        # Vercel Serverless 函数有执行时长上限 (免费版约 10-60 秒),
        # 全量抓取要 4-7 分钟, 且函数返回后后台线程会被冻结, 跑不完。
        # 正确做法见 README"部署到 Vercel": 在本地/Render 跑 fetcher.py,
        # 把 data/ 提交到仓库后重新部署, 快照即更新。
        return jsonify({"ok": False,
                        "error": "Vercel 上无法在一次请求内完成全量抓取 (需 4-7 分钟, 超过函数时长上限, "
                                 "且后台线程在函数返回后会被冻结)。请在本地或 Render 上跑 python3 fetcher.py, "
                                 "把 data/ 提交到 GitHub 后重新部署, 快照即更新。"}), 503
    if _update_state["running"]:
        return jsonify({"ok": False, "error": "正在抓取中, 请稍候",
                        "running": True}), 409
    threading.Thread(target=_run_fetch, daemon=True).start()
    return jsonify({"ok": True, "message": "已开始抓取"})


@app.route("/api/update/status")
def api_update_status():
    if not _authed():
        return jsonify({"ok": False, "error": "未登录"}), 401
    if _update_state["running"]:
        _update_state["elapsed_seconds"] = _elapsed_seconds()
    # 卡住判定: 进行中但超过 STALL_AFTER_SECONDS 秒无日志也无进度变化
    stalled = False
    stalled_seconds = 0
    if _update_state["running"]:
        last_act = _update_state.get("last_activity_at") or _update_state.get("started_at")
        if last_act:
            try:
                dt = datetime.fromisoformat(last_act.replace("Z", "+00:00"))
                stalled_seconds = max(
                    0, int((datetime.now(timezone.utc) - dt).total_seconds()))
                stalled = stalled_seconds >= STALL_AFTER_SECONDS
            except Exception:
                pass
    return jsonify({"ok": True, **_update_state,
                    "stalled": stalled, "stalled_seconds": stalled_seconds,
                    "stall_after_seconds": STALL_AFTER_SECONDS})


if __name__ == "__main__":
    _ensure_data_dir()
    if not IS_VERCEL or VERCEL_FETCH_ENABLED:
        threading.Thread(target=_scheduler_loop, daemon=True).start()
    print(f"启动: http://0.0.0.0:{PORT}  间隔 {UPDATE_INTERVAL_DAYS} 天  数据目录 {DATA_DIR}"
          + (" (Vercel 模式: 只读快照, 定时/手动抓取已关闭)" if IS_VERCEL and not VERCEL_FETCH_ENABLED else ""),
          flush=True)
    app.run(host="0.0.0.0", port=PORT)
