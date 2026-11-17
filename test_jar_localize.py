#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地验证: 2026-11-17 jar 本地化 (用户纠正: assets 不是备份, JSON 的 jar
字段必须指向本地 ./assets/<文件>, 否则 assets 里的文件没人用、等于白下)。

只用桩和本地临时文件, 不跑真实全量抓取、不联网（除 test_download_asset_reuse
外全部离线；reuse 测试只读本地已存在的文件，不发请求）。
用法: 在项目根目录运行 python3 tests/test_jar_localize.py
覆盖:
  ① _parse_jar_md5: ;md5;HEX / ;HEX / 无后缀 / 非 hex 尾巴 / 非字符串
  ② _asset_filename: 命名规则与 _download_asset 一致（prefix_hash_basename，
     无扩展名补 .bin，.png 伪装 jar 保留 .png）
  ③ _download_asset 复用已存在的文件（不断点、不联网）
  ④ _is_jar_bytes: PK 头判定（真 jar / 空文件 / HTML / 不存在）
  ⑤ _localize_site_jars: 远程 jar 改写（md5 命中保留后缀 / 失配去后缀 / 无 md5）、
     已本地不动、相对路径按 _base_dir 解析、相对路径无基准保留、下载失败保留、
     非 jar 内容不改写且删文件、api/ext 原样不动、同一 URL 多站只下一份
