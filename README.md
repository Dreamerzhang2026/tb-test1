# TVBox + Legado 源聚合器

定时抓取全网公开的 TVBox 聚合源和 Legado（阅读 App）书源，清洗、去重后生成
`tvbox.json` / `books.json`，通过带密码的网页查看、下载（可打包成 ZIP），也可手动触发更新并实时看进度。

> **诚实说明 / 已知限制（必读）**
> - **2026-11-13 全量验证与合并交付**：本轮全量跑（09:11–10:32 UTC）再次撞上出口代理劣化——TVBox 测速仅 15/957 通过第一轮、书源上游抓取因 403 + 预算耗尽得 0 候选，测速/搜书结果无效，未放宽门槛凑数；修复后书源单跑（10:36–10:41 UTC）上游恢复（3254 候选）但 146 个小说站域名全部 hostDead，四步校验无有效样本。交付为合并数据：A 套 120 沿用健康跑验证（热门 75 / 偏门 45），B 套 32 为本轮新鲜验证；书源 150 里 39 个实测通过、女频/言情 41 个（含晋江、潇湘书院等 16 个新换入），《缠珑》类女频书本轮 0 个源验证可读（待代理恢复后重跑）。详见 `data/tvbox/meta.json` / `data/books/meta.json` 的 `data_provenance`。
> - **2026-11-13 并入他方 AI 成果**：@js: 规则沙箱（Node.js 子进程 + vm 沙箱，`js/` 目录）、书源四步全流程校验（搜索→详情→目录→正文）、TVBox 真实带宽测速 + DoH 大陆可达性判定、对方种子清单（seed-cfgs.txt / seed-bases.txt）、第二书源合集 legado.aoaostar.com、受限内容词表合并、女频/言情缺口补齐，详见各节。**注意三点诚实边界**：(1) TVBox 的“大陆可达性”是 DoH DNS 层面判定（大陆解析器返回 0.0.0.0/NXDOMAIN 而境外能解析 → GFW 污染），**不是**真从大陆测速；真实带宽是顺着 m3u8 下载一个分片（2MB 或 5 秒）算出的 kbps，写在 meta 的 `throughput_kbps` 里，测不到时记 `null`（不编数）；(2) @js: 沙箱只覆盖对方代码里已实现的规则 API（searchUrl、ruleSearch、ruleBookInfo、ruleToc、ruleContent 的常用字段与 `java.ajax/get/post`、`createSymmetricCrypto`、`t2s`/`s2t` 为占位 stub，执行失败仍标未测试、不判死刑；沙箱有正则黑名单，理论上可被绕过，只用于跑受信书源规则，不可执行任意用户脚本）；(3) 书源四步校验的“真实章节数”除《缠珑》外为约数，只作 0.8 比率的补充信号，不单独淘汰。
> - **女频/言情缺口（2026-11-13 用户反馈）**：此前搜书抽查的测试书全是男频玄幻，女频源永远测不过；现测试书改为男女频混合（3 男频 +《缠珑》+《知否知否应是绿肥红瘦》），女频/言情源单独计 `female_count`（目标 ≥30），aoaostar“关耳女频”专包优先拉取。验收口径：最终 150 里能正常看《缠珑》这类晋江/潇湘女频书的源数如实写在 `data/books/meta.json` 的 `chanlong_readable` / `female_books_readable` 里，不报虚数。
> - **不再支持 Vercel**：Vercel 是 Serverless，部署后文件系统除 `/tmp` 外只读、函数几十秒就超时，全量抓取（20–60 分钟）跑不完；2026-11-13 起本项目不再支持 Vercel，部署目标为 Render / VPS / 本地。代码里留着的 Vercel 兼容逻辑不再维护，`vercel.json` 和 `api/index.py` 已从仓库删除。
> - **自动发现新源**：每轮抓取前先联网搜索找新上游（GitHub 多关键词多页搜索 + SearXNG 等免费公开搜索引擎 + yck 聚合索引），候选再走同一套筛选；发现 ≠ 收录，不合规照样淘汰，见“自动发现新源”一节。
> - **长抓取与 Render 免费版休眠**：全量抓取现在要 20–60 分钟，Render 免费版 15 分钟无访问会休眠，休眠时后台抓取会被冻结甚至杀掉。应对：用 UptimeRobot 每 5 分钟 ping 一次保活，或在本地/VPS 上跑 `python3 fetcher.py` 再把 `data/` 提交；抓取每完成一个阶段都会增量写盘（`data/*/checkpoint_*.json`），中途被杀已测的数据不丢，但要从头再跑一轮才能出完整结果。免费版上不要假装 1 小时一定能跑完。
> - **大陆速度未实测**：本次抓取在新加坡的机器上完成，TVBox 测速是新加坡 VM 三轮实测 + 国内 CDN 启发式（jsDelivr / `.cn` / 国内云域名加分），不是大陆多点实测。配置文件下载快不等于播放不卡，播放取决于各 `sites.api` 指向的 CMS 站和 m3u8 源站带宽。请按下面“筛选标准”里的方法在大陆实测后自行调整上游（改 `fetcher.py` 里的 `TVBOX_SOURCES` / `LEGADO_SOURCES`）。
> - **不求快、只求质量**：本项目宁缺毋滥、多测几轮、多验证几遍，TVBox 只留 80–150 站、书源只留 100–200 个，测不过 / 搜不到的一律剔除，不凑数。
> - **书籍全不全无法仅凭清单判断**：聚合包覆盖广但混有重复 / 失效源，导入后请自行搜热门书、打开一章验证。
> - **上游随时可能失效**：某个上游抓不到时，程序不会编造假数据填充，缺失会如实写进 `data/*/meta.json` 的 `sources` 里（`ok: false` + 原因）。
> - **合规**：部分上游 README 声明仅供学习、禁止商用；请遵守当地法律和各源的使用声明，不要商用、不要在境内使用明确禁止的源。

