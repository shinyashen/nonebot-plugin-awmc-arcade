"""域逻辑：机厅解析、人数换算与 Nearcade 云同步编排、搜索会话流转、文案组装。

本模块无注册副作用、不发消息，只做「输入 → 文案/数据」的纯编排，
供 matchers.py 的各 handler 复用（指令入口与会话消费 matcher 共用同一份流程）。
"""

import re
import json
import math
import time
import asyncio
from datetime import datetime
from dataclasses import dataclass

from nonebot import logger
from nonebot_plugin_awmc_helper.config import plugin_config as awmc_helper_config

from . import session, nearcade
from .store import ArcadeEntry, store
from .config import plugin_config

# 人数上报解析：机厅名 + 操作符 + 数字。与上游正则等价
# （名++/--、名+3/-3、名=5/==5、名5 裸数字重置）
_COUNT_RE = re.compile(r"^([\u4e00-\u9fa5\w]+?)([+\-=]{0,2})(\d*)$")

OP_INC = "inc"
OP_DEC = "dec"
OP_SET = "set"


async def resolve_arcade(
    group_id: int, text: str, *, by_alias: bool = True
) -> ArcadeEntry | None:
    """解析机厅：序号（群内列表 1 起）→ 本名 →（可选）别名。

    管理类操作（删除/加别名）按上游惯例不接受别名定位，传 by_alias=False。
    """
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        entries = await store.list_arcades(group_id)
        idx = int(text) - 1
        if 0 <= idx < len(entries):
            return entries[idx]
    if entry := await store.get_arcade_by_name(group_id, text):
        return entry
    if by_alias:
        return await store.find_arcade_by_alias(group_id, text)
    return None


def parse_count(text: str) -> tuple[str, str, int | None] | None:
    """解析人数上报形态，非上报消息（裸词、无操作）返回 None。"""
    m = _COUNT_RE.match(text.strip())
    if not m:
        return None
    name, op, num_str = m.groups()
    if not op and not num_str:
        return None  # 裸词，与人数无关
    if op in ("=", "==") and not num_str:
        return None  # 等号后没有数字，无意义
    if not op and name.isdigit():
        # 裸数字重置的「名」必须含非数字字符：\w 含数字，纯数字串会被
        # 非贪婪拆成「首字符序号 + 其余人数」（211 → 2号厅=11），任意
        # 数字串由此大范围误触；序号操作走显式操作符（2=11 / 2+11）不受限
        return None
    num = int(num_str) if num_str else None
    if op in ("+", "++"):
        return name, OP_INC, num
    if op in ("-", "--"):
        return name, OP_DEC, num
    return name, OP_SET, num  # = / == / 裸数字


async def compute_count(
    entry: ArcadeEntry, op: str, num: int | None
) -> tuple[int, int, int] | str:
    """人数换算，返回 (当前值, 新值, 实际增量)；非法时返回错误文案。"""
    cfg = plugin_config
    current = await store.current_count(entry.group_id, entry.key)
    # 单次变更上限复用主插件同名配置（内置排卡同语义，单一来源）
    max_delta = awmc_helper_config.awmc_arcade_max_delta
    if op in (OP_INC, OP_DEC):
        # 显式 +0/-0 是「无变化」的合法路径，不得因 falsy 判定当作 +1
        delta = 1 if num is None else num
        if abs(delta) > max_delta:
            return "检测到非法数值，拒绝更新"
        new_num = current + (delta if op == OP_INC else -delta)
        if not 0 <= new_num <= cfg.awmc_arcade_max_count:
            return "检测到非法数值，拒绝更新"
        return current, new_num, new_num - current
    if num is None or not 0 <= num <= cfg.awmc_arcade_max_count:
        return "检测到非法数值，拒绝更新"
    return current, num, num - current


async def _shop_id_of(entry: ArcadeEntry) -> str | None:
    """店铺 id：条目缓存 → 条目详情链接解析 → 按添加顺序扫描地图网址。"""
    if entry.nearcade_shop_id:
        return entry.nearcade_shop_id
    if entry.nearcade_url and (sid := shop_id_from_url(entry.nearcade_url)):
        return sid
    for url in await store.list_maps(entry.group_id, entry.key):
        if sid := shop_id_from_url(url):
            return sid
    return None


def shop_id_from_url(url: str) -> str | None:
    """从 Nearcade 店铺网页链接提取店铺 id（域名限定，防误绑任意店铺）。

    此前的「路径数字后缀」启发式会把任意以数字结尾的网址解析成店铺 id
    （读数合并进本地账本、上传写进别人店铺），故收紧为只认
    nearcade.cn/shops/ 的全局路径——按数据源隔离的编号空间与全局 id
    无关（见 nearcade.py 模块注记）。
    """
    m = re.fullmatch(r"(?:https?://)?(?:www\.)?nearcade\.cn/shops/(\d+)/?", url.strip())
    return m.group(1) if m else None