"""
import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fetcher  # noqa: E402

FAKE_JAR = b"PK\x03\x04" + b"\x00" * 200
FAKE_JAR2 = b"PK\x03\x04" + b"\x11" * 100
HTML = b"<html><body>404 Not Found</body></html>"


def _md5(b):
    return hashlib.md5(b).hexdigest()


def _sha256(b):
    return hashlib.sha256(b).hexdigest()


def test_parse_jar_md5():
    p = fetcher._parse_jar_md5
    url = "https://raw.githubusercontent.com/cyao2q/files/master/jar/Rabbit.jar"
    md5 = "BC9F4388C057279D6EFDF338BC236A1E"
    assert p(url) == (url, None), p(url)
    assert p(url + ";md5;" + md5) == (url, md5), p(url + ";md5;" + md5)
    # ;HEX（无 md5; 标签，如 ...#.png;3130dcf...）
    u2 = "https://cac.opple.com/yc-media/getFile?id=f61279a6953f4206bdcbd1c86a8804d3#.png"
    h2 = "3130dcf9527d5aeaa37f4eb30f08ca26"
    assert p(u2 + ";" + h2) == (u2, h2), p(u2 + ";" + h2)
    # 非 hex 尾巴：md5 为 None，clean 照旧从第一个 ; 截断（与 _strip_md5_suffix 一致）
    assert p(url + ";md5;nothex") == (url, None)
    assert p(url + ";v=2") == (url, None)
    assert p(None) == (None, None)
    assert p(123) == (123, None)
    print("① _parse_jar_md5 通过")


def test_asset_filename():
    f = fetcher._asset_filename
    u = "https://raw.githubusercontent.com/yw88075/tvbox/main/yw530.jar"
    tag = hashlib.sha256(u.encode()).hexdigest()[:8]
    assert f(u, "jar") == "jar_%s_yw530.jar" % tag, f(u, "jar")
    # 无扩展名补 .bin
    u2 = "https://cac.opple.com/yc-media/getFile?id=abc#.png"
    assert f(u2, "jar").endswith("_getFile.bin"), f(u2, "jar")
    # .png 伪装 jar 保留 .png
    u3 = "https://agit.ai/wzs1973/test/raw/branch/master/jar/999.png"
    assert f(u3, "jar").endswith("_999.png"), f(u3, "jar")
    # 同一 URL 同一 prefix 命名稳定
    assert f(u, "jar") == f(u, "jar")
    print("② _asset_filename 通过")


def test_download_asset_reuse(tmp):
    # 预先按命名规则放好文件，_download_asset 应直接复用、不发请求
    url = "https://example.com/spider.jar"
    name = fetcher._asset_filename(url, "jar")
    p = os.path.join(tmp, name)
    with open(p, "wb") as fh:
        fh.write(FAKE_JAR)
    rec = fetcher._download_asset(url, tmp, "jar")
    assert rec is not None and rec["file"] == name, rec
    assert rec["size_bytes"] == len(FAKE_JAR), rec
    assert rec["sha256"] == _sha256(FAKE_JAR), rec
    print("③ _download_asset 复用已存在文件通过（未联网）")


def test_is_jar_bytes(tmp):
    ok = os.path.join(tmp, "ok.jar")
    with open(ok, "wb") as fh:
        fh.write(FAKE_JAR)
    assert fetcher._is_jar_bytes(ok) is True
    empty = os.path.join(tmp, "empty.jar")
    open(empty, "wb").close()
    assert fetcher._is_jar_bytes(empty) is False
    html = os.path.join(tmp, "err.html")
    with open(html, "wb") as fh:
        fh.write(HTML)
    assert fetcher._is_jar_bytes(html) is False
    assert fetcher._is_jar_bytes(os.path.join(tmp, "nope.jar")) is False
    # PK\x05\x06（空 zip）也算
    e5 = os.path.join(tmp, "empty5.jar")
    with open(e5, "wb") as fh:
        fh.write(b"PK\x05\x06" + b"\x00" * 18)
    assert fetcher._is_jar_bytes(e5) is True
    print("④ _is_jar_bytes 通过")


def test_localize(tmp):
    assets = os.path.join(tmp, "assets")
    os.makedirs(assets, exist_ok=True)

    # 预置清单里的文件（复用路径，不下载）
    u_match = "https://raw.githubusercontent.com/cyao2q/files/master/jar/Rabbit.jar"
    md5_match = _md5(FAKE_JAR)
    f_match = fetcher._asset_filename(u_match, "jar")
    with open(os.path.join(assets, f_match), "wb") as fh:
        fh.write(FAKE_JAR)

    u_mismatch = "https://gitee.com/xhuban/getjar/raw/master/jar/custom_spider_cat.jar"
    f_mismatch = fetcher._asset_filename(u_mismatch, "jar")
    with open(os.path.join(assets, f_mismatch), "wb") as fh:
        fh.write(FAKE_JAR2)

    u_plain = "https://raw.githubusercontent.com/yw88075/tvbox/main/yw530.jar"
    f_plain = fetcher._asset_filename(u_plain, "jar")
    with open(os.path.join(assets, f_plain), "wb") as fh:
        fh.write(FAKE_JAR)

    u_png = "https://agit.ai/wzs1973/test/raw/branch/master/jar/999.png"
    f_png = fetcher._asset_filename(u_png, "jar")
    with open(os.path.join(assets, f_png), "wb") as fh:
        fh.write(FAKE_JAR)

    jar_files = [
        {"file": f_match, "url": u_match, "source": "spider",
         "size_bytes": len(FAKE_JAR), "sha256": _sha256(FAKE_JAR)},
        {"file": f_mismatch, "url": u_mismatch, "source": "type3",
         "size_bytes": len(FAKE_JAR2), "sha256": _sha256(FAKE_JAR2)},
        {"file": f_plain, "url": u_plain, "source": "type3",
         "size_bytes": len(FAKE_JAR), "sha256": _sha256(FAKE_JAR)},
        {"file": f_png, "url": u_png, "source": "type3",
         "size_bytes": len(FAKE_JAR), "sha256": _sha256(FAKE_JAR)},
    ]

    # 假下载：相对路径解析后 / 下载失败 / 非 jar 内容 三种桩
    calls = []

    def fake_download(url, dest_dir, prefix, progress=None):
        calls.append(url)
        if url == "https://cdn.jsdelivr.net/gh/some/repo@main/jar/x.jar":
            name = fetcher._asset_filename(url, prefix)
            with open(os.path.join(dest_dir, name), "wb") as fh:
                fh.write(FAKE_JAR)
            return {"file": name, "url": url, "size_bytes": len(FAKE_JAR),
                    "sha256": _sha256(FAKE_JAR)}
        if url == "https://example.com/bad.jar":
            name = fetcher._asset_filename(url, prefix)
            with open(os.path.join(dest_dir, name), "wb") as fh:
                fh.write(HTML)
            return {"file": name, "url": url, "size_bytes": len(HTML),
                    "sha256": _sha256(HTML)}
        return None  # 其余一律下载失败

    sites = [
        # md5 命中：保留后缀
        {"key": "S1", "jar": u_match + ";md5;" + md5_match,
         "api": "csp_AppRJ", "ext": {"url": "http://v.rbotv.cn"}},
        # md5 失配：去后缀
        {"key": "S2", "jar": u_mismatch + ";md5;" + _md5(b"other content"),
         "api": "csp_X"},
        # 无 md5： plain
        {"key": "S3", "jar": u_plain, "api": "csp_Y"},
        # 同一 URL 第二站：复用，不重复下载
        {"key": "S3b", "jar": u_plain, "api": "csp_Y2"},
        # .png 伪装 jar
        {"key": "S4", "jar": u_png + ";md5;" + md5_match, "api": "csp_Z"},
        # 已是本地
        {"key": "S5", "jar": "./assets/old.jar", "api": "csp_W"},
        # 无 jar
        {"key": "S6", "api": "csp_V"},
        # 相对路径 + _base_dir：解析后下载成功
        {"key": "S7", "jar": "./jar/x.jar", "api": "csp_U",
         "_base_dir": "https://cdn.jsdelivr.net/gh/some/repo@main/"},
        # 相对路径，无基准
        {"key": "S8", "jar": "./jar/y.jar", "api": "csp_T"},
        # 远程 jar，下载失败
        {"key": "S9", "jar": "https://example.com/missing.jar", "api": "csp_S"},
        # 远程 jar，下载到 HTML（非 jar）
        {"key": "S10", "jar": "https://example.com/bad.jar", "api": "csp_R"},
    ]
    stats = fetcher._localize_site_jars(sites, assets, jar_files,
                                        download_fn=fake_download)

    assert sites[0]["jar"] == "./assets/%s;md5;%s" % (f_match, md5_match), sites[0]
    assert sites[1]["jar"] == "./assets/%s" % f_mismatch, sites[1]
    assert sites[2]["jar"] == "./assets/%s" % f_plain, sites[2]
    assert sites[3]["jar"] == "./assets/%s" % f_plain, sites[3]
    assert sites[4]["jar"] == "./assets/%s;md5;%s" % (f_png, md5_match), sites[4]
    assert sites[5]["jar"] == "./assets/old.jar", sites[5]
    assert "jar" not in sites[6], sites[6]
    assert sites[7]["jar"].startswith("./assets/jar_") and sites[7]["jar"].endswith("_x.jar"), sites[7]
    assert sites[8]["jar"] == "./jar/y.jar", sites[8]
    assert sites[9]["jar"] == "https://example.com/missing.jar", sites[9]
    assert sites[10]["jar"] == "https://example.com/bad.jar", sites[10]
    # api/ext 原样不动
    assert sites[0]["api"] == "csp_AppRJ" and sites[0]["ext"] == {"url": "http://v.rbotv.cn"}
    # 统计
    assert stats["rewritten"] == 6, stats  # S1 S2 S3 S3b S4 S7
    assert stats["already_local"] == 1, stats
    assert len(stats["kept_remote"]) == 3, stats  # S8 S9 S10
    assert len(stats["md5_mismatched"]) == 1 and stats["md5_mismatched"][0]["key"] == "S2", stats
    kept_keys = {k["key"] for k in stats["kept_remote"]}
    assert kept_keys == {"S8", "S9", "S10"}, kept_keys
    # 非 jar 的下载文件已被删掉，不留坏文件
    bad_name = fetcher._asset_filename("https://example.com/bad.jar", "jar")
    assert not os.path.exists(os.path.join(assets, bad_name))
    # 同一 URL 只下载一次：fake_download 只被调了 3 次（S7/S9/S10 各 1 次）
    assert len(calls) == 3, calls
    # 新下载的 jar 已追加进 jar_files 清单
    assert any(j["url"] == "https://cdn.jsdelivr.net/gh/some/repo@main/jar/x.jar"
               for j in jar_files), jar_files
    print("⑤ _localize_site_jars 通过（改写 6 / 已本地 1 / 保留 3 / md5 失配 1）")


def main():
    tmp = tempfile.mkdtemp(prefix="jar_localize_")
    try:
        test_parse_jar_md5()
        test_asset_filename()
        test_download_asset_reuse(tmp)
        test_is_jar_bytes(tmp)
        test_localize(tmp)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n全部通过。")


if __name__ == "__main__":
    main()
