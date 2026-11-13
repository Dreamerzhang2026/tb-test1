#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TVBox + Legado 源聚合器 Web 入口 (Flask)
环境变量:
  ACCESS_PASSWORD      访问密码, 默认 change-me (未改会打印警告)
  UPDATE_INTERVAL_DAYS 定时抓取间隔天数, 默认 7
  PORT                 监听端口, 默认 8000
  DATA_DIR             数据目录, 默认 data
  SECRET_KEY           会话密钥, 不设则每次启动随机生成
启动: python app.py   (或 gunicorn / docker)
"""
import io
import json
import os
import secrets
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
DATA_DIR = os.environ.get("DATA_DIR", "data")

if ACCESS_PASSWORD == "change-me":
    print("⚠️  警告: ACCESS_PASSWORD 未修改, 仍为默认值 change-me, 请在环境变量中改成强密码!",
          flush=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

# ---------------- 抓取进度 (内存) ----------------
_update_lock = threading.Lock()
_update_state = {
    "running": False, "done": False, "started_at": None, "finished_at": None,
    "logs": [], "tvbox_sites": None, "book_sources": None, "error": None,
}


def _status_path():
    return os.path.join(DATA_DIR, "status.json")


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
    return not (os.path.exists(os.path.join(DATA_DIR, "tvbox", "tvbox.json"))
                and os.path.exists(os.path.join(DATA_DIR, "books", "books.json")))


def _run_fetch():
    """后台抓取 (只能一个实例在跑), 更新 _update_state。"""
    with _update_lock:
        if _update_state["running"]:
            return False
        _update_state.update(running=True, done=False, logs=[], error=None,
                             started_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                             finished_at=None, tvbox_sites=None, book_sources=None)
    def _progress(msg):
        _update_state["logs"].append(msg)
        # 日志只留最近 300 行
        if len(_update_state["logs"]) > 300:
            _update_state["logs"] = _update_state["logs"][-300:]
    try:
        tv_meta, books_meta = fetcher.fetch_all(DATA_DIR, progress=_progress,
                                                interval_days=UPDATE_INTERVAL_DAYS)
        _update_state["tvbox_sites"] = tv_meta.get("site_count")
        _update_state["book_sources"] = books_meta.get("book_count")
    except Exception as e:  # noqa: BLE001
        _update_state["error"] = str(e)
        _progress(f"抓取出错: {e}")
    finally:
        _update_state["running"] = False
        _update_state["done"] = True
        _update_state["finished_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return True


def _scheduler_loop():
    """后台线程: 启动时缺失/过期先抓一次, 之后每间隔检查一次。"""
    # 等 Flask 启动完成再抓, 避免阻塞首个请求
    time.sleep(3)
    if _data_missing() or _is_expired():
        print("[scheduler] 数据缺失或已过期, 开始首次抓取", flush=True)
        _run_fetch()
    while True:
        time.sleep(3600)  # 每小时检查一次是否到期
        if _is_expired() and not _update_state["running"]:
            print("[scheduler] 到达更新间隔, 开始定时抓取", flush=True)
            _run_fetch()


# ---------------- 页面与 API ----------------
def _authed():
    return session.get("authed") is True


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    if data.get("password") == ACCESS_PASSWORD:
        session["authed"] = True
        session.permanent = True
        return jsonify({"ok": True})
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
    tv_count = _count(os.path.join(DATA_DIR, "tvbox", "tvbox.json"), "sites")
    book_count = None
    try:
        with open(os.path.join(DATA_DIR, "books", "books.json"), encoding="utf-8") as f:
            book_count = len(json.load(f))
    except Exception:
        pass
    return jsonify({
        "ok": True,
        "last_update": st.get("last_update"),
        "next_update": st.get("next_update"),
        "interval_days": st.get("interval_days", UPDATE_INTERVAL_DAYS),
        "tvbox_sites": tv_count if tv_count is not None else st.get("tvbox_sites"),
        "book_sources": book_count if book_count is not None else st.get("book_sources"),
        "updating": _update_state["running"],
    })


@app.route("/api/files")
def api_files():
    if not _authed():
        return jsonify({"ok": False, "error": "未登录"}), 401
    files = []
    for rel in ["tvbox/tvbox.json", "tvbox/meta.json",
                "books/books.json", "books/meta.json", "status.json"]:
        p = os.path.join(DATA_DIR, rel)
        if os.path.exists(p):
            s = os.stat(p)
            files.append({"name": rel, "size": s.st_size,
                          "mtime": datetime.fromtimestamp(s.st_mtime, timezone.utc)
                                   .isoformat().replace("+00:00", "Z")})
    return jsonify({"ok": True, "files": files})


ALLOWED_DOWNLOADS = {
    "tvbox.json": os.path.join("tvbox", "tvbox.json"),
    "tvbox-meta.json": os.path.join("tvbox", "meta.json"),
    "books.json": os.path.join("books", "books.json"),
    "books-meta.json": os.path.join("books", "meta.json"),
    "status.json": "status.json",
}


@app.route("/api/download/<name>")
def api_download(name):
    if not _authed():
        return jsonify({"ok": False, "error": "未登录"}), 401
    rel = ALLOWED_DOWNLOADS.get(name)
    if not rel:
        abort(404)
    p = os.path.join(DATA_DIR, rel)
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
        for root, _dirs, filenames in os.walk(DATA_DIR):
            for fn in filenames:
                full = os.path.join(root, fn)
                arc = os.path.relpath(full, DATA_DIR)
                z.write(full, arc)
    buf.seek(0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return send_file(buf, as_attachment=True,
                     download_name=f"tvbox-legado-sources-{stamp}.zip",
                     mimetype="application/zip")


@app.route("/api/update", methods=["POST"])
def api_update():
    if not _authed():
        return jsonify({"ok": False, "error": "未登录"}), 401
    if _update_state["running"]:
        return jsonify({"ok": False, "error": "正在抓取中, 请稍候",
                        "running": True}), 409
    threading.Thread(target=_run_fetch, daemon=True).start()
    return jsonify({"ok": True, "message": "已开始抓取"})


@app.route("/api/update/status")
def api_update_status():
    if not _authed():
        return jsonify({"ok": False, "error": "未登录"}), 401
    return jsonify({"ok": True, **_update_state})


if __name__ == "__main__":
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    print(f"启动: http://0.0.0.0:{PORT}  间隔 {UPDATE_INTERVAL_DAYS} 天  数据目录 {DATA_DIR}",
          flush=True)
    app.run(host="0.0.0.0", port=PORT)