def _tip_for(wait_minutes: int) -> str:
    tips = plugin_config.awmc_arcade_smart_tips
    return next((r.tip for r in tips if wait_minutes <= r.max_minutes), tips[-1].tip)


def estimate_msg(name: str, count: int, coutnum: int, *, updated: bool) -> str:
    """按机台数估算等待时间的人数文案（updated 切换「已更新为/为」措辞）。"""
    cfg = plugin_config
    per_round = max(int(coutnum), 1) * 2  # 每轮最多游玩人数（至少按 1 台算）
    label = "人数已更新为" if updated else "人数为"
    head = (
        f"📍 {name}  {label} {count}\n🕹️ 机台数量：{coutnum} 台（每轮 {per_round} 人）"
    )
    queue_num = max(count - per_round, 0)
    if queue_num <= 0:
        return f"{head}\n\n{_tip_for(0)}"
    per = cfg.awmc_arcade_per_round_minutes
    avg = round(queue_num / per_round * per)
    lo_rounds = queue_num // per_round
    hi_rounds = -(-queue_num // per_round)  # 向上取整轮数
    tip = _tip_for(avg)
    return (
        f"{head}\n\n"
        f"⌛ 预计等待：约 {avg} 分钟\n"
        f"   ↳ 范围：{lo_rounds * per}~{hi_rounds * per} 分钟"
        f"（{lo_rounds}~{hi_rounds} 轮）\n\n💡 {tip}"
    )


async def _shop_games(shop_id: str) -> list[dict]:
    """云端店铺的机种列表（失败返回空）。"""
    info = await nearcade.get_shop(shop_id)
    return (info or {}).get("shop", {}).get("games", [])


def maimai_game_of(games: list[dict], *, fallback: int = 1) -> tuple[int, int | None]:
    """扫描云端机种列表取 maimai DX：返回 (机台数, 上传 gameId)。

    上传必须挂在 maimai DX 机种上——店铺详情 games[0] 可能是太鼓等其他
    机种，兜底取 games[0] 数错格会把人数写进别的游戏的出勤计数；无该
    机种时 gameId 为 None（调用方走「未获取上传通道」分支），机台数取
    ``fallback``（调用方条目缓存值）。
    """
    for game in games:
        if game.get("name") == "maimai DX":
            return max(int(game.get("quantity", 1) or 1), 1), game.get("gameId")
    return fallback, None


async def _maimai_coutnum(entry: ArcadeEntry, shop_id: str) -> int:
    """从云端店铺详情刷新 maimai DX 机台数（无该机种沿用条目缓存）。"""
    coutnum, _ = maimai_game_of(await _shop_games(shop_id), fallback=entry.coutnum)
    return coutnum


async def apply_count_update(
    entry: ArcadeEntry, op: str, num: int | None, user_name: str, *, silent: bool
) -> str | None:
    """人数上报全流程：本地记账 → 云端增量合并 → 上传 → 回复文案。

    静默模式只吞掉上传结果回复（成功/关门/上传失败），非法数值、无店铺
    关联、无上传通道等前置错误提示保留，返回 None。
    与上游的一处有意偏差：显式设置（名=5）为绝对值，不再向云端看齐合并；
    相对增减（名+1/--2）保持上游语义——云端已有他人上报时，本次增量
    叠加到云端值上。

    读-并-写-传四步按（群, 机厅）维度加锁串行：整群同时打卡是常态，
    无锁时两人都基于同一旧值算增量，云端只净加 1 且本地账本重复记账。
    """
    async with _count_lock(entry.group_id, entry.key):
        return await _apply_count_update(entry, op, num, user_name, silent=silent)


_COUNT_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}


def _count_lock(group_id: int, arcade_id: int) -> asyncio.Lock:
    lock = _COUNT_LOCKS.get((group_id, arcade_id))
    if lock is None:
        lock = _COUNT_LOCKS[(group_id, arcade_id)] = asyncio.Lock()
    return lock


