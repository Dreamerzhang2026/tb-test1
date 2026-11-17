"""冒烟测试: 书源入口 OOM 修复 (2026-11-17)。

1. _fetch_aoa_sources 只数条数、不解析、不保留 items (items 必须为空)。
2. fetch_text 超过 MAX_FETCH_BYTES 的响应按失败处理, 记"响应过大";
   正常小响应仍正常返回。
3. 内存上限: RSS 超过 MEM_LIMIT_MB 时书源主循环应收尾 (这里只验证
   _rss_mb 与阈值常量存在, 不真的跑满内存)。
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import fetcher

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)

class FakeResp:
    def __init__(self, status=200, text="{}", ctype="application/json"):
        self.status_code = status
        self.text = text
        self.headers = {"Content-Type": ctype}
        self.encoding = "utf-8"
    def iter_content(self, chunk_size=65536):
        data = self.text.encode("utf-8")
        for i in range(0, len(data), chunk_size):
            yield data[i:i + chunk_size]
    def close(self):
        pass

# ---- 1. aoaostar 只计数 ----
home = ('<html><body>'
        '<div>合集A</div>'
        '<a href="legado://import/bookSource?src=https://legado.aoaostar.com/sources/aaaa1111.json">访问直链</a>'
        '<a href="legado://import/bookSource?src=https://legado.aoaostar.com/sources/aaaa1111.json">一键导入</a>'
        '<div>合集B</div>'
        '<a href="legado://import/bookSource?src=https://legado.aoaostar.com/sources/bbbb2222.json">访问直链</a>'
        '<a href="legado://import/bookSource?src=https://legado.aoaostar.com/sources/bbbb2222.json">一键导入</a>'
        '</body></html>')
bundle_a = json.dumps([{"bookSourceUrl": f"https://s{i}.example.com", "bookSourceName": f"s{i}",
                        "ruleSearch": "x"} for i in range(5)])
bundle_b = json.dumps([{"bookSourceUrl": "https://only.example.com", "bookSourceName": "only",
                        "ruleSearch": "x"}])

def fake_get(url, **kw):
    if url == fetcher.AOA_BASE + "/":
        return FakeResp(200, home, "text/html")
    if "aaaa1111" in url:
        return FakeResp(200, bundle_a)
    if "bbbb2222" in url:
        return FakeResp(200, bundle_b)
    return FakeResp(404, "not found", "text/html")

orig_get = fetcher.requests.get
fetcher.requests.get = fake_get
try:
    bundles = fetcher._fetch_aoa_sources(progress=None)
    check("aoaostar 返回 2 个 bundle", len(bundles) == 2, str(bundles))
    counts = {b["hash"]: b["count"] for b in bundles}
    check("合集A 数出 5 条", counts.get("aaaa1111") == 5, str(counts))
    check("合集B 数出 1 条", counts.get("bbbb2222") == 1, str(counts))
    check("items 全部为空 (不保留)", all(b.get("items") == [] for b in bundles), str(bundles))
finally:
    fetcher.requests.get = orig_get

# ---- 2. fetch_text 大小上限 ----
orig_max = fetcher.MAX_FETCH_BYTES
fetcher.MAX_FETCH_BYTES = 100
fetcher._fail_reset()
tmp = __import__("tempfile").mkdtemp(prefix="oom-cap-test-")
fetcher._dead_hosts_init(tmp)
try:
    fetcher.requests.get = lambda url, **kw: FakeResp(200, "x" * 500)
    t, u, c = fetcher.fetch_text(["http://example.com/big.json"], progress=None)
    check("超大响应返回 None", (t, u, c) == (None, None, None), str((t, u, c)))
    check("超大响应记'响应过大'", fetcher._FAIL["by_kind"].get("响应过大", 0) == 1,
          str(fetcher._FAIL["by_kind"]))
    fetcher.requests.get = lambda url, **kw: FakeResp(200, '{"a": 1}')
    t, u, c = fetcher.fetch_text(["http://example.com/small.json"], progress=None)
    check("小响应正常返回", t == '{"a": 1}' and u == "http://example.com/small.json", str((t, u, c)))
finally:
    fetcher.requests.get = orig_get
    fetcher.MAX_FETCH_BYTES = orig_max

# ---- 3. 内存上限常量 ----
check("MEM_LIMIT_MB 默认 420", fetcher.MEM_LIMIT_MB == 420, str(fetcher.MEM_LIMIT_MB))
check("MAX_FETCH_BYTES 默认 20MB", fetcher.MAX_FETCH_BYTES == 20 * 1024 * 1024,
      str(fetcher.MAX_FETCH_BYTES))
rss = fetcher._rss_mb()
check("RSS 能读到", isinstance(rss, (int, float)) and rss > 0, str(rss))

print()
print("FAILED: %d" % len(fails) if fails else "全部通过。")
sys.exit(1 if fails else 0)
