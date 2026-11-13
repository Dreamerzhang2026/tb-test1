# TVBox + Legado 源聚合器

定时抓取全网公开的 TVBox 聚合源和 Legado（阅读 App）书源，清洗、去重后生成
`tvbox.json` / `books.json`，通过带密码的网页查看、下载（可打包成 ZIP），也可手动触发更新并实时看进度。

> **诚实说明 / 已知限制（必读）**
> - **大陆速度未实测**：本次抓取在新加坡的机器上完成，TVBox 测速是新加坡 VM 两轮实测 + 国内 CDN 启发式（jsDelivr / `.cn` / 国内云域名加分），不是大陆多点实测。配置文件下载快不等于播放不卡，播放取决于各 `sites.api` 指向的 CMS 站和 m3u8 源站带宽。请按下面“筛选标准”里的方法在大陆实测后自行调整上游（改 `fetcher.py` 里的 `TVBOX_SOURCES` / `LEGADO_SOURCES`）。
> - **不求快、只求质量**：本项目宁缺毋滥、多测几轮、多验证几遍，TVBox 只留 80–150 站、书源只留 100–200 个，测不过 / 搜不到的一律剔除，不凑数。
> - **书籍全不全无法仅凭清单判断**：聚合包覆盖广但混有重复 / 失效源，导入后请自行搜热门书、打开一章验证。
> - **上游随时可能失效**：某个上游抓不到时，程序不会编造假数据填充，缺失会如实写进 `data/*/meta.json` 的 `sources` 里（`ok: false` + 原因）。
> - **合规**：部分上游 README 声明仅供学习、禁止商用；请遵守当地法律和各源的使用声明，不要商用、不要在境内使用明确禁止的源。

## 功能

- 定时抓取：环境变量 `UPDATE_INTERVAL_DAYS`（默认 7 天），后台线程每小时检查一次是否到期；启动时数据缺失或过期也会先抓一次；`data/status.json` 记录上次 / 下次更新时间。
- TVBox 抓取（`fetcher.py`）：按优先级抓 hebijunge、TVboxorg、qist、Lightconer 单仓聚合、cluntop、gao，再加 4 个偏门补充上游（25175/tvyuan、102244653/dianshi.json、jinenge、niubihu1，见“筛选标准”）；每个上游 jsDelivr → ghproxy → raw 三通道回退，超时 12 秒；剥离行首 `//` 注释；`./jar` 等相对路径解析为绝对 URL；spider 剥掉 `;md5;…` 后缀再解析，jpg/png 伪装的 spider 原样保留；type=3 站不再要求 api 是 http jar，csp_ 类名 / py_ 模块 / drpy JS 都保留，校验方式改为上游 spider jar 多通道 HEAD/GET 200（jsDelivr 对 .jar 返回 403，须改走 raw 才能测）+ ext 若是 URL 则 HEAD/GET 能打开，按 quota 只加约 34 个偏门；成人过滤：关键词表作用于全部 TVBox 站（去重后、测速前）、type-3 挑选兜底、最终写文件前复核，并过滤 lives 频道名，2026-11-11 修复后重抓剔除 13 个成人向站（含 3d涩动漫.py、91动漫.py、Cam4直播.py），明细见 `data/tvbox/meta.json` 的 `adult_removed`；多仓索引 `{urls:[]}` 二次解析；按 key 去重后做两轮实测再截断到 120 站（80–150，约 70% 热门 + 30% 偏门）；保留 parses 全集；直播优先用 Lightconer TVBox-Sources。
- 书源抓取：优先抓 yck 聚合 `yck2026.fun/yuedu/shuyuans/json/id/1135.json`，再抓 luanmuc、suwa-ko、XIU2、cjj；逐条校验 HTTP 200 + JSON 解析 + 含 `bookSourceName` / `bookSourceUrl` / `ruleSearch`；只留 `bookSourceType==0` 文字小说；按关键词表过滤成人 / 短剧 / 有声 / 漫画（`PO18、海棠、御书屋、短剧、成人、色情、黄文、黄色、有声、听书、漫画` 等，单字“黄”不用作过滤词以免误删正常小说）；规则齐全 + 官网可打开 + 3 本书搜索抽查后，按通过率排序留 150 个（100–200，约 70% 热门 + 30% 偏门）；按 `bookSourceUrl` 去重。