async def _apply_count_update(
    entry: ArcadeEntry, op: str, num: int | None, user_name: str, *, silent: bool
) -> str | None:
    computed = await compute_count(entry, op, num)
    if isinstance(computed, str):
        return computed
    current, new_num, _delta = computed
    group_id, arcade_id = entry.group_id, entry.key
    now = datetime.now().strftime("%H:%M")

    shop_id = await _shop_id_of(entry)
    if shop_id is None:
        # 无云端店铺关联：仅本地记账（上游无地图网址时的行为）
        await store.add_count_log(group_id, arcade_id, new_num - current, user_name)
        return f"[{entry.name}] 当前人数更新为 {new_num}\n由 {user_name} 于 {now} 更新"

    if op in (OP_INC, OP_DEC):
        cloud = await nearcade.get_attendance(shop_id)
        if cloud is not None and cloud != current:
            new_num = cloud + (new_num - current)

    if op == OP_SET:
        await store.reset_count(group_id, arcade_id, new_num, user_name)
    else:
        await store.add_count_log(group_id, arcade_id, new_num - current, user_name)

    # 机台数与上传 gameId 同源自一次店铺详情请求，扫描口径单源
    # maimai_game_of（无 maimai DX 机种时 game_id 为 None，走下方
    # 「未获取上传通道」分支，不得兜底取 games[0]）
    coutnum, game_id = maimai_game_of(
        await _shop_games(shop_id), fallback=entry.coutnum
    )
    if coutnum != entry.coutnum:
        await store.update_nearcade_info(arcade_id, coutnum=coutnum)
    msg = estimate_msg(entry.name, new_num, coutnum, updated=True)

    if game_id is None:
        return f"{msg}\n\n⚠️ 未能在 Nearcade 获取上传通道，人数仅记录在本群"
    status, text = await nearcade.upload_attendance(
        shop_id, game_id, new_num, plugin_config.awmc_arcade_nearcade_api_token
    )
    if silent:
        return None
    if status == 200:
        return f"感谢使用，机厅人数已上传 Nearcade\n{msg}"
    if status == 400:
        return f"似乎在Nearcade上这家店关门了😴\n{msg}"
    status_text = status if status > 0 else "网络错误"
    logger.warning(f"Nearcade 上传异常（{shop_id}）：{status_text} {text}")
    return f"上传失败: {status_text}\n返回信息: {text}\n\n{msg}"


async def count_query_reply(entry: ArcadeEntry) -> str:
    """「XX几/几人/j」查询：云端有新数据时先对齐再作答。"""
    group_id, arcade_id = entry.group_id, entry.key
    shop_id = await _shop_id_of(entry)
    if shop_id:
        # 对齐段（读云端→比对→回写）与上报持同一把锁：无锁时查询读旧值
        # 挂起期间一次上报完成，恢复后 reset_count(旧云端值) 会覆盖上报结果
        async with _count_lock(group_id, arcade_id):
            cloud = await nearcade.get_attendance(shop_id)
            current = await store.current_count(group_id, arcade_id)
            if cloud is not None and cloud != current:
                await store.reset_count(group_id, arcade_id, cloud, "Nearcade")
    count = await store.current_count(group_id, arcade_id)
    if count <= 0:
        return f"[{entry.name}] 今日人数尚未更新\n你可以爽霸机了\n快去出勤吧！"
    coutnum = entry.coutnum
    if shop_id:
        coutnum = await _maimai_coutnum(entry, shop_id)
        if coutnum != entry.coutnum:
            await store.update_nearcade_info(arcade_id, coutnum=coutnum)
    msg = estimate_msg(entry.name, count, coutnum, updated=False)
    if last := await store.last_count_update(group_id, arcade_id):
        by = last.updated_by or "未知"
        msg += f"\n（{by} · {last.updated_at.strftime('%H:%M')}）"
    return msg


async def updated_today_reply(group_id: int) -> str:
    """「mai/机厅人数」：当日有更新记录的机厅列表（汇总单次拉全）。"""
    stats = await store.count_today_stats(group_id)
    lines = []
    for entry in await store.list_arcades(group_id):
        if (stat := stats.get(entry.key)) is None:
            continue
        last, count = stat
        by = last.updated_by or "未知"
        lines.append(
            f"[{entry.name}] {count}人 \n（{by} · {last.updated_at.strftime('%H:%M')}）"
        )
    if not lines:
        return "📋 今日机厅人数更新情况\n\n暂无更新记录\n您可以爽霸机了"
    return "📋 今日机厅人数更新情况\n\n" + "\n".join(lines)


# ---- 添加机厅（Nearcade 搜索交互） ----

# 地区词解析：@江苏 @南京 逐级下钻；首词若不是国家则默认在中国境内查找
REGION_CACHE_TTL = 7 * 86400.0  # 地区树几乎不变，子级缓存 7 天
_AT_REGION = re.compile(r"@(\S+)")


async def _region_children(parent: str | None) -> list[dict]:
    """地区树子级（meta 缓存 7 天，省/市级条目几十条）。"""
    key = f"region:children:{parent or '_root'}"
    now = time.time()
    raw = await store.get_meta(key)
    if raw:
        try:
            data = json.loads(raw)
            if now - data["ts"] < REGION_CACHE_TTL:
                return data["items"]
        except Exception:
            pass
    items = await nearcade.region_children(parent)
    if items:
        try:
            await store.set_meta(
                key, json.dumps({"ts": now, "items": items}, ensure_ascii=False)
            )
        except Exception:  # 缓存写失败只损失加速，不影响解析
            logger.debug("地区树缓存写入失败", exc_info=True)
    return items


def _region_hits(items: list[dict], token: str) -> list[dict]:
    exact = [
        r
        for r in items
        if r.get("label") == token or r.get("id") == token or r.get("value") == token
    ]
    if exact:
        return exact[:1]
    return [r for r in items if token in str(r.get("label", ""))]


