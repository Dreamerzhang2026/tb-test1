# TVBox 源调研报告（2026-11-11，新加坡 VM，未在大陆实测）

## 格式要点
- 顶层：spider, wallpaper, sites（必需）, parses, lives, doh, rules, ads 等。
- sites[]: key（唯一）, name, type（0=XML直连，1=JSON直连，3=Spider，4=JSON+Base64 ext）, api, ext, searchable/quickSearch/filterable 等。
- parses[]: {name,type,url,ext}；lives[]: {name,type,url,epg}；doh[]: {name,url}。
- 脏格式处理：剥离行首 // 注释；相对路径 ./jar ./lib ./py ./js 需解析为绝对URL或只保留 type=0/1；多仓索引 {urls:[{name,url}]} 需二次解析；spider 若不是 .jar 结尾不要原样保留。

## 候选（已验证打得开的优先）
1. hebijunge/tvbox-config tvbox_recommended.json — 1708站实测连通，每日03:00更新
   - https://cdn.jsdelivr.net/gh/hebijunge/tvbox-config@main/tvbox_recommended.json
   - raw: https://raw.githubusercontent.com/hebijunge/tvbox-config/main/tvbox_recommended.json
   - 另有 exports/healthy.json
2. TVboxorg/TVbox dist/official.json — CI探测合并，2026-09-28新建，持续更新
   - https://cdn.jsdelivr.net/gh/TVboxorg/TVbox@main/dist/official.json
   - raw: https://raw.githubusercontent.com/TVboxorg/TVbox/main/dist/official.json
3. qist/tvbox jsm.json — 12809 Stars，151源
   - https://raw.githubusercontent.com/qist/tvbox/master/jsm.json
   - Cloudflare Pages: https://qist.wyfc.qzz.io/jsm.json
   - 注意大量相对路径，需处理
4. cluntop/tvbox box.json — 671 Stars
   - https://raw.githubusercontent.com/cluntop/tvbox/main/box.json
   - https://clun.top/box.json
5. gaotianliuyun/gao js.json — 8185 Stars
   - https://raw.githubusercontent.com/gaotianliuyun/gao/master/js.json
6. jinenge/tvbox tvbox.json — 1206 Stars，开头有 // 注释需剥离
   - https://raw.githubusercontent.com/jinenge/tvbox/main/tvbox.json
   - https://jinenge.us.kg/app/tvbox/tvbox.json
7. Lightconer/tvbox-ysc-config output/单仓聚合.json — 每日两次更新，2026-11-11 9/10成功，需URL编码中文文件名
   - raw 需编码：https://raw.githubusercontent.com/Lightconer/tvbox-ysc-config/main/output/%E5%8D%95%E4%BB%93%E8%81%9A%E5%90%88.json
8. Lightconer/TVBox-Sources output/tvbox.json — 仅直播，sites为空，每6小时更新
   - https://cdn.jsdelivr.net/gh/Lightconer/TVBox-Sources@main/output/tvbox.json
9. hengge345666/tvbox-subs subs.json — 多仓索引 {urls:[]}
   - https://raw.githubusercontent.com/hengge345666/tvbox-subs/main/subs.json

## 排除
- 2hacc/TVBox 已停更（2024-12-15），排除。
- 饭太硬 http://www.饭太硬.net/tv 不稳定，2026-11-11抓取失败，不作默认唯一源。

## 大陆速度说明（必须向用户声明）
- 本次未在大陆实测，均为启发式：jsDelivr国内有节点、多个仓库README写国内优先用jsDelivr；raw.githubusercontent.com国内常慢；ghproxy等第三方代理不稳定；cnb.cool理论最快但候选极少。
- 配置文件下载快 ≠ 播放不卡，真正决定卡不卡的是每个 sites api 指向的 CMS 站和 m3u8 源站带宽。
- 抓取器应：多通道回退 jsDelivr → ghproxy → raw，超时10-15s；按 key 去重；截断到150-300优质站（不要全量1708站塞进去）；保留 parses 全集和 lives。
- 合规提示：qist/gao README写仅供学习、禁止商用、请不要在中华人民共和国境内使用。
