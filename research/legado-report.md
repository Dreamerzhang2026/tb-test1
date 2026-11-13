# Legado 书源调研报告（2026-11-11，以 yck2026.fun 为主）

## 格式要点
- 顶层必须是数组，UTF-8，无BOM，合法JSON。
- 单源必需：bookSourceName, bookSourceUrl（全站唯一，去重键）, bookSourceType（0=文字，1=音频，3=文件）, ruleSearch, ruleBookInfo, ruleToc, ruleContent。
- 可选：searchUrl（含 {{key}} {{page}}）, bookUrlPattern, ruleExplore, bookSourceGroup/Comment, enabled, header, loginUrl 等。
- 规则语法：CSS + @text/@href/@src，或 @json: / @xpath: / @regex: / js: 前缀。

## yck2026.fun 页面结构
- 入口：https://www.yck2026.fun/yuedu/shuyuan/index.html ，共5849条，每页约100条，形如 index.html?page=4。
- 主站 www.yckceo.com，备用 www.yck2026.fun，另有 www.yck2026.top。
- 详情页：/yuedu/shuyuan/content/id/<id>.html
- 单源 JSON 规律：/yuedu/shuyuan/json/id/<id>.json （id 与详情页一一对应，已用 id=6661 番茄（发现）双向证实）
- 聚合 JSON 规律：/yuedu/shuyuans/json/id/<id>.json （带s），详情页 /yuedu/shuyuans/content/id/<id>.html
- 首页文本抓取拿不到每条的下载链接（链接被剥离），不能按顺序猜 id。正确做法：真实浏览器点详情页取 id 再拼 JSON，或直接用聚合 JSON 一次导入成百上千个源再清洗。
- 内容混杂警告：除小说外还混有漫画、听书、短剧和大量成人/擦边条目，做纯小说 JSON 时必须按 bookSourceType=0 过滤并剔除短剧/成人源，不能整站照搬。
- 聚合实测：id=1135 自用(2) 约1500个源、6.51 MB，前500条多为小说站，也混有成人站，需清洗。

## 书源 JSON 链接清单（验证状态如实标注）
### B档 — 搜索引擎快照验证过 JSON 原文
- 番茄（发现） fanqienovel.com：https://www.yckceo.com/yuedu/shuyuan/json/id/6661.json

### A档 — 2026-01 转载记录，未逐个重验（域名 yck2026.top）
- 八零小说 6887, 爱去小说 6886, 团夕小说 6884, 八叉书库 6882, Lofter 6873, 顶点小说 6875, 必去读书库 6785, 幻梦轻小说 6280：https://www.yck2026.top/yuedu/shuyuan/json/id/<id>.json
- 聚合：灰灰 1026, 自用 1024, 刺猬喵 957, 沐鸢 950：https://www.yck2026.top/yuedu/shuyuans/json/id/<id>.json

### C档 — 详情页已确认，JSON 按规律推导，未逐个验证
- 番茄小说 1750, 89, 7992, 1807；小说网 6129；追书神器 3135；番茄怀旧 4515；哔哩轻小说 3253；书豪小说网 7952；天天看小说 6429：https://www.yckceo.com/yuedu/shuyuan/json/id/<id>.json
- 聚合自用(2) 1135：https://www.yck2026.fun/yuedu/shuyuans/json/id/1135.json

### 备选 GitHub 合集（主力备选）
1. luanmuc/legado-book-sources — 612纯小说源+1176净化规则，含 yckceo 前30合集，每周自动更新
   - https://github.com/luanmuc/legado-book-sources
   - books.json（最新 Release）
2. XIU2/Yuedu — 26精选源，老牌，但 2026-11 有失效报告
   - https://raw.githubusercontent.com/XIU2/Yuedu/master/shuyuan
   - jsDelivr 等多线路
3. suwa-ko/legado-sources — 一站一文件，all.json 一次导入全部
   - https://cdn.jsdelivr.net/gh/suwa-ko/legado-sources@main/all.json
4. cjj200011222/legado-booksource — 订阅式 all.json
   - https://cdn.jsdelivr.net/gh/cjj200011222/legado-booksource@master/all.json

## 大陆速度 / 书籍全不全 — 诚实说明
- 未在大陆实测，不能声称任何一条源大陆快、不卡。
- yck发布页称部分地区打不开是 DNS污染/运营商拦截/地区限制，建议换 DNS 223.5.5.5/223.6.6.6、切流量或代理。
- GitHub：raw 在大陆常慢/被墙；jsDelivr 国内一般可用，推荐；fastly/ghproxy/bitbucket 为备用。
- 书籍全不全无法仅凭清单判断；下载量最高：番茄 40752、八叉 35207、爱丽丝 35381 等；聚合包覆盖最广但混有重复/失效/成人源，“全”≠“可用”，必须做导入后校验（搜热门书、打开一章）并去重。
- 抓取脚本必须对每条 JSON 做三重校验：HTTP 200 + JSON解析 + 含 bookSourceName/bookSourceUrl/ruleSearch，失败即剔除。