async def resolve_region(tokens: list[str]) -> tuple[str, str] | str:
    """逐级解析 @地区词链，返回 (regionId, 展示路径)；失败返回错误文案。

    首词若不命中任何国家，默认落到中国的省级继续找（覆盖「@南京」这类
    省略国名/省名的写法）；市/区级靠逐级下钻（子级列表缓存后零请求）。
    """
    parent: str | None = None
    path: list[str] = []
    for i, tok in enumerate(tokens):
        items = await _region_children(parent)
        hits = _region_hits(items, tok)
        if not hits and parent is None and i == 0:
            cn = next(
                (r for r in items if r.get("id") == "CN" or r.get("label") == "中国"),
                None,
            )
            if cn is not None:
                parent = str(cn["id"])
                path.append(str(cn.get("label") or parent))
                items = await _region_children(parent)
                hits = _region_hits(items, tok)
        if not hits and parent is not None and i == 0 and len(path) == 1:
            # 省级仍无命中：并发扫各省子级（市级直达，如「@南京」）——
            # 冷缓存串行拉 30+ 省要数秒，gather 压到单省量级；信号量限 8
            # 防一次打爆 nearcade
            sem = asyncio.Semaphore(8)

            async def _children(pid: str) -> list[dict]:
                async with sem:
                    return await _region_children(pid)

            children_lists = await asyncio.gather(
                *(_children(str(prov["id"])) for prov in items)
            )
            for prov, children in zip(items, children_lists):
                sub = _region_hits(children, tok)
                if sub:
                    hits = sub
                    parent = str(prov["id"])
                    path = [str(prov.get("label") or parent)]
                    break
        if not hits:
            options = "、".join(str(r.get("label")) for r in items[:8])
            suffix = "…" if len(items) > 8 else ""
            scope = f"「{path[-1]}」下" if path else ""
            return f"未找到地区「{tok}」：{scope}可用 {options}{suffix}"
        if len(hits) > 1:
            names = "、".join(str(h.get("label")) for h in hits[:6])
            return f"地区「{tok}」有歧义：{names}？请用更完整的名称"
        hit = hits[0]
        parent = str(hit["id"])
        path.append(str(hit.get("label") or parent))
    assert parent is not None  # tokens 非空时循环内必然已为 parent 赋值
    return parent, "/".join(path)


def format_shop_info(shop: dict, index: int) -> str:
    """搜索候选条目文案。"""
    name = shop.get("name", "未知机厅")
    address = shop.get("address", {}).get("detailed", "地址未知")
    games = [
        f"{g.get('name', '未知游戏')}（{g.get('quantity', 0)}台）"
        for g in shop.get("games", [])
        if g.get("quantity", 0) > 0
    ]
    games_str = " | ".join(games[:2]) if games else "游戏信息未知"
    return f"{index}. {name}\n   📍 {address}\n   🎮 {games_str}"


def shop_web_url(shop: dict) -> str:
    """Nearcade 店铺网页链接（全局 id，无 source——见 nearcade.py 模块注记）。"""
    shop_id = shop.get("id")
    if not shop_id:
        return ""
    return f"https://nearcade.cn/shops/{shop_id}"


# 搜索菜单动作词（非数字——数字专用于选店序号，避免 4/5/6 与店序混淆）
SEARCH_ACTIONS = ("更多", "原名", "取消")
# 每页拉取的候选数（合并转发承载长列表；「更多」追加下一页、序号延续）
SEARCH_PAGE_LIMIT = 10


@dataclass
class AddMenu:
    """添加机厅搜索菜单：合并转发节点（标题/操作/逐店）+ 降级纯文本。"""

    text: str  # 合并转发不可用时的单条降级文案（内容与节点一致）
    nodes: list[str]  # 转发节点：[找到 N 家, 操作说明, 机厅…]


def _menu_actions(query: str, has_more: bool) -> str:
    """操作说明：数字只用于选店，动作一律非数字词。

    地区全量检索（无关键词）没有「原名」可加——空店名不可落库。
    """
    lines = ["回复序号 选择对应机厅"]
    if has_more:
        lines.append("「更多」 查看更多结果")
    if query:
        lines.append(f"「原名」 直接添加「{query}」")
    lines.append("「取消」 放弃操作")
    return "\n".join(lines)


def _build_menu(
    query: str, total: int, shops: list[dict], *, region_label: str = ""
) -> AddMenu:
    """由累计候选构建菜单（合并转发节点 + 同内容降级单条文本）。"""
    total = max(total, len(shops))
    has_more = total > len(shops)
    where = f"（{region_label}）" if region_label else ""
    actions = _menu_actions(query, has_more)
    shops_text = "\n\n".join(
        format_shop_info(shop, i) for i, shop in enumerate(shops, 1)
    )
    text = f"🔍 找到 {total} 个相关机厅{where}：\n\n{shops_text}\n\n{actions}"
    nodes = [
        f"🔍 找到 {total} 个相关机厅{where}",
        actions,
        *(format_shop_info(shop, i) for i, shop in enumerate(shops, 1)),
    ]
    return AddMenu(text=text, nodes=nodes)