## 筛选标准（为什么只留这些）

不求快、只求质量高：宁缺毋滥，多测几轮、多验证几遍。所有阈值和记录都写在 `fetcher.py` 里，每次抓取的结果写在 `data/tvbox/meta.json` / `data/books/meta.json` 里，可复核。

### TVBox：80–150 站，目标 120，约 70% 热门 + 30% 偏门

1. **候选池**：先抓 10 个上游（含 4 个偏门补充源）去重，得到候选池（本次约数百站），不直接按 Star 取前几名。
2. **第一轮实测**：对每个候选站的 `api` 做 HEAD/GET 计时——TTFB、总耗时、限 200KB 内下载算带宽（KB/s），CMS 站请求 `?ac=list` 看能否正常返回 vod 列表（正文含 `"list"` / `"class"` / `"vod` 等）。10 并发，每站超时 5 秒。
3. **门槛**：TTFB 建议 `< 1.5s`、总耗时 `< 3s`、能返回 vod 列表，三项都过才算通过。国内 CDN / 域名加分：jsDelivr、`.cn` 域名、阿里云 / 腾讯云等国内云域名在排序时减分（相当于约 200ms 优势）。
4. **第二轮复测**：分开取第一轮通过里热门前 110 名 + 偏门前 60 名复测（不只取全局速度前 160，否则偏门站会被热门站挤出复测池，导致 70/30 构成无偏门可选），两轮都通过才算最终通过，延迟和带宽取两轮中较差的一次（保守计分），确认不是偶发快。
5. **多次失败剔除**：本轮测速失败的 key 记入 `data/tvbox/failures.json` 累计，连续失败 ≥ 2 次的直接剔除，哪怕本轮偶然通过也不留。
6. **构成**：按延迟 + 带宽排序，先取热门前 84 名、偏门前 36 名（70/30），哪边不够用另一边补；通过数不足 80 时才从一轮通过的里按分数补到 80，并在 meta 里标 `threshold_relaxed: true` 和补录原因，不偷偷放宽。
7. **记录**：每个保留的站在 `data/tvbox/meta.json` 的 `speed_records` 里留：key、api 域名、延迟 ms（TTFB/总耗时）、带宽 KB/s、走哪个通道、国内 CDN 是否加分、抓取时间、category（热门/偏门）、niche_tag。带宽 KB/s 是 `?ac=list` 小样本（通常只有几 KB）的等效速率，受 TTFB 主导，不能直接等同于视频播放带宽，`sample_bytes` 字段记录了每次实测实际下载的字节数，可复核。
8. **偏门占比已达 70/30（2026-11-11 成人过滤修复后重抓）**：本次最终 120 站里偏门 36 个（热门 84），占比 30%。其中 34 个是 type-3 偏门站（4K 12、动漫 6、地方台/直播 5、体育 4、少儿 3、戏曲 2、日韩 2），按 quota 从 479 个严格分类的 type-3 偏门候选里挑出，本轮 108 个通过 jar/ext 可达性校验；另 2 个是 type-0/1 偏门站（魔都动漫-动漫、360┃直连-4K），过了两轮实测。**诚实说明**：type-3 偏门站只做了 spider jar / api / ext 可达性校验，**未做单站 HTTP 延迟测试**（csp_ 类名本身不是 URL，没法单独测速），实际播放速度取决于 spider jar 和对应 App，见 `data/tvbox/meta.json` 的 `type3_picked` / `type3_records`。另注意本轮 type-3 通过数（108）比上轮（405）低得多：type-3 校验依赖共享 spider jar 的可达性，本轮 hebijunge 的 LIBVIO.jar 多通道校验未通过，330 个站因 spider 失败被拦，输出 spider 改为 moji 的 jpg 伪装 jar（HEAD 200 已验证）；偏门 tag 分布会随上游 spider 可达性逐轮波动，但总数 120、70/30 构成不变。第一版因丢掉全部 type-3 非 jar 站，偏门只有 2 个；第一版还曾按 categories 宽列表分类，把 36 个综合站误判成偏门，已修正为严格分类，不为凑 30% 降低标准或误标。
9. **成人过滤（TVBox，2026-11-11 修复）**：关键词表写在 `fetcher.py` 的 `TVBOX_ADULT_STRONG_KEYWORDS` / `TVBOX_ADULT_TOKEN_RE` / `TVBOX_ADULT_CAUTIOUS_KEYWORDS` 里，作用于全部 TVBox 站（去重后、测速前）、type-3 挑选兜底、最终写文件前复核，并过滤 lives 频道名。强匹配（子串）：涩、色情、成人、黄片、黄文、四级、fullerotic、erotic、porn、情色、毛片、三级片、18禁、裸聊、cam4、91动漫；词边界匹配（正则，前后不能是字母数字）：av、sex、xxx——裸子串查 `av` 会误删 `kxgav.com` 的白嫖站（已验证），不要改回子串；谨慎词 `老片` 绝不单独过滤（1905“经典影视”等正常老片站保留），只作审计清单。本次重抓剔除 13 个成人向站：3d涩动漫.py、91动漫.py、Cam4直播.py（任务点名的 3 个），另有同类 10 个（cam4.py、聚优AV解说、fulleroticmovies怀旧经典.py、vf四级老片.py、口爆大全AV.py、小涩界.py、涩库免翻.py、猫咪AV.py、爱豆AV.py、萝莉AV.py），明细与命中原因见 `data/tvbox/meta.json` 的 `adult_removed`。被删的 3 个偏门名额由 type-3 配额的 surplus 补齐：动漫补 AnimeAiMoDu（魔都动漫）、AnimeCiYuanCheng（次元动漫）、AnimeXiFan（稀饭动漫）、Dm84（巴士动漫）、Gugu（咕咕动漫），直播补 LiveAiHuYa（虎牙直播）、WexNewBiLiLive（哔哩直播）——同 tag 内按 quota + surplus 补位，因本轮 spider 波动整个 type-3 pick 集有所洗牌，动漫/直播 tag 的新入选不止这 3 个名额的直接对应，属正常逐轮波动。关键词过滤防不住改名伪装的成人站（换个正常名字就进得来），也可能误伤极少数擦边命名的正常站，导入后请自行验证，见“已知限制”。
8. **大陆视角（诚实说明）**：本次是新加坡 VM 实测 + 国内 CDN 启发式，**不是**大陆实测。部署后请在大陆这样复测：用 [itdog.cn](https://www.itdog.cn/)、[boce.com](https://www.boce.com/)、[17ce.com](https://www.17ce.com/) 这类多点测速站，把 `tvbox.json` 里各站的 api 域名逐个测大陆多节点延迟和下载速度，慢的在 `fetcher.py` 里调低排序或直接删掉；也可以在大陆机器上直接跑 `python3 fetcher.py` 再测一轮——代码里的测速逻辑不用改，换台机器跑就是大陆视角。如果当时打不开这些测速站，meta.json 会如实写“新加坡 VM 实测 + 国内 CDN 启发式，部署后需大陆实测”，不谎称已在大陆测过。

### 偏门是怎么找的（TVBox，占比约 30%）

- 不只看大聚合的 Star 排序：从聚合里的冷门 key、站点的 `categories` 字段、直播源里的特色频道里挖。
- 专门补了 4 个小众上游（2026-11-11 实测可打开，URL 真实可验证）：`25175/tvyuan`（每小时自动聚合，站名自带 `[KB/s|ms]` 测速标注）、`102244653/tvbox-resource/dianshi.json`（298 站，纪录片/日韩/港台/动漫分类全）、`jinenge/tvbox`（Bilibili/MTV 纪录片/演唱会）、`niubihu1/tvbox`（4K/体育/直播向）。
- 偏门分类按 key/name/group/categories 匹配：纪录片、体育、海外剧、日韩、港台、经典老片、戏曲、少儿、动漫、4K、地方台/直播。type-0/1 偏门站要过同样的两轮测速，type-3 偏门站要过 spider jar / api / ext 可达性校验（未做单站延迟测试），测不过一样剔除，不为凑偏门降低标准。入选的偏门清单见 `data/tvbox/meta.json` 的 `niche_picked`（含 type-3 的 `type3_picked`）。

### 书源：100–200 个，目标 150，约 70% 热门 + 30% 偏门

1. **基础过滤**：HTTP 200 + JSON 解析 + 含 `bookSourceName` / `bookSourceUrl` / `ruleSearch`；只留 `bookSourceType==0` 文字小说；成人/短剧/有声/漫画按关键词表过滤。
2. **规则齐全**：必须同时有 `ruleSearch`、`ruleContent`、`ruleToc`，且有 `searchUrl`（只有发现、没有搜索的剔除）；备注含登录/付费/VIP/会员/充值的剔除。
3. **官网可打开**：对头部池（热门 280 + 偏门 120，共 400 个）逐个 GET `bookSourceUrl`，超时 5 秒，打不开的排后面。
4. **搜索抽查**：取头部池前 200 个（热门 140 + 偏门 60），用 3 本热门书做测试词——《斗破苍穹》（天蚕土豆）、《凡人修仙传》（忘语）、《庆余年》（猫腻），按各源的 `searchUrl` 模板拼 URL 请求，返回 200 且正文含书名或作者才算通过。**2 本以上通过才算“搜得全”**。`searchUrl` 尾部带 `@js:` 代码的先剥掉再解析；以 `{{url=…}}` / `{{cookie…}}` 开头的按规则处理或跳过并记录，不直接判 0 分。**环境限制（诚实说明）**：本机出口拦截 POST（httpbin.org/post 也超时，GET 正常），POST 搜索的源无法在本机实测，本次 200 个抽查源里 80 个全跳过、标记为 `search_pass=null`、不计入 0/3，待在能 POST 的正常环境（Render/VPS）重跑 `fetcher.py` 才能得到 POST 源的真实通过率；首页返回、404 的仍算失败。详见 `data/books/meta.json` 的 `threshold_note`。
5. **第二轮复测**：2/3 通过的源，把没通过的那本再测一次，确认是偶发失败还是真搜不到，复测通过可升级为 3/3。
6. **构成与排序**：按搜索通过率 > 规则齐全度 > 官网可打开排序，取热门 105 + 偏门 45 = 150；2/3 以上通过的不足 150 时，用 1/3 或 0/3 但官网可打开且规则齐全的源补到 150，并在 meta 的 `threshold_note` 里写明，不偷偷放宽。
7. **记录**：`data/books/meta.json` 留总数、抽查覆盖率（抽查了多少 / 候选多少）、搜索通过分布（3/3、2/3、1/3、0/3 各多少）、剔除原因统计（规则不全/无搜索/登录付费墙/官网打不开/成人等各多少）、每个保留源的 `kept_sources`（书源名、URL、category、niche_tag、搜索通过数、齐全度、官网是否可打开）。

### 偏门是怎么找的（书源，占比约 30%）

- 不只按下载量排序：从 yck 聚合包按分组/标签筛冷门源，从 luanmuc/suwa-ko 全量里挑覆盖冷门分类的源。
- 偏门分类按书源名/分组/URL 匹配：轻小说、二次元/同人、武侠/仙侠、科幻、悬疑/推理、历史/军事、言情/女频、玄幻/奇幻、儿童/少儿、体育/游戏。热门大站指番茄、起点、七猫、笔趣阁、纵横、掌阅、咪咕等。
- 偏门也要过同样的规则、官网、3 本书搜索抽查，搜不到书的一样剔除。入选的偏门清单见 `data/books/meta.json` 的 `niche_kept`。
- 网页：未登录只显示密码框；密码正确后（服务端 session，密码不写进前端 JS）显示上次 / 下次更新、站点数、书源数、文件列表、单个下载、全部打包 ZIP 下载、手动更新（轮询 `/api/update/status` 实时显示步骤和日志）。
- API：`POST /api/login`、`GET /api/status`、`GET /api/files`、`GET /api/download/<name>`、`GET /api/download-all`、`POST /api/update`、`GET /api/update/status`。正在抓取时重复触发会返回 409。

## 目录结构

```
app.py                 Flask 入口 (PORT, 默认 8000)
fetcher.py             抓取逻辑 (TVBox + Legado, 可单独运行)
requirements.txt       只用 Flask + requests
Dockerfile             python:3.11-slim
render.yaml            Render 一键部署配置
.env.example           环境变量模板, 复制为 .env 后修改
templates/index.html   前端 (中文、手机可用)
data/                  抓取生成的数据 (首次启动自动生成, 默认不提交到 Git)
  tvbox/tvbox.json     TVBox 聚合源 (sites/parses/lives)
  tvbox/meta.json      TVBox 抓取详情 (各上游成功/失败、站点数)
  books/books.json     Legado 书源数组
  books/meta.json      书源抓取详情
  status.json          上次/下次更新时间
research/              调研报告 (TVBox / Legado, 含上游清单与格式说明)
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `ACCESS_PASSWORD` | `change-me` | 访问密码，**务必修改**；未改启动时会打印警告 |
| `UPDATE_INTERVAL_DAYS` | `7` | 定时抓取间隔（天） |
| `PORT` | `8000` | 监听端口（Render 会自动注入 `PORT`） |
| `DATA_DIR` | `data` | 数据目录 |
| `SECRET_KEY` | 随机 | 会话密钥，不设则每次启动随机生成（重启后需重新登录） |

## 从零开始：本地运行

```bash
# 1. 需要 Python 3.11+ (本项目在 3.12 上实测通过)
python3 --version

# 2. 下载代码并进目录
git clone <你的仓库地址>
cd tvbox-legado-project

# 3. (可选) 创建虚拟环境
python3 -m venv .venv && source .venv/bin/activate

# 4. 安装依赖
pip install -r requirements.txt

# 5. 配置环境变量
cp .env.example .env
# 编辑 .env, 把 ACCESS_PASSWORD 改成你自己的强密码
# Windows 记事本 / macOS/Linux 用任意编辑器打开 .env 修改即可

# 6. 先手动抓一次 (生成 data/tvbox/tvbox.json 和 data/books/books.json)
python3 fetcher.py
# 或: DATA_DIR=data python3 fetcher.py

# 7. 启动 Web 服务
python3 app.py
# 打开 http://localhost:8000, 输入 .env 里的密码登录
```

只用环境变量不用 `.env` 文件也可以：

```bash
ACCESS_PASSWORD=你的密码 UPDATE_INTERVAL_DAYS=7 PORT=8000 python3 app.py
```

## 部署到 Render

1. 把代码推到 GitHub（见下）。
2. Render → New → Web Service → 选你的仓库，Render 会自动识别 `render.yaml`（Docker 环境）。
3. 在 Render 的 Environment 里设置 `ACCESS_PASSWORD`（必填，改成强密码），`UPDATE_INTERVAL_DAYS` 默认 7。
4. 部署完成后打开 Render 给你的域名，输入密码登录。首次启动数据缺失会自动抓取一次（1-3 分钟），可点“手动更新”看进度。
5. 注意：Render 免费版实例休眠后会重启，`data/` 是临时磁盘，重启后会重新抓取（这是设计如此：数据总能从上游重新生成）。

## 部署到 Hugging Face Spaces

1. 在 Hugging Face 新建一个 Space，SDK 选 **Docker**（推荐，`Dockerfile` 即入口）或 **Python**。
2. 把代码推到 Space 的仓库。
3. 在 Space → Settings → Variables and secrets 里设置 `ACCESS_PASSWORD`、`UPDATE_INTERVAL_DAYS`。
4. 等构建完成，打开 Space 地址登录使用。
5. 若选 Python SDK：入口就是 `app.py`，需在 Space 的 `requirements.txt`（本仓库已有）和启动命令里确认用 `python app.py`；端口用 Space 注入的 `PORT`。

## 部署到 VPS / 小鸡（Ubuntu 为例）

```bash
# 1. 装 Python 和 git (Debian/Ubuntu)
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git

# 2. 拉代码
git clone <你的仓库地址>
cd tvbox-legado-project

# 3. 虚拟环境 + 依赖
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 4. 配置密码
cp .env.example .env
nano .env   # 把 ACCESS_PASSWORD 改掉

# 5a. 直接后台运行 (最简单)
nohup python3 app.py > app.log 2>&1 &
# 打开 http://你的服务器IP:8000

# 5b. 或用 Docker
docker build -t tvbox-legado .
docker run -d --name tvbox-legado -p 8000:8000 \
  -e ACCESS_PASSWORD=你的密码 -e UPDATE_INTERVAL_DAYS=7 tvbox-legado

# 5c. 或用 systemd 开机自启 (生产推荐, 需 root)
# 见下方示例, 按实际路径修改后放到 /etc/systemd/system/tvbox-legado.service
```

systemd 示例（`/etc/systemd/system/tvbox-legado.service`）：

```ini
[Unit]
Description=TVBox Legado Aggregator
After=network.target

[Service]
WorkingDirectory=/root/tvbox-legado-project
Environment=ACCESS_PASSWORD=你的密码
Environment=UPDATE_INTERVAL_DAYS=7
Environment=PORT=8000
ExecStart=/root/tvbox-legado-project/.venv/bin/python app.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tvbox-legado
sudo systemctl status tvbox-legado
```

反向代理（可选）：用 Nginx/Caddy 把域名反代到 `127.0.0.1:8000`，并配 HTTPS；本项目本身不内置 HTTPS，生产请放在反代后面。

## 上传到 GitHub

```bash
cd tvbox-legado-project
git init
git add .
git commit -m "TVBox + Legado 源聚合器"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

注意：`.env`（真实密码）和 `data/` 默认在 `.gitignore` 里不会被提交；部署平台靠首次启动自动抓取生成数据。如需把本次抓到的数据也一并提交，可 `git add -f data/`（不推荐长期这么做，数据会过期）。

## 使用

1. 打开部署地址，输入访问密码登录。
2. 首页显示 TVBox 站点数、书源数、上次 / 下次更新时间。
3. 文件列表可单个下载：`tvbox.json` 导入 TVBox，`books.json` 导入阅读 App（Legado）。
4. “全部下载为 ZIP”会把整个 `data/` 文件夹打包成 `tvbox-legado-sources-YYYYMMDD.zip` 下载。
5. “手动更新抓取”会重新从上游抓取，下方实时显示步骤日志（正在抓 XX、已完成、成功/失败数）。

## 已知限制

- 详见各 `data/*/meta.json` 的 `sources`（`ok: false` 的就是本次没抓到的上游）和顶部的“诚实说明”。
- 大陆速度未实测，需用户在大陆实测；上游域名在大陆可能被墙或变慢，`fetcher.py` 已做 jsDelivr → ghproxy → raw 回退。
- TVBox 截断到 120 站（80–150，约 70% 热门 + 30% 偏门），不是全量；如需更多，改 `fetcher.py` 的 `MAX_TVBOX_SITES`（同时改 `MIN_TVBOX_SITES`）。书源截断到 150 个（100–200），改 `fetcher.py` 的 `MAX_BOOK_SOURCES`。
- 书源过滤用了关键词表，极少数正常书源可能被误删、极少数擦边源可能漏网，导入后请自行验证。
- TVBox 成人过滤也是关键词表（强匹配 + 词边界 + 老片不单独过滤），防不住改名伪装的成人站，也可能误伤极少数擦边命名的正常站；本次重抓剔除 13 个，明细见 `data/tvbox/meta.json` 的 `adult_removed`，导入后请自行验证。
