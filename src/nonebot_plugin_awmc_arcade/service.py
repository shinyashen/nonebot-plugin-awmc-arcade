"""域逻辑：机厅解析、人数换算与 Nearcade 云同步编排、搜索会话流转、文案组装。

本模块无注册副作用、不发消息，只做「输入 → 文案/数据」的纯编排，
供 matchers.py 的各 handler 复用（指令入口与会话消费 matcher 共用同一份流程）。
"""

import re
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
        delta = num if num else 1
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
    """店铺 id：优先条目缓存，其次按添加顺序扫描地图网址提取。"""
    if entry.nearcade_shop_id:
        return entry.nearcade_shop_id
    for url in await store.list_maps(entry.group_id, entry.key):
        if sid := shop_id_from_url(url):
            return sid
    return None


def shop_id_from_url(url: str) -> str | None:
    """从 Nearcade 店铺链接提取店铺 id（上游以地图网址为店铺关联载体）。"""
    m = re.search(r"/(\d+)/?$", url)
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


async def _maimai_coutnum(entry: ArcadeEntry, shop_id: str) -> int:
    """从云端店铺详情刷新 maimai DX 机台数（失败沿用条目缓存）。"""
    for game in await _shop_games(shop_id):
        if game.get("name") == "maimai DX":
            return max(int(game.get("quantity", 1) or 1), 1)
    return entry.coutnum


async def apply_count_update(
    entry: ArcadeEntry, op: str, num: int | None, user_name: str, *, silent: bool
) -> str | None:
    """人数上报全流程：本地记账 → 云端增量合并 → 上传 → 回复文案。

    静默模式只吞掉上报确认（含上传失败提示），返回 None。
    与上游的一处有意偏差：显式设置（名=5）为绝对值，不再向云端看齐合并；
    相对增减（名+1/--2）保持上游语义——云端已有他人上报时，本次增量
    叠加到云端值上。
    """
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

    # 机台数与上传 gameId 同源自一次店铺详情请求；上传必须挂在
    # maimai DX 机种上（店铺详情 games[0] 可能是太鼓等其他机种，
    # 数错格会把人数写进别的游戏的出勤计数）
    games = await _shop_games(shop_id)
    game_id = next((g.get("gameId") for g in games), None)
    maimai_game = next((g for g in games if g.get("name") == "maimai DX"), None)
    coutnum = entry.coutnum
    if maimai_game is not None:
        coutnum = max(int(maimai_game.get("quantity", 1) or 1), 1)
        game_id = maimai_game.get("gameId")
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
    """「mai/机厅人数」：当日有更新记录的机厅列表。"""
    lines = []
    for entry in await store.list_arcades(group_id):
        last = await store.last_count_update(group_id, entry.key)
        if last is None:
            continue
        count = await store.current_count(group_id, entry.key)
        by = last.updated_by or "未知"
        lines.append(
            f"[{entry.name}] {count}人 \n（{by} · {last.updated_at.strftime('%H:%M')}）"
        )
    if not lines:
        return "📋 今日机厅人数更新情况\n\n暂无更新记录\n您可以爽霸机了"
    return "📋 今日机厅人数更新情况\n\n" + "\n".join(lines)


# ---- 添加机厅（Nearcade 搜索交互） ----


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


def _search_menu(page: int, shops: list[dict], total: int) -> str:
    head = f"🔍 找到 {len(shops)} 个相关机厅：" if page == 1 else f"🔍 第{page}页结果："
    text = (
        head
        + "\n\n"
        + "\n\n".join(format_shop_info(shop, i) for i, shop in enumerate(shops, 1))
    )
    text += "\n\n请选择操作：\n1️⃣2️⃣3️⃣ 选择对应机厅"
    if total > page * 3:
        text += "\n4️⃣ 查看更多结果"
    text += "\n5️⃣ 直接添加原名称\n6️⃣ 取消操作"
    return text


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
    """操作说明：数字只用于选店，动作一律非数字词。"""
    lines = ["回复序号 选择对应机厅"]
    if has_more:
        lines.append("「更多」 查看更多结果")
    lines.append(f"「原名」 直接添加「{query}」")
    lines.append("「取消」 放弃操作")
    return "\n".join(lines)


def _build_menu(query: str, total: int, shops: list[dict]) -> AddMenu:
    """由累计候选构建菜单（合并转发节点 + 同内容降级单条文本）。"""
    total = max(total, len(shops))
    has_more = total > len(shops)
    actions = _menu_actions(query, has_more)
    shops_text = "\n\n".join(
        format_shop_info(shop, i) for i, shop in enumerate(shops, 1)
    )
    text = f"🔍 找到 {total} 个相关机厅：\n\n{shops_text}\n\n{actions}"
    nodes = [
        f"🔍 找到 {total} 个相关机厅",
        actions,
        *(format_shop_info(shop, i) for i, shop in enumerate(shops, 1)),
    ]
    return AddMenu(text=text, nodes=nodes)


async def begin_add_arcade(
    scope: int, group_id: int, user_id: str, name: str
) -> "str | AddMenu":
    """添加机厅入口：命中搜索则挂选择会话并返回菜单，否则直接添加。

    ``scope`` 是会话表的键维度（群聊=群号、私聊=0），``group_id`` 是
    落库目标群——私聊扩权时两者不同。
    """
    if await store.get_arcade_by_name(group_id, name):
        return "机厅已在群聊中"
    result = await nearcade.search_shops(name, limit=SEARCH_PAGE_LIMIT)
    shops = result.get("shops", [])
    if not shops:
        await store.add_arcade(group_id, name, created_by=user_id)
        return f"未找到相关机厅，已直接添加「{name}」到群聊名单中"
    total = result.get("totalCount", 0)
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
            "created_by": user_id,
        },
    )
    return _build_menu(name, total, shops)


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
        session.pop(scope, user_id)
        if await store.get_arcade_by_name(group_id, query):
            return f"机厅「{query}」已在群聊中"
        await store.add_arcade(group_id, query, created_by=user_id)
        return f"✅ 已添加机厅：{query}"
    if choice == "更多":
        if total <= len(shops):
            return "没有更多结果了"
        result = await nearcade.search_shops(query, page + 1, limit=SEARCH_PAGE_LIMIT)
        new_shops = result.get("shops", [])
        if not new_shops:
            return "没有更多结果了"
        shops.extend(new_shops)  # 追加：既有序号保持稳定
        payload["page"] = page + 1
        payload["total"] = result.get("totalCount", total)
        return _build_menu(query, payload["total"], shops)
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