## 功能

- 定时抓取：环境变量 `UPDATE_INTERVAL_DAYS`（默认 7 天），后台线程每小时检查一次是否到期；启动时数据缺失或过期也会先抓一次；`data/status.json` 记录上次 / 下次更新时间。
- TVBox 抓取（`fetcher.py`）：按优先级抓 hebijunge、TVboxorg、qist、Lightconer 单仓聚合、cluntop、gao，再加 4 个偏门补充上游（25175/tvyuan、102244653/dianshi.json、jinenge、niubihu1，见“筛选标准”）；每个上游 jsDelivr → ghproxy → raw 三通道回退，超时 12 秒；剥离行首 `//` 注释；`./jar` 等相对路径解析为绝对 URL；spider 剥掉 `;md5;…` 后缀再解析，jpg/png 伪装的 spider 原样保留；type=3 站不再要求 api 是 http jar，csp_ 类名 / py_ 模块 / drpy JS 都保留，校验方式改为上游 spider jar 多通道 HEAD/GET 200（jsDelivr 对 .jar 返回 403，须改走 raw 才能测）+ ext 若是 URL 则 HEAD/GET 能打开，按 quota 只加约 34 个偏门；内容分级与双套输出（2026-11-13）：TVBox 最终生成两套——A 套 `tvbox/tvbox.json`（严格模式，受限内容一律排除，宁可错杀不可漏进 A 套）与 B 套 `tvbox/tvbox_b.json`（被判为受限内容的站，同样过三轮测速 / type-3 jar-ext 校验，通过才入选；spider/parses/lives 照搬 A 套）。关键词表作用于全部 TVBox 站（去重后、测速前）、type-3 挑选兜底、最终写文件前复核，并过滤 lives 频道名；误杀修复见“筛选标准”第 9 条，明细见 `data/tvbox/meta.json` 的相关字段；多仓索引 `{urls:[]}` 二次解析；按 key 去重后做三轮实测、按速度排序再截断到 120 站（80–150，约 70% 热门 + 30% 偏门；不足 120 也不放宽补录）；保留 parses 全集；直播优先用 Lightconer TVBox-Sources。**2026-11-13 并入**：对方种子清单 `seed-cfgs.txt`（158 个配置 URL，144 个 GitHub + 14 个直链）追加进上游、seed-bases.txt（549 条直连接口）按 200/轮轮换补入候选池；入选站再加一档“真实带宽 + 大陆可达性”实测（顺 m3u8 下一个分片算 kbps、DoH 四解析器判 GFW 污染、ip-api 查归属地打分，见“筛选标准”第 10 条），结果写进 `data/tvbox/meta.json` 的 `extras`（`throughput_kbps` / `cn_reachability` / `region_score`）。
- 书源抓取：优先抓 yck 聚合 `yck2026.fun/yuedu/shuyuans/json/id/1135.json`，再抓 luanmuc、suwa-ko、XIU2、cjj；逐条校验 HTTP 200 + JSON 解析 + 含 `bookSourceName` / `bookSourceUrl` / `ruleSearch`；只留 `bookSourceType==0` 文字小说；按关键词表过滤受限内容、短剧、有声、漫画等（关键词表见 fetcher.py，单字“黄”不用作过滤词以免误删正常小说）；规则齐全 + 官网可打开 + 5 本书搜索抽查（3/5 以上通过才算搜得全）后，按通过率排序留 150 个（不足 150 也不放宽补录）（100–200，约 70% 热门 + 30% 偏门）；按 `bookSourceUrl` 去重。**2026-11-13 并入**：第二合集 legado.aoaostar.com（7 个 bundle，含“关耳女频”86 条专包）与 yck 并列为上游；抽查改走 Node.js 子进程四步全流程校验（`js/validate_books.js`，搜索→详情→目录→正文，@js: 规则在 vm 沙箱里真跑，不再一律标未测试）；测试书改为男女频混合 5 本（斗破苍穹、凡人修仙传、庆余年、缠珑、知否知否应是绿肥红瘦），女频源用女频 5 本题（缠珑、知否、庶女攻略、甄嬛传、三生三世十里桃花）；最终 150 里女频/言情源目标 ≥30 个，记在 `data/books/meta.json` 的 `female_count`；四步校验拦掉“搜得到但看不成”的源（乱码/占位/VIP 截断/文不对题），记 `content_bad_excluded`。