async def _region_overflow(total: int, region_label: str, region_id: str) -> AddMenu:
    """地区全量检索超上限：不起选择会话，转发提示改用更细地区缩小。

    节点携带该地区下辖子级（地区树已缓存，零请求）供用户直接照抄。
    """
    cap = plugin_config.awmc_arcade_search_max_total
    leaf = region_label.rsplit("/", 1)[-1]
    nodes = [f"🔍 「{leaf}」共收录 {total} 家机厅，超过单次可浏览上限（{cap} 家）"]
    children = await _region_children(region_id)
    if children:
        names = "、".join(str(c.get("label")) for c in children)
        nodes.append(f"📍 「{leaf}」下辖：{names}")
    nodes.append("💡 换个更细的地区再来，如：添加机厅 @江苏 @南京")
    return AddMenu(text="\n\n".join(nodes), nodes=nodes)


async def begin_add_arcade(
    scope: int, group_id: int, user_id: str, name: str
) -> "str | AddMenu":
    """添加机厅入口：命中搜索则挂选择会话并返回菜单，否则直接添加。

    ``scope`` 是会话表的键维度（群聊=群号、私聊=0），``group_id`` 是
    落库目标群——私聊扩权时两者不同。
    只带 ``@地区`` 不带店名时按地区全量检索（Nearcade 官方支持空关键词
    + regionId）；超过可浏览上限不起会话，提示缩小地区，且无「原名」
    直加兜底（空店名不可落库）。
    """
    region_tokens = _AT_REGION.findall(name)
    name = _AT_REGION.sub("", name).strip()
    if not name and not region_tokens:
        return "请输入机厅名称，可带 @地区 缩小范围，如：添加机厅 天空之城 @南京"
    region_id: str | None = None
    region_label = ""
    if region_tokens:
        resolved = await resolve_region(region_tokens)
        if isinstance(resolved, str):
            return resolved
        region_id, region_label = resolved
    if name and await store.get_arcade_by_name(group_id, name):
        return "机厅已在群聊中"
    result = await nearcade.search_shops(
        name, limit=SEARCH_PAGE_LIMIT, region_id=region_id
    )
    shops = result.get("shops", [])
    total = max(int(result.get("totalCount", 0)), len(shops))
    if not name:
        # 地区全量检索：超上限提示缩小地区；无结果不落空名机厅
        if total > plugin_config.awmc_arcade_search_max_total:
            assert region_id is not None  # 空名称走到这里必然带过 @地区词
            return await _region_overflow(total, region_label, region_id)
        if not shops:
            return f"Nearcade 未收录{region_label}的机厅"
    elif not shops:
        await store.add_arcade(group_id, name, created_by=user_id)
        return f"未找到相关机厅，已直接添加「{name}」到群聊名单中"
    session.start(
        session.KIND_SEARCH,
        scope,
        user_id,
        payload={
            "group_id": group_id,
            "shops": shops,
            "query": name,
            "page": 1,
            "total": total,
            "region_id": region_id,
            "region_label": region_label,
            "created_by": user_id,
        },
    )
    return _build_menu(name, total, shops, region_label=region_label)


async def _add_from_shop(group_id: int, shop: dict, fallback: str, user_id: str) -> str:
    """按搜索候选落库：写店铺关联并自动添加地图网址（上游行为）。"""
    name = shop.get("name") or fallback
    if await store.get_arcade_by_name(group_id, name):
        return f"机厅「{name}」已在群聊中"
    url = shop_web_url(shop)
    shop_id = str(shop.get("id")) if shop.get("id") else None
    entry = await store.add_arcade(
        group_id,
        name,
        created_by=user_id,
        source=shop.get("source"),  # 搜索结果国内店多为 None，仅留档不作链接依据
        shop_id=shop_id,
        shop_url=url or None,
    )
    assert entry is not None  # 上方已查重，同事件循环内无竞态
    reply = f"✅ 已添加机厅：{name}"
    if url:
        await store.add_map(group_id, entry.key, url)
        reply += f"\n🔗 详情链接：{url}\n🗺️ 已添加机厅地图"
    return reply


