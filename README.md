# nonebot-plugin-awmc-arcade

[![python3](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](https://opensource.org/licenses/MIT)

[nonebot-plugin-awmc-helper](https://github.com/shinyashen/nonebot-plugin-awmc-helper) 生态的第三方机厅插件：机厅人数上报、附近机厅查找、线上排卡、Nearcade 云同步。迁移自 [YuuzukiRin/nonebot_plugin_mai_arcade](https://github.com/YuuzukiRin/nonebot_plugin_mai_arcade) v0.2.0，按 awmc 生态约定重写了存储、HTTP 与会话层。

> **替代内置机厅插件**：主插件内置的 `awmc.arcade` 与本插件指令大量同名，二者只能启用其一。启用本插件请把 `arcade` 加入主插件 `awmc_disabled_plugins` 配置。内置机厅表（华立官方库/订阅）不会自动迁移，群内需重新「添加群聊 + 添加机厅」。

## 特性

- **每群独立机厅表**：群主/管理员开通后，通过 Nearcade 搜索把机厅加入本群（自动挂店铺链接与地图），跨群互不可见；
- **人数上报 + 云同步**：`店名++`、`店名+3`、`店名=8` 一句话上报，自动与 Nearcade 云端出勤互相校平，并按机台数估算等待时间给出勤建议；
- **线上排卡**：`排卡 <机厅>` 入队、`上机` 轮转、`延后` 调序、`闭店` 清队，队列按用户 id 记账（重名/改名不会错位）；
- **附近机厅**：群内直接发送位置消息，基于 Nearcade 数据库返回 3 家最近机厅。支持的分享形态（坐标提取后统一纠偏到 GCJ-02）：
  - 旧版 QQ 位置卡片（json `meta.Location.Search`）与新版图文卡（`com.tencent.tuwen.lua`，坐标在跳转链接 `coord` 参数）；
  - 高德分享：`surl.amap.com` 短链跟随 302 取坐标；
  - QQ 地图 H5 链接与谷歌图钉/坐标链接（`coord=`、`@lat,lng`、`!3d…!4d…`，谷歌为 WGS-84 自动纠偏）；
  - 百度 marker 链接（`location=`，BD-09 自动纠偏）；百度/谷歌的**按地点名分享短链解析不出坐标，暂不支持**（静默忽略）；
- **别名与序号索引**：机厅、别名、地图 URL 全部支持序号操作，群内口播友好；
- **静默监听模式**：SUPERUSER 可对群关闭人数上报的确认回复（只保留云同步），配置持久化。

## 要求

- NoneBot2 ≥ 2.4.3，Python ≥ 3.10
- [nonebot-plugin-awmc-helper](https://github.com/shinyashen/nonebot-plugin-awmc-helper)（主插件，复用其 HTTP 智能代理、异常兜底与合并转发能力）
- OneBot v11 适配器（位置监听与群管权限依赖 v11 协议）
- 一个 [Nearcade](https://nearcade.cn) 开发者 API 令牌（用于人数上传；不配置则使用上游公开的开发令牌，仅保证可用，建议替换）

## 安装

**方式一（主推）：awmc_plugins/ 目录即装**

主插件支持 `awmc_plugins/` 固定目录的「clone 即安装」：

```bash
cd <bot 工作目录>
git clone https://github.com/shinyashen/nonebot-plugin-awmc-arcade.git awmc_plugins/nonebot-plugin-awmc-arcade
```

重启 bot 即自动加载。同时在 `.env` 停用内置机厅插件：

```dotenv
AWMC_DISABLED_PLUGINS='["arcade"]'
```

**方式二：pip / PyPI**

```bash
pip install nonebot-plugin-awmc-arcade
```

随后将 `"nonebot_plugin_awmc_arcade"` 加入 bot 的加载列表。

## 配置

| 配置项 | 默认 | 说明 |
|---|---|---|
| `awmc_arcade_nearcade_api_token` | 上游公开开发令牌 | Nearcade 开发者 API 令牌（人数上传） |
| `awmc_arcade_smart_tips` | 5 档默认提示 | 等待时间提示规则（`max_minutes` 升序，`tip` 文案） |
| （复用主插件）`awmc_arcade_max_delta` | `30` | 单次人数变更上限（±，超过拒绝）；与内置排卡同名字段，单一来源 |
| `awmc_arcade_manage_ttl` | `1800` | 私聊「管理群」上下文的存活秒数 |
| `awmc_arcade_max_count` | `100` | 人数合法区间上限 |
| `awmc_arcade_per_round_minutes` | `16` | 单轮游玩时长（分钟），等待时间估算基数 |
| `awmc_arcade_nearby_radius_km` | `10` | 附近机厅发现半径（公里） |
| `awmc_arcade_session_ttl` | `120` | 搜索选择/追问会话的存活秒数 |

```dotenv
AWMC_ARCADE_NEARCADE_API_TOKEN=nk_xxxxxxxx
```

## 指令

| 指令 | 权限 | 说明 |
|---|---|---|
| `机厅help` / `机厅帮助` | — | 完整指令说明（合并转发展示） |
| `管理群 <群号>` | 私聊 | 设置私聊管理目标（30 分钟内有效）；`管理群` 查看、`管理群 取消` 清除 |
| `添加群聊` / `删除群聊` | 管理 | 开通/关闭本群排卡功能 |
| `静默监听模式` / `关闭静默监听模式` | SUPERUSER | 开关人数上报确认回复（持久化） |
| `添加机厅 <店名> [@省 @市 …]` | 管理 | Nearcade 搜索后回复序号选择添加（合并转发菜单，每页 10 家；`更多` 翻页、`原名` 直接添加搜索词、`取消` 放弃；`@地区词` 按省/市/区过滤重名机厅，如 `添加机厅 天空之城 @南京`；转发不可用降级单条文本） |
| `删除机厅 <店名/序号>` | 管理 | 从本群移除机厅 |
| `机厅列表` / `群机厅` | — | 本群机厅列表（序号索引依据） |
| `添加机厅别名 <店名/序号> <别名>` | 管理 | 添加别名 |
| `删除机厅别名 <店名/序号> <别名/序号>` | 管理 | 删除别名 |
| `机厅别名 <店名/序号>` | — | 查看别名列表 |
| `添加机厅地图 <店名/序号> <URL>` | 管理 | 添加音游地图网址 |
| `删除机厅地图 <店名/序号> <URL/序号>` | 管理 | 删除地图网址 |
| `机厅地图 <店名/序号>` | — | 查看地图网址列表 |
| `<店名>++/--/+n/-n` | — | 人数增减（自动同步 Nearcade） |
| `<店名>=n / <店名>n` | — | 人数重置为 n（显式设置，不向云端合并） |
| `<店名>几/几人/j` | — | 查询人数与预计等待 |
| `mai` / `机厅人数` / `jtj` / `机厅几人` | — | 当日已更新机厅列表 |
| `排卡 <店名/序号>` | — | 加入排队队列 |
| `上机` / `退勤` / `延后` | — | 队列轮转 / 退出 / 延后一位 |
| `排卡现状 <店名/序号>` | — | 查看当前队列 |
| `闭店 <店名/序号>` | 管理 | 清空该机厅队列 |

发送位置消息可发现附近机厅（Nearcade 数据，取最近 3 家）。

## 私聊管理

SUPERUSER 或某群的管理员可以在**私聊**执行管理/查询类指令，避免设置期在群里刷屏：

```text
管理群 123456          ← 设置私聊管理目标（默认 30 分钟内有效）
添加机厅 某店          ← 直接作用于目标群（含 Nearcade 搜索选择交互）
删除机厅 1             ← 序号操作照常
管理群                 ← 查看当前目标；管理群 取消 清除
```

也可以不设上下文，在指令开头临时带群号（≥4 位，与机厅序号天然区分）：`添加机厅 123456 某店`。

- **身份校验**：SUPERUSER 直通；其余用户经 `get_group_member_info` 直查目标群角色（admin/owner 放行，结果缓存 5 分钟），bot 不在目标群时提示校验失败；
- **覆盖指令**：添加/删除群聊、添加/删除机厅、别名与地图增删查、机厅列表、机厅别名、机厅地图、排卡现状、闭店、静默监听模式（SUPERUSER）；
- **不开放私聊**：人数上报、排卡/上机/退勤/延后、mai/机厅人数（排队与上报者是群成员本人，私聊无语义）。

## 与上游的主要差异

- **存储**：进程内 JSON 全局 dict → 独立 SQLite（localstore 数据目录 `awmc_arcade.db`），全异步读写；上游「重启丢静默配置」「并发写坏 JSON」两类问题随之消除；
- **HTTP**：上报链路的同步 `http.client`（阻塞整个事件循环）→ 共享 `httpx.AsyncClient`；
- **会话**：`got`/`pause` 与 `^[1-6]$` 全局正则（会吞掉群里所有单个数字消息）→ TTL 会话表 + priority=0 定向消费，会话超时自动失效；
- **权限**：添加机厅地图补上上游缺失的管理员检查（帮助文案本就标注「管理」）；
- **语义修正**：显式设置人数（`店名=5`）为绝对值；相对增减（`++/--`）保持上游「云端有他人上报时增量叠加」的合并语义；
- **静默模式**：只吞人数上报确认，查询照常回答（上游静默时查询几乎无输出的行为比较费解）；
- **私聊扩权**：新增「管理群」上下文与前导群号语法，SUPERUSER/目标群管理员可在私聊完成全部设置（见上节）；
- **搜索菜单**：添加机厅的候选列表改为合并转发（标题/操作/逐店分节点），序号专用于选店、动作换「更多/原名/取消」非数字词，每页 10 家翻页追加、序号稳定；转发不可用降级单条文本（上游为固定 1-6 数字菜单）；
- **地区过滤**：`@地区词` 走 Nearcade 官方地区树（`regionId` 参数）过滤重名机厅，支持省/市/区逐级与中文直达（`@南京` 自动下钻到南京市），地区树本地缓存 7 天；
- **Nearcade 接口修正**：店铺数字 id 是全局 id，与 `/shops/{source}/{数字}` 按 source 隔离的编号空间无关——云同步与网页链接一律走无 source 的全局路径（上游拼 `bemanicn` 会命中完全不同的店铺）；人数上传挂在 maimai DX 机种的 gameId 上（上游取 games[0]，多机种店铺会数错格）；
- **生态复用**：HTTP 层接入主插件智能代理、异常兜底复用 `core.utils.handle_errors`、长文帮助走主插件合并转发（LLBot 实测兼容实现）。

## 截图

<details>
<summary><strong>📍 附近机厅</strong>：基于 Nearcade 数据库的位置服务，在群聊发送位置可发现附近机厅</summary>

![image](./docs/discover_nearby_arcades.png)

</details>

<details>
<summary><strong>⏰ 实时建议</strong>：查询机厅人数显示最新上报用户及上报时间，从 Nearcade 同步实时人数信息，同时根据人数和机台信息给出出勤建议</summary>

![image](./docs/live_suggestion.png)

</details>

<details>
<summary><strong>🔎 添加机厅</strong>：添加时关键词搜索 Nearcade 收录的机厅，选择后自动挂店铺链接与地图</summary>

![image](./docs/search_add_arcades.png)

</details>

## 许可证

MIT（继承上游 © YuuzukiRin 与本仓修改）。