## 筛选标准（为什么只留这些）

不求快、只求质量高：宁缺毋滥，多测几轮、多验证几遍。所有阈值和记录都写在 `fetcher.py` 里，每次抓取的结果写在 `data/tvbox/meta.json` / `data/books/meta.json` 里，可复核。

### TVBox：80–150 站，目标 120，约 70% 热门 + 30% 偏门

1. **候选池**：先抓 10 个固定上游（含 4 个偏门补充源）+ 自动发现的新候选上游（GitHub 搜索、免费搜索引擎、yck 聚合索引，见“自动发现新源”），去重后得到候选池（本次约数百站），不直接按 Star 取前几名。
2. **第一轮实测**：对每个候选站的 `api` 做 HEAD/GET 计时——TTFB、总耗时、限 200KB 内下载算带宽（KB/s），CMS 站请求 `?ac=list` 看能否正常返回 vod 列表（正文含 `"list"` / `"class"` / `"vod` 等）。10 并发，每站超时 5 秒。
3. **门槛**：TTFB 建议 `< 1.5s`、总耗时 `< 3s`、能返回 vod 列表，三项都过才算通过。国内 CDN / 域名加分：jsDelivr、`.cn` 域名、阿里云 / 腾讯云等国内云域名在排序时减分（相当于约 200ms 优势）。
4. **第二、三轮复测**：分开取第一轮通过里热门前 110 名 + 偏门前 60 名复测（不只取全局速度前 160，否则偏门站会被热门站挤出复测池，导致 70/30 构成无偏门可选），两轮通过的再测第三轮，三轮都通过才算最终通过，延迟和带宽取三轮中较差的一次（保守计分），确认不是偶发快。
5. **多次失败剔除**：本轮测速失败的 key 记入 `data/tvbox/failures.json` 累计，连续失败 ≥ 2 次的直接剔除，哪怕本轮偶然通过也不留。
6. **构成**：按延迟 + 带宽排序，先取热门前 84 名、偏门前 36 名（70/30），哪边不够用另一边补；通过数不足 80 时才从一轮通过的里按分数补到 80，并在 meta 里标 `threshold_relaxed: true` 和补录原因，不偷偷放宽。
7. **记录**：每个保留的站在 `data/tvbox/meta.json` 的 `speed_records` 里留：key、api 域名、延迟 ms（TTFB/总耗时）、带宽 KB/s、走哪个通道、国内 CDN 是否加分、抓取时间、category（热门/偏门）、niche_tag。带宽 KB/s 是 `?ac=list` 小样本（通常只有几 KB）的等效速率，受 TTFB 主导，不能直接等同于视频播放带宽，`sample_bytes` 字段记录了每次实测实际下载的字节数，可复核。
8. **偏门占比已达 70/30（2026-11-11 受限内容过滤修复后重抓，2026-11-13 双套重构后为热门 75 / 偏门 45）**：本次最终 120 站里偏门 45 个（热门 75），占比 37.5%。其中 34 个是 type-3 偏门站（4K 12、动漫 6、地方台/直播 5、体育 4、少儿 3、戏曲 2、日韩 2），按 quota 从 479 个严格分类的 type-3 偏门候选里挑出，本轮 108 个通过 jar/ext 可达性校验；另 2 个是 type-0/1 偏门站（魔都动漫-动漫、360┃直连-4K），过了两轮实测。**诚实说明**：type-3 偏门站只做了 spider jar / api / ext 可达性校验，**未做单站 HTTP 延迟测试**（csp_ 类名本身不是 URL，没法单独测速），实际播放速度取决于 spider jar 和对应 App，见 `data/tvbox/meta.json` 的 `type3_picked` / `type3_records`。另注意本轮 type-3 通过数（108）比上轮（405）低得多：type-3 校验依赖共享 spider jar 的可达性，本轮 hebijunge 的 LIBVIO.jar 多通道校验未通过，330 个站因 spider 失败被拦，输出 spider 改为 moji 的 jpg 伪装 jar（HEAD 200 已验证）；偏门 tag 分布会随上游 spider 可达性逐轮波动，但总数 120、70/30 构成不变。第一版因丢掉全部 type-3 非 jar 站，偏门只有 2 个；第一版还曾按 categories 宽列表分类，把 36 个综合站误判成偏门，已修正为严格分类，不为凑 30% 降低标准或误标。
9. **内容分级与双套输出（TVBox，2026-11-13 重构）**：最终生成两套——A 套 `data/tvbox/tvbox.json` 与 B 套 `data/tvbox/tvbox_b.json`（同样 TVBox 格式，spider/parses/lives/doh/rules 照搬 A 套，只换 sites 列表）。A 套用严格模式：强词、短词词边界与严格词多档匹配，命中受限词的站不丢弃，进 B 套。B 套也要过同样的实测：可测速站三轮测速（TTFB<1500ms / 总耗时<3000ms / 样本≤200KB，取最差一轮），type-3 站 jar/ext 可达性校验；通过率与数量记在 `data/tvbox/meta.json` 里。
误杀修复：旧逻辑把 key+name+api+ext 拼成一个字符串做子串/词边界匹配，XBPQ 配置里 `分类url` 的 `;;av` 只是配置语法后缀（同类后缀还有 `;;vr1au0`、`;;z`），被旧正则误判命中，误删了 相声/评书（两者 分类 全是 郭德纲/德云社/马三立、袁阔成/单田芳/田连元 等曲艺内容）；九月影视的 ext 配置含受限词也被整串匹配命中。新逻辑只看四类直接证据：(1) 站名/key（中文强词子串 + 英文短词词边界 + 严格词）；(2) ext 字典的 `分类` 字段与 type-1 的 `categories` 列表（站点自声明的内容分类）——ext 的其它字段（主页url/分类url/搜索url/请求头/正则/过滤规则）一律不匹配；(3) URL 只看域名标签/路径段完整等于受限内容词，不拿整个 URL 做子串包含（`kxgav.com` 这类含子串的站名、`;;av` 后缀不受影响）；(4) ext 是相对路径时只看文件名，是短标签时按站名规则判，长串跳过。重跑后 相声/评书/九月影视 回到 A 套的前提是它们仍在上游池中且通过校验，结果见 meta.json 的 `fixed_false_positives`（若某站本轮上游池中已无此站，会如实记 `not_in_pool`）。
新逻辑的 categories 检查还揪出旧 A 套里的 3 个漏网站，现均已移入 B 套。
关键词过滤防不住改名伪装的受限内容站（换个正常名字就进得来），也可能误伤极少数擦边命名的正常站，导入后请自行验证，见“已知限制”。用法：日常使用只导入 A 套 `tvbox.json`；B 套 `tvbox_b.json` 单独存放、按需导入，不要两个都导入（会重复）。
8. **大陆视角（诚实说明）**：本次是新加坡 VM 实测 + 国内 CDN 启发式，**不是**大陆实测。部署后请在大陆这样复测：用 [itdog.cn](https://www.itdog.cn/)、[boce.com](https://www.boce.com/)、[17ce.com](https://www.17ce.com/) 这类多点测速站，把 `tvbox.json` 里各站的 api 域名逐个测大陆多节点延迟和下载速度，慢的在 `fetcher.py` 里调低排序或直接删掉；也可以在大陆机器上直接跑 `python3 fetcher.py` 再测一轮——代码里的测速逻辑不用改，换台机器跑就是大陆视角。如果当时打不开这些测速站，meta.json 会如实写“新加坡 VM 实测 + 国内 CDN 启发式，部署后需大陆实测”，不谎称已在大陆测过。另：用户建议的 [shifter.io/cn/web-proxy](https://shifter.io/cn/web-proxy) 已核实——它是免费网页代理（浏览器里打开目标网页、无需注册、无 API、无 HTTP 代理地址），fetcher 无法直接用它测速或搜书；其付费住宅代理套餐才提供可供脚本使用的代理地址，需另行购买、未接入。备选拨测站 boce.com / itdog.cn / 17ce.com 只能人工在网页上复核大陆可达性（逐个域名测多节点延迟），没有可供 fetcher 免费调用的 API，结论只作复核参考，不接入抓取流程。
10. **真实带宽 + 大陆可达性（2026-11-13 并入，入选站附加实测）**：在三轮 TTFB 复测之外，对入选的 120 站（及B 套可测速站）再跑一档：(a) 顺着列表里的第一条 vod 的 `vod_play_url` 取 m3u8（按 `$$$` 分组、`#` 分集、`$` 取第 2 段，逐个试最多 4 个候选 m3u8，200 且以 `#EXTM3U` 开头才算有效；同时读 `#EXT-X-STREAM-INF` 的 BANDWIDTH/RESOLUTION 作声明码率）；(b) 真实带宽：跟进 m3u8 里的第一个分片（`.ts`/`.m4s`，主/子清单都跟），下载 2MB 或 5 秒取其一，算真实 kbps（有效条件：≥32KB 且 ≥0.15 秒），记 `throughput_kbps`，测不到记 `null` 不编数；(c) 大陆可达性：DoH 并行问 4 个解析器（大陆 dns.alidns.com、doh.pub；境外 cloudflare-dns.com、dns.google），大陆返回 0.0.0.0/NXDOMAIN 而境外能解析 → GFW 污染判定（≥2 个大陆解析器污染且境外有答案 = BLOCKED，≥1 个 = SUSPECT），再用 ip-api 查 IP 归属地打分（CN 52+6、HK 34+6、TW/MO 26、新加坡/日韩等 18+4、Cloudflare 20、其它 5，SUSPECT −14、BLOCKED −40）。结果写进 `data/tvbox/meta.json` 的 `extras`（`throughput_kbps` / `cn_reachability` / `region` / `region_score` / `score`）和每站的 `sample`（列表首个 vod 名，供人工复核）。**诚实边界**：这是 DNS 层面判定 + 单分片带宽抽样，不是真从大陆测速、也不是全片播放测速；DoH-BLOCKED 的站会从最终 120 里剔除并从后备补位，`extras` 里记 `blocked_dropped` 数量。实测环境若出口代理劣化（视频 CDN 403/超时），多数站的 `throughput_kbps` 会是 `null`，如实记录、不放宽门槛。
11. **种子清单（2026-11-13 并入）**：`seed-cfgs.txt`（158 个配置 URL，144 个 GitHub + 14 个直链，已去掉 5 个与固定清单完全重复的、1 个可疑域名）追加进 TVBox 上游（GitHub 条目按 owner/repo/branch/path 抓，`direct_url` 条目直接抓）；`seed-bases.txt`（549 条直连接口，已去掉 6 条 HOST 占位符）按每轮 200 条轮换补入候选池（`data/tvbox/rotation.json` 记 offset，下轮接着取），作为 type-1 候选站。两份清单都先过滤占位符（HOST）和可疑域名再做存活探测，不合规照样淘汰。来源记在 `data/tvbox/meta.json` 的 `seed_cfgs_added` / `seed_bases_window`。

### 偏门是怎么找的（TVBox，占比约 30%）

- 不只看大聚合的 Star 排序：从聚合里的冷门 key、站点的 `categories` 字段、直播源里的特色频道里挖。
- 专门补了 4 个小众上游（2026-11-11 实测可打开，URL 真实可验证）：`25175/tvyuan`（每小时自动聚合，站名自带 `[KB/s|ms]` 测速标注）、`102244653/tvbox-resource/dianshi.json`（298 站，纪录片/日韩/港台/动漫分类全）、`jinenge/tvbox`（Bilibili/MTV 纪录片/演唱会）、`niubihu1/tvbox`（4K/体育/直播向）。
- 偏门分类按 key/name/group/categories 匹配：纪录片、体育、海外剧、日韩、港台、经典老片、戏曲、少儿、动漫、4K、地方台/直播。type-0/1 偏门站要过同样的两轮测速，type-3 偏门站要过 spider jar / api / ext 可达性校验（未做单站延迟测试），测不过一样剔除，不为凑偏门降低标准。入选的偏门清单见 `data/tvbox/meta.json` 的 `niche_picked`（含 type-3 的 `type3_picked`）。

### 书源：100–200 个，目标 150，约 70% 热门 + 30% 偏门

1. **基础过滤**：HTTP 200 + JSON 解析 + 含 `bookSourceName` / `bookSourceUrl` / `ruleSearch`；只留 `bookSourceType==0` 文字小说；受限内容/短剧/有声/漫画按关键词表过滤。
2. **规则齐全**：必须同时有 `ruleSearch`、`ruleContent`、`ruleToc`，且有 `searchUrl`（只有发现、没有搜索的剔除）；备注含登录/付费/VIP/会员/充值的剔除。
3. **官网可打开**：对头部池（热门 280 + 偏门 120，共 400 个）逐个 GET `bookSourceUrl`，超时 5 秒，打不开的排后面。
4. **搜索抽查 + 四步全流程校验（2026-11-13 并入他方做法）**：取头部池前 200 个（热门 120 + 偏门 40 + 女频 40），用 **5 本混合测试书**做抽查——男频 3 本《斗破苍穹》（天蚕土豆）、《凡人修仙传》（忘语）、《庆余年》（猫腻）+ 女频 2 本《缠珑》（尤四姐，晋江）、《知否知否应是绿肥红瘦》（关心则乱）；女频/言情源改用女频 5 本题（缠珑、知否、《庶女攻略》吱吱、《甄嬛传》流潋紫、《三生三世十里桃花》唐七公子），每源仍按“5 本过 3 本即通过”。校验由 Node.js 子进程跑 `js/validate_books.js`（`js/validate_relaxed.mjs`，对方 validate.mjs 的放宽版）：搜索→详情→目录→正文四步全跑，目录章节数/真实章节数 ≥0.8 否则记 `toc_lagging`（只作信号、不淘汰）；正文第一章须 ≥300 字、汉字为主、含主角名或书名，不是占位/VIP 截断文本（第一章是序章等正文质量失败时再试第二章一次）；反爬/占位检测（Cloudflare 质询页、验证码页、“请下载 APP/试读结束”等占位文本）；`@js:` 规则在 vm 沙箱里真跑（仿真 `java.ajax/get/post`、jsoup 式元素 API、source/book 对象，覆盖 searchUrl、ruleSearch、ruleBookInfo、ruleToc、ruleContent），执行失败标未测试并记原因、不判死刑；Node 不可用时回退到旧 Python 搜索抽查（15 秒超时）。**“搜得到但看不成”要判掉**：四步里 ≥2 本书正文系统性不符（`content_bad>=2`：乱码、占位、章节错乱、VIP 截断、文不对题）的源不进最终 150，记 `content_bad_excluded`；3/5–4/5 的源把没通过的那本再测一次。**环境限制（诚实说明）**：本机出口拦截 POST（httpbin.org/post 也超时，GET 正常），POST 搜索的源无法在本机实测，本次 200 个抽查源里 80 个全跳过、标记为 `search_pass=null`、不计入 0/3，待在能 POST 的正常环境（Render/VPS）重跑 `fetcher.py` 才能得到 POST 源的真实通过率；首页返回、404 的仍算失败。详见 `data/books/meta.json` 的 `threshold_note`。
5. **第二轮复测**：2/3 通过的源，把没通过的那本再测一次，确认是偶发失败还是真搜不到，复测通过可升级为 3/3。
6. **构成与排序**：按搜索通过率 > 规则齐全度 > 官网可打开排序，取热门 105 + 偏门 45 = 150；2/3 以上通过的不足 150 时，用 1/3 或 0/3 但官网可打开且规则齐全的源补到 150，并在 meta 的 `threshold_note` 里写明，不偷偷放宽。**女频/言情配额**：最终 150 里女频/言情源目标 ≥30 个（记 `female_count`），不足时从头部池按同一标准补位（替换排名最低的非女频源，同域名的不重复入选）；aoaostar“关耳女频”专包优先拉取，yck 按分类筛女频/言情源（不只按下载量取前 N，下载量排序会埋没女频源）。《缠珑》类晋江/潇湘女频书的真实可读源数记在 `female_books_readable` / `chanlong_readable` 里，如实统计、不报虚数。
7. **记录**：`data/books/meta.json` 留总数、抽查覆盖率（抽查了多少 / 候选多少）、搜索通过分布（3/3、2/3、1/3、0/3 各多少）、剔除原因统计（规则不全/无搜索/登录付费墙/官网打不开/受限内容等各多少）、每个保留源的 `kept_sources`（书源名、URL、category、niche_tag、搜索通过数、齐全度、官网是否可打开）。

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
js/                    Node.js 书源四步校验 (2026-11-13 并入他方做法)
  validate_books.js    驱动: 按域名分组、hostAlive 门禁、镜像轮换、写结果 JSON
  validate_relaxed.mjs 对方 validate.mjs 的放宽版 (toc-lagging 只作信号、正文失败试第二章、书名也算 proof)
  jsrule.mjs / syncnet.mjs / syncnet-worker.mjs / rules.mjs / html.mjs / http.mjs / charset.mjs
                       对方代码复用 (MIT 授权, 每文件头有归属说明; http.mjs 已去掉证书失败自动重试的不安全逻辑)
  filters.mjs / validate.mjs  对方原版 (对照参考, 不直接跑)
seed-cfgs.txt          对方种子清单 158 个 TVBox 配置 URL (并入上游)
seed-bases.txt         对方种子清单 549 条直连接口 (轮换池, 每轮 200 条)
requirements.txt       只用 Flask + requests
Dockerfile             python:3.11-slim
render.yaml            Render 一键部署配置
.env.example           环境变量模板, 复制为 .env 后修改
templates/index.html   前端 (中文、手机可用)
data/                  抓取生成的数据 (首次启动自动生成, 默认不提交到 Git)
  tvbox/tvbox.json     TVBox 聚合源 (sites/parses/lives)
  tvbox/tvbox_b.json  TVBox B 套聚合源 (与 A 套同格式, 只换 sites 列表)
  tvbox/meta.json      TVBox 抓取详情 (各上游成功/失败、站点数、extras: throughput_kbps / cn_reachability)
  books/books.json     Legado 书源数组
  books/meta.json      书源抓取详情 (female_count / chanlong_readable / content_bad_excluded)
  status.json          上次/下次更新时间
research/              调研报告 (TVBox / Legado, 含上游清单与格式说明)
```

## 自动发现新源

每轮抓取前（第 0 阶段）先联网搜索找新上游，而不是只靠写死的 `TVBOX_SOURCES` / `LEGADO_SOURCES`。发现 ≠ 收录：候选上游仍要走同一套格式校验、去重、受限内容过滤、速度/搜书实测，不合规照样淘汰；结果记在 `data/*/meta.json` 的 `discovered_upstreams`（名字、来源、是否通过筛选）。

- **TVBox 发现**：GitHub 搜索 API 多关键词多页翻查（`tvbox`、`tvbox-config`、`tvbox源`、`tvbox配置`、`dianshi`、`电视源`、`tvbox config`、`tvbox json`，按最近更新排序，每词翻 2 页），跳过已在固定清单里的仓库和 fork；有 `GITHUB_TOKEN` 时再用 code search 补一轮（搜 `tvbox.json` / `box.json` 文件名）；再用免费公开搜索引擎补充——SearXNG 只固定用 2 个实例（`baresearch.org`、`searx.be`，通的那个才用，两个都不通就跳过，不再用 DuckDuckGo）；从搜索结果里解析 GitHub 仓库、raw/jsDelivr 直链，再探测常见配置文件路径（`tvbox.json`、`box.json`、`tvbox_recommended.json`、`jsm.json` 等），含 `sites` 数组才算候选。每轮最多 50 个 TVBox 候选。
- **书源发现**：yck 聚合索引页（`/yuedu/shuyuans/index.html`）全量扫聚合 id，取最新聚合（Range 只取前 4KB 探针确认是 JSON 数组，不整文件下载，最多 10 个）；GitHub 多关键词搜索 `legado` 相关仓库（`legado`、`阅读书源`、`shuyuan`、`yuedu`、`book-source` 等），探测 `books.json` / `all.json` 等文件（含 `bookSourceUrl` 的条目 ≥ 20 才算数）；搜索引擎补充同上。每轮最多 30 个书源候选。
- **上限与安全**：发现阶段最多 15 分钟，超时即停；全轮总预算 60 分钟（`FETCH_BUDGET_MINUTES` 可改），超时即收尾。GitHub 未登录搜索限流约 10 次/分钟，被限流（403/429）就记日志跳过搜索、用已找到的候选继续，绝不让整轮抓取失败；某个搜索引擎挂了同样跳过。开关：`DISCOVERY_ENABLED`，默认 `1` 开启，设 `0` 关闭。
- **域名收敛（2026-11-13）**：发现阶段只碰已经允许过的域名（`github.com` / `api.github.com` / `raw.githubusercontent.com` / `cdn.jsdelivr.net`、yck 三个镜像站）+ 固定 2 个 SearXNG 实例；候选探测只用 jsDelivr + raw，不走 `ghproxy.net` 等新镜像站；新域名先收集起来一次过完校验再批量处理，不一个一个零散触发。注意：测速/搜书实测本身要访问各源站自己的域名（那是产品功能，不是发现），不在收敛范围内。
- **已知限制**：GitHub 搜索只能找到公开仓库；搜索只能找到“有人维护的配置”，不能保证新；发现的候选仍要过实测筛选，通过率以 `meta.json` 为准。另：SearXNG / DuckDuckGo 从机房 IP 常返回机器人验证页（实测本机如此），此时搜索引擎补充会返回 0 条、自动降级为只用 GitHub + yck，这不是 bug，是对方的反爬；换出口 IP 或用 `GITHUB_TOKEN` 可缓解。

## 依赖包 (assets/)

TVBox 里需要加载 java 包的源（type-3 站的 spider jar / api / ext 文件）以前只校验可达性、从不下载保存。2026-11-13 起，抓取时会把最终 spider 的 jar 和入选 type-3 站的 api/ext 依赖文件下载到 `data/tvbox/assets/`（按来源命名、去重，记大小和 SHA256，下载失败记日志跳过、不中断），清单记在 `data/tvbox/meta.json` 的 `jar_files`（文件名、来源、大小、SHA256）。

- **交付**：前端“全部下载为 ZIP”会把 `tvbox/` 整个文件夹打进去（含 A 套 `tvbox.json`、B 套 `tvbox_b.json`、`meta.json` 和 `assets/`）；文件列表里也会显示 `assets/` 下的文件，可单独下载。
- **注意**：`tvbox.json` 里的 `api` / `spider` 远程 URL 保持不动，TVBox 仍远程加载才稳；`assets/` 里的 jar 包只是随包附带、供本地/离线使用和备份，不要改写 json 里的 URL 去指本地文件。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `ACCESS_PASSWORD` | `change-me` | 访问密码，**务必修改**；未改启动时会打印警告 |
| `UPDATE_INTERVAL_DAYS` | `7` | 定时抓取间隔（天） |
| `PORT` | `8000` | 监听端口（Render 会自动注入 `PORT`） |
| `DATA_DIR` | `data` | 数据目录。**Vercel 上默认自动改为 `/tmp/tvbox-legado-data`**（部署后文件系统只读，只有 `/tmp` 可写，写项目内 `data/` 会报 `[Errno 30] Read-only file system`）；本地/Render/VPS 保持 `data`。显式设置时优先用你设的值 |
| `SECRET_KEY` | 随机 | 会话密钥，不设则每次启动随机生成（重启后需重新登录）。**Vercel 上务必设置固定值**，否则每次冷启动换密钥，登录态会失效 |
| `VERCEL_FETCH_ENABLED` | `0` | 仅 Vercel（已不再支持，仅留作兼容）：是否允许手动/定时抓取。默认 `0`（关闭，只读仓库 `data/` 快照） |
| `DISCOVERY_ENABLED` | `1` | 自动发现新源开关。默认 `1` 开启（每轮抓取前先联网搜索找新上游）；设 `0` 关闭，只用固定上游清单 |
| `GITHUB_TOKEN` | （空） | 可选。GitHub 个人访问令牌（classic，public_repo 只读即可）：有则 GitHub 搜索限流从 10 次/分钟提到 30 次/分钟，且能用 code search 补一轮；没有也不报错，只是搜得少一点。不要把真实 token 提交到 GitHub |
| `FETCH_BUDGET_MINUTES` | `60` | 全轮抓取总时间预算（分钟）。发现 + 抓取 + 测速超过预算即收尾，已抓到的数据靠检查点落盘，不会丢失 |
| `PROXY_URL` / `CHINA_PROXY_URL` | （空） | 可选代理，格式如 `http://user:pass@host:port`。只用于 TVBox 测速、jar/ext 校验与书源搜书抽查，让测试从中国出口 IP 发起。注意：Shifter 免费网页代理（shifter.io/cn/web-proxy）没有可供脚本用的代理地址，不能用；只有其付费住宅代理（Lite 套餐 $10/月含 5GB 起）才提供代理地址，需另行购买后填入 |
| `TVBOX_SITE_COUNT` | `120` | TVBox A 套目标站数（2026-11-13 并入他方配置口径，默认值与原来一致） |
| `NOVEL_SOURCE_COUNT` | `150` | 书源目标数（同上） |
| `NOVEL_FETCH_TOP` | `1200` | yck 按下载量取前 N 个详情（对方默认 1200；我方聚合 id 列表另算） |
| `NOVEL_VALIDATE_MAX` | `200` | 书源四步校验池上限（对方默认 1600；我方头部池 400、实测池 200，200 已是本机 60 分钟预算内的上限） |
| `SESSION_HOURS` | `12` | 登录会话有效期（小时，对方默认 12） |

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
4. 部署完成后打开 Render 给你的域名，输入密码登录。首次启动数据缺失会自动抓取一次（全量约 20–60 分钟，含发现新源、三轮复测、5 本书抽查），可点“手动更新”看进度（8 个阶段 + 百分比 + 实时日志）。
5. 注意：Render 免费版 15 分钟无访问会休眠，休眠时后台抓取会被冻结甚至杀掉，一次 20–60 分钟的全量抓取很可能中途断掉。应对（二选一）：(a) 用免费的 UptimeRobot 每 5 分钟 ping 一次你的 Render 域名保活；(b) 在本地/VPS 上跑 `python3 fetcher.py`，再把 `data/` 提交到 GitHub 重新部署。抓取每完成一个阶段都会增量写盘（`data/*/checkpoint_*.json`），即使中途被杀，已测的数据也不会丢，但下次要从头再跑一轮才能出完整结果。免费版上不要假装 1 小时一定能跑完。

## 关于 Vercel（不再支持）

Vercel 是 Serverless，部署后文件系统除 `/tmp` 外全部只读，且函数有执行时长上限（免费版约 10–60 秒），全量抓取（20–60 分钟）跑不完；2026-11-13 起本项目不再支持 Vercel，部署目标为 Render / VPS / 本地。代码里留着的 Vercel 兼容逻辑（`VERCEL=1` 时写 `/tmp`、抓取默认关闭）不再维护，`vercel.json` 和 `api/index.py` 已从仓库删除。

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

注意：`.env`（真实密码）和 `data/` 默认在 `.gitignore` 里不会被提交；部署平台靠首次启动自动抓取生成数据。如需把本次抓到的数据也一并提交，可 `git add -f data/`（不推荐长期这么做，数据会过期）。如需把本次抓到的数据也一并提交，可 `git add -f data/`（不推荐长期这么做，数据会过期）。

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
- TVBox 内容分级也是关键词表（强匹配 + 词边界），防不住改名伪装的受限站，也可能误伤极少数擦边命名的正常站；明细见 `data/tvbox/meta.json`，导入后请自行验证。