async def continue_search(
    scope: int, user_id: str, choice: str
) -> "str | AddMenu | None":
    """搜索选择会话续接：数字选店 / 「更多」翻页 / 「原名」直加 / 「取消」。

    翻页为**追加**语义——已列出的序号保持不变，新候选延续编号；
    落库目标群取自会话 payload（私聊扩权时 ≠ 会话键 scope）。
    返回 None 表示输入无效（调用方静默吞掉，与上游一致）。
    """
    sess = session.get(scope, user_id)
    if sess is None or sess.kind != session.KIND_SEARCH:
        return None
    payload = sess.payload
    shops: list[dict] = payload["shops"]
    query: str = payload["query"]
    page: int = payload["page"]
    total: int = payload["total"]
    group_id: int = payload["group_id"]
    user_id = payload.get("created_by") or user_id

    if choice == "取消":
        session.pop(scope, user_id)
        return "❌ 已取消添加操作"
    if choice == "原名":
        if not query:
            return None  # 地区全量检索无「原名」（菜单里也不展示），静默吞掉
        session.pop(scope, user_id)
        if await store.get_arcade_by_name(group_id, query):
            return f"机厅「{query}」已在群聊中"
        await store.add_arcade(group_id, query, created_by=user_id)
        return f"✅ 已添加机厅：{query}"
    if choice == "更多":
        if total <= len(shops):
            return "没有更多结果了"
        result = await nearcade.search_shops(
            query, page + 1, limit=SEARCH_PAGE_LIMIT, region_id=payload.get("region_id")
        )
        new_shops = result.get("shops", [])
        if not new_shops:
            return "没有更多结果了"
        shops.extend(new_shops)  # 追加：既有序号保持稳定
        payload["page"] = page + 1
        # totalCount 强转：上游偶发返回字符串，下轮比较会 TypeError
        payload["total"] = int(result.get("totalCount", total) or 0)
        return _build_menu(
            query,
            payload["total"],
            shops,
            region_label=payload.get("region_label") or "",
        )
    if choice.isdigit():
        idx = int(choice) - 1
        if not 0 <= idx < len(shops):
            return None
        session.pop(scope, user_id)
        return await _add_from_shop(group_id, shops[idx], query, user_id)
    return None


# ---- 查询/删除流程（指令入口与会话追问共用） ----


async def delete_arcade_reply(group_id: int, text: str) -> str:
    entry = await resolve_arcade(group_id, text, by_alias=False)
    if entry is None:
        return "机厅不在群聊中或为机厅别名，请先添加该机厅或使用该机厅本名"
    await store.delete_arcade(group_id, entry.key)
    return f"已从群聊名单中删除机厅：{entry.name}"


async def resolve_from_list(items: list[str], text: str) -> str | None:
    """从字符串列表按序号（1 起）或原文解析。"""
    text = text.strip()
    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(items):
            return items[idx]
    return text if text in items else None


async def alias_list_reply(group_id: int, text: str) -> str:
    entry = await resolve_arcade(group_id, text)
    if entry is None:
        return f"找不到机厅或机厅别名为「{text.strip()}」的相关信息"
    aliases = await store.list_aliases(group_id, entry.key)
    if not aliases:
        return f"机厅「{entry.name}」尚未添加别名"
    body = "\n".join(f"{i}. {a}" for i, a in enumerate(aliases, 1))
    return f"机厅「{entry.name}」的别名列表如下：\n{body}"


async def map_list_reply(group_id: int, text: str) -> str:
    entry = await resolve_arcade(group_id, text)
    if entry is None:
        return f"找不到机厅或机厅别名为「{text.strip()}」的相关信息"
    maps = await store.list_maps(group_id, entry.key)
    if not maps:
        return f"机厅「{entry.name}」尚未添加地图网址"
    body = "\n".join(f"{i}. {u}" for i, u in enumerate(maps, 1))
    return f"机厅「{entry.name}」的音游地图网址如下：\n{body}"


def discover_reply(data: dict, web_url: str) -> str | None:
    """位置消息 → 附近机厅文案；查询失败（无链接）返回 None 忽略。"""
    if not web_url:
        return None
    shops = data.get("shops", [])
    if not shops:
        return f"附近没有找到机厅\n👉 详情可查看：{web_url}"
    lines = []
    for shop in shops[:3]:  # 只展示 3 个，避免刷屏
        name = shop.get("name", "未知机厅")
        dist = shop.get("distance", 0)
        dist_str = (
            f"{dist * 1000:.0f}米" if isinstance(dist, (int, float)) else "未知距离"
        )
        addr = shop.get("address", {}).get("detailed", "")
        lines.append(f"🎮 {name}（{dist_str}）\n📍 {addr}")
    return "\n\n".join(lines) + f"\n\n👉 更多详情请点开：{web_url}"


# ---- 位置消息解析（附近机厅） ----
#
# LLBot/QQNT 实测（2026-09-27）位置分享有四种形态，坐标位置各不相同：
# - 旧版位置卡片：json 段 meta.Location.Search.lat/lng
# - 新版位置卡片：json 段 app=com.tencent.tuwen.lua，坐标藏在
#   meta.news[].jumpUrl.url 的 coord=lng,lat（另有 n=地点名）
# - 高德分享：纯文本 surl.amap.com 短链，302 到
#   wb.amap.com/?p=<poiid>,<lat>,<lng>,<名称>,<地址>
# - QQ 地图链接：纯文本 map.wap.qq.com/h5-poi-detail…coord=lng,lat


def coords_from_location_json(obj: dict) -> tuple[float, float, str | None] | None:
    """旧版位置卡片：meta.Location.Search.lat/lng。"""
    location = obj.get("meta", {}).get("Location.Search", {})
    try:
        lat = float(location.get("lat", 0))
        lng = float(location.get("lng", 0))
    except (TypeError, ValueError):
        return None
    if lat and lng:
        return lat, lng, location.get("name") or None
    return None


def _card_jump_urls(obj: dict) -> list[str]:
    """收集卡片里的跳转链接（news 兼容列表/字典、jumpUrl 兼容字典/字符串）。"""
    meta = obj.get("meta")
    news = meta.get("news") if isinstance(meta, dict) else None
    if isinstance(news, dict):
        # dict 形态包成单项列表，复用逐项解析（jumpUrl 含 url 键、
        # news["url"] 与字符串 jumpUrl 都要收，静默漏收会拿不到坐标）
        news = [news]
    urls: list[str] = []
    if isinstance(news, list):
        for item in news:
            if not isinstance(item, dict):
                continue
            jump = item.get("jumpUrl")
            if isinstance(jump, dict) and jump.get("url"):
                urls.append(str(jump["url"]))
            elif isinstance(jump, str):
                urls.append(jump)
            elif isinstance(item.get("url"), str):
                urls.append(item["url"])
    return urls


def _coords_from_amap_redirect(location: str) -> tuple[float, float, str | None] | None:
    """amap 302 目标：wb.amap.com/?p=<poiid>,<lat>,<lng>,<名称>,<地址>。"""
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(location).query)
    fields = (query.get("p") or [""])[0].split(",")
    if len(fields) < 4:
        return None
    try:
        lat, lng = float(fields[1]), float(fields[2])
    except ValueError:
        return None
    if not (lat and lng):
        return None
    return lat, lng, fields[3]


def _coords_from_url(url: str) -> tuple[float, float, str | None] | None:
    return (
        _coords_from_qq_poi_url(url)
        or coords_from_google_url(url)
        or coords_from_baidu_url(url)
    )


async def coords_from_card(obj: dict) -> tuple[float, float, str | None] | None:
    """tuwen.lua 图文卡坐标：先解析各跳转链接，高德短链再跟随 302 兜底。

    实测（2026-09-27 真实卡片）news 为字典、jumpUrl 为字符串形态的
    surl.amap.com 短链——坐标只能在 302 目标里拿到，故此函数必须异步。
    """
    coords = coords_from_location_json(obj)  # 旧版 Location.Search 卡片
    if coords:
        return coords
    urls = _card_jump_urls(obj)
    for url in urls:
        coords = _coords_from_url(url)
        if coords:
            return coords
    for url in urls:
        if "amap.com" in url:
            location = await nearcade.resolve_redirect(url)
            if location:
                coords = _coords_from_amap_redirect(location)
                if coords:
                    return coords
    return None


def _coords_from_qq_poi_url(url: str) -> tuple[float, float, str | None] | None:
    """QQ 地图 poi 链接：coord=lng,lat（经度在前），n=地点名。"""
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(url).query)
    coord = next(
        (q[0] for q in (query.get(k) for k in ("coord", "centercoord", "m", "c")) if q),
        "",
    )
    parts = coord.split(",")
    if len(parts) < 2:
        return None
    try:
        lng, lat = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (lat and lng):
        return None
    name = (query.get("n") or [None])[0]
    return lat, lng, name


async def coords_from_text(text: str) -> tuple[float, float, str | None] | None:
    """纯文本形态：QQ 地图链接直接取参；高德短链跟随 302 还原。

    ``text`` 传入**未转义**的纯文本（如 OB11 ``extract_plain_text()``）；
    这里再兜底反转义一次 CQ/html 转义，防止 raw_message 口径带入 &amp;。
    """
    text = text.replace("&amp;", "&").replace("&#38;", "&")
    m = re.search(r"https?://map\.wap\.qq\.com/\S+", text)
    if m:
        coords = _coords_from_qq_poi_url(m.group(0))
        if coords:
            return coords
    m = re.search(r"https?://surl\.amap\.com/\S+", text)
    if m:
        location = await nearcade.resolve_redirect(m.group(0))
        if location:
            coords = _coords_from_amap_redirect(location)
            if coords:
                return coords
    return None


# ---- 地理坐标纠偏（百度 BD-09 / 谷歌 WGS-84 → 国测局 GCJ-02） ----
# Nearcade 收录的国内店铺坐标为 GCJ-02 口径；百度分享是 BD-09、谷歌分享是
# WGS-84，直接用会偏移数百米到一公里。公式为社区通用公开算法。


def _out_of_china(lat: float, lng: float) -> bool:
    return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)


def wgs84_to_gcj02(lat: float, lng: float) -> tuple[float, float]:
    """WGS-84 → GCJ-02（境外坐标原样返回）。"""
    if _out_of_china(lat, lng):
        return lat, lng
    d_lat = _transform_lat(lng - 105.0, lat - 35.0)
    d_lng = _transform_lng(lng - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * math.pi
    magic = 1 - 0.00669342162296594323 * math.sin(rad_lat) ** 2
    sqrt_magic = math.sqrt(magic)
    d_lat = (d_lat * 180.0) / (
        (6378245.0 * (1 - 0.00669342162296594323)) / (magic * sqrt_magic) * math.pi
    )
    d_lng = (d_lng * 180.0) / (6378245.0 / sqrt_magic * math.cos(rad_lat) * math.pi)
    return lat + d_lat, lng + d_lng


def _transform_lat(lng: float, lat: float) -> float:
    ret = (
        -100.0
        + 2.0 * lng
        + 3.0 * lat
        + 0.2 * lat * lat
        + 0.1 * lng * lat
        + 0.2 * math.sqrt(abs(lng))
    )
    ret += (
        (20.0 * math.sin(6.0 * lng * math.pi) + 20.0 * math.sin(2.0 * lng * math.pi))
        * 2.0
        / 3.0
    )
    ret += (
        (20.0 * math.sin(lat * math.pi) + 40.0 * math.sin(lat / 3.0 * math.pi))
        * 2.0
        / 3.0
    )
    ret += (
        (160.0 * math.sin(lat / 12.0 * math.pi) + 320 * math.sin(lat * math.pi / 30.0))
        * 2.0
        / 3.0
    )
    return ret


def _transform_lng(lng: float, lat: float) -> float:
    ret = (
        300.0
        + lng
        + 2.0 * lat
        + 0.1 * lng * lng
        + 0.1 * lng * lat
        + 0.1 * math.sqrt(abs(lng))
    )
    ret += (
        (20.0 * math.sin(6.0 * lng * math.pi) + 20.0 * math.sin(2.0 * lng * math.pi))
        * 2.0
        / 3.0
    )
    ret += (
        (20.0 * math.sin(lng * math.pi) + 40.0 * math.sin(lng / 3.0 * math.pi))
        * 2.0
        / 3.0
    )
    ret += (
        (
            150.0 * math.sin(lng / 12.0 * math.pi)
            + 300.0 * math.sin(lng / 30.0 * math.pi)
        )
        * 2.0
        / 3.0
    )
    return ret


def bd09_to_gcj02(lat: float, lng: float) -> tuple[float, float]:
    """百度 BD-09 → GCJ-02。"""
    x_lng = lng - 0.0065
    y_lat = lat - 0.006
    z = math.sqrt(x_lng * x_lng + y_lat * y_lat) - 0.00002 * math.sin(
        y_lat * 3000.0 * math.pi / 180.0
    )
    theta = math.atan2(y_lat, x_lng) - 0.000003 * math.cos(
        x_lng * 3000.0 * math.pi / 180.0
    )
    return z * math.sin(theta), z * math.cos(theta)


def coords_from_google_url(url: str) -> tuple[float, float, str | None] | None:
    """谷歌地图链接坐标（WGS-84，转 GCJ-02）。

    支持三种落点形态：路径 @lat,lng、data 参数 !3dlat!4dlng、查询 q=lat,lng。
    按地点名分享的链接（无坐标）返回 None，调用方走名称/地址文本搜索。
    """
    m = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", url)
    if not m:
        m = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", url)
    if m:
        lat, lng = float(m.group(1)), float(m.group(2))
    else:
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(url).query)
        q = (query.get("q") or [""])[0]
        m2 = re.match(r"(-?\d+\.\d+),(-?\d+\.\d+)$", q.strip())
        if not m2:
            return None
        lat, lng = float(m2.group(1)), float(m2.group(2))
    if not _out_of_china(lat, lng):
        lat, lng = wgs84_to_gcj02(lat, lng)
    return lat, lng, None


def coords_from_baidu_url(url: str) -> tuple[float, float, str | None] | None:
    """百度地图 marker 链接 location=lat,lng（BD-09，转 GCJ-02）。"""
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(url).query)
    location = (query.get("location") or [""])[0]
    parts = location.split(",")
    if len(parts) < 2:
        return None
    try:
        lat, lng = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (lat and lng):
        return None
    lat, lng = bd09_to_gcj02(lat, lng)
    name = (query.get("title") or [None])[0]
    return lat, lng, name


# ---- 每日清零 ----

_RESET_MARK = "last_reset_date"


async def daily_reset() -> int:
    """清零全部当日人数流水并落日清标记，返回清掉的行数。"""
    count = await store.clear_day_logs()
    await store.set_meta(_RESET_MARK, datetime.now().date().isoformat())
    return count


async def ensure_daily_reset() -> None:
    """启动补偿：停机跨天错过 0 点定时则立即补清零（幂等）。"""
    today = datetime.now().date().isoformat()
    if await store.get_meta(_RESET_MARK) == today:
        return
    count = await daily_reset()
    if count:
        logger.info(f"机厅人数日清补偿：清掉 {count} 条流水")
