"""指令入口：全部 matcher 定义与 handler（装配层 import 本模块即完成注册）。

本文件只保留 matcher 定义、参数解析、权限检查与会话分发；业务编排下沉
service/store/nearcade。消息文案与上游 mai_arcade 保持一致（个别明显笔误
顺手修正），指令集合与语义不增不减：

- 群管理：添加群聊 / 删除群聊（管理员）、静默监听模式开关（SUPERUSER）
- 机厅管理：添加机厅（Nearcade 搜索选择）/ 删除机厅 / 机厅列表
- 别名：添加机厅别名 / 删除机厅别名 / 机厅别名
- 地图：添加机厅地图 / 删除机厅地图 / 机厅地图
- 人数：名++/--/+n/-n/=n/裸数字 上报（Nearcade 云同步）、名几/几人/j 查询、
  mai/机厅人数/jtj/机厅几人 当日更新列表
- 排卡：上机 / 排卡 / 退勤 / 排卡现状 / 延后 / 闭店（管理员）
- 位置监听：群内发送位置消息发现附近机厅

会话型交互（搜索选择 1-6、指令缺参追问）走 TTL 会话表，由 priority=0 的
``session_consumer`` 统一消费，不用 ``got``（awmc 生态约定）。
"""

import json
from functools import wraps

from nonebot import logger, on_command, on_message, on_fullmatch
from nonebot.params import CommandArg
from nonebot.typing import T_State
from nonebot.adapters import Bot, Event
from nonebot.exception import MatcherException
from nonebot.permission import SUPERUSER
from nonebot.adapters.onebot.v11 import Message, MessageEvent, GroupMessageEvent
from nonebot.adapters.onebot.v11.permission import GROUP_ADMIN, GROUP_OWNER

from . import service, session, nearcade
from .store import ArcadeEntry, store
from .config import plugin_config

# ---- 私有小辅助 ----

_NOT_OPEN = "本群尚未开通排卡功能,请联系群主或管理员添加群聊"


async def _is_admin(bot: Bot, event: GroupMessageEvent) -> bool:
    """上游管理员口径：SUPERUSER ∨ 群管理员 ∨ 群主。"""
    return await (SUPERUSER | GROUP_ADMIN | GROUP_OWNER)(bot, event)


def handle_errors(fallback: str = "出错了，请稍后再试或联系管理员。"):
    """统一异常兜底（对齐主插件 core.utils 同名约定；第三方插件不能
    import 主插件 core，本地复刻）。MatcherException 是 matcher 控制流程
    （finish 等）必须原样透传；DI 按关键字传参，bot/event 从 kwargs 取。
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except MatcherException:
                raise
            except Exception:
                logger.exception("awmc-arcade 处理指令时出现未捕获异常")
                bot: Bot | None = kwargs.get("bot")
                event: Event | None = kwargs.get("event")
                if bot is not None and event is not None:
                    try:
                        await bot.send(event, fallback)
                    except Exception:  # 兜底发送本身失败则仅留日志
                        logger.warning("awmc-arcade 错误提示发送失败")

        return wrapper

    return decorator


# ---- 群管理 ----

add_group = on_command("添加群聊", priority=10, block=True)
delete_group = on_command("删除群聊", priority=10, block=True)
silent_on = on_command(
    "静默监听模式",
    aliases={"静默模式", "监听模式"},
    permission=SUPERUSER,
    priority=10,
    block=True,
)
silent_off = on_command(
    "关闭静默监听模式",
    aliases={"关闭静默模式", "关闭监听模式"},
    permission=SUPERUSER,
    priority=10,
    block=True,
)


@add_group.handle()
@handle_errors()
async def _(bot: Bot, event: GroupMessageEvent):
    if not await _is_admin(bot, event):
        await add_group.finish("只有管理员能够添加群聊")
    if await store.add_group(event.group_id):
        await add_group.finish("已添加当前群聊到名单中")
    await add_group.finish("当前群聊已在名单中")


@delete_group.handle()
@handle_errors()
async def _(bot: Bot, event: GroupMessageEvent):
    if not await _is_admin(bot, event):
        await delete_group.finish("只有管理员能够删除群聊")
    if await store.remove_group(event.group_id):
        await delete_group.finish("已从名单中删除当前群聊")
    await delete_group.finish("当前群聊不在名单中，无法删除")


@silent_on.handle()
@handle_errors()
async def _(event: GroupMessageEvent):
    if await store.set_silent(event.group_id, True):
        await silent_on.finish("已开启静默监听模式：人数上报不再回复，仅同步云端")
    await silent_on.finish(_NOT_OPEN)


@silent_off.handle()
@handle_errors()
async def _(event: GroupMessageEvent):
    if await store.set_silent(event.group_id, False):
        await silent_off.finish("已关闭静默监听模式，恢复正常回复")
    await silent_off.finish(_NOT_OPEN)


# ---- 机厅管理 ----

add_arcade = on_command("添加机厅", priority=10, block=True)
delete_arcade = on_command("删除机厅", aliases={"移除机厅"}, priority=10, block=True)
show_arcade = on_command("机厅列表", aliases={"群机厅"}, priority=10, block=True)
updated_list = on_fullmatch(
    ("mai", "机厅人数", "jtj", "机厅几人"), priority=10, block=True
)


@add_arcade.handle()
@handle_errors()
async def _(
    bot: Bot,
    event: GroupMessageEvent,
    args: Message = CommandArg(),
):
    if await store.get_group(event.group_id) is None:
        await add_arcade.finish(_NOT_OPEN)
    if not await _is_admin(bot, event):
        await add_arcade.finish("只有管理员能够添加机厅")
    name = str(args).strip()
    if not name:
        session.start(
            session.KIND_ASK,
            event.group_id,
            event.get_user_id(),
            topic=session.TOPIC_ADD,
        )
        await add_arcade.finish("请输入机厅名称：")
    await add_arcade.finish(
        await service.begin_add_arcade(event.group_id, event.get_user_id(), name)
    )


@delete_arcade.handle()
@handle_errors()
async def _(
    bot: Bot,
    event: GroupMessageEvent,
    args: Message = CommandArg(),
):
    if await store.get_group(event.group_id) is None:
        await delete_arcade.finish(_NOT_OPEN)
    if not await _is_admin(bot, event):
        await delete_arcade.finish("只有管理员能够删除机厅")
    name = str(args).strip()
    if not name:
        session.start(
            session.KIND_ASK,
            event.group_id,
            event.get_user_id(),
            topic=session.TOPIC_DELETE,
        )
        await delete_arcade.finish("请输入要删除的机厅名称/序号：")
    await delete_arcade.finish(await service.delete_arcade_reply(event.group_id, name))


@show_arcade.handle()
@handle_errors()
async def _(event: GroupMessageEvent):
    if await store.get_group(event.group_id) is None:
        await show_arcade.finish(_NOT_OPEN)
    entries = await store.list_arcades(event.group_id)
    body = (
        "\n".join(f"{i}：{e.name}" for i, e in enumerate(entries, 1)) or "（暂无机厅）"
    )
    await show_arcade.finish(f"机厅列表如下：\n{body}")


@updated_list.handle()
@handle_errors()
async def _(event: GroupMessageEvent):
    if await store.get_group(event.group_id) is None:
        await updated_list.finish(_NOT_OPEN)
    await updated_list.finish(await service.updated_today_reply(event.group_id))


# ---- 别名 ----

add_alias = on_command("添加机厅别名", priority=10, block=True)
delete_alias = on_command(
    "删除机厅别名", aliases={"移除机厅别名"}, priority=10, block=True
)
get_alias = on_command("机厅别名", priority=10, block=True)


@add_alias.handle()
@handle_errors()
async def _(
    bot: Bot,
    event: GroupMessageEvent,
    args: Message = CommandArg(),
):
    parts = str(args).strip().split(maxsplit=1)
    if len(parts) != 2:
        await add_alias.finish("格式错误：添加机厅别名 <店名/序号> <别名>")
    if await store.get_group(event.group_id) is None:
        await add_alias.finish(_NOT_OPEN)
    if not await _is_admin(bot, event):
        await add_alias.finish("只有管理员能够添加机厅别名")
    name, alias = parts[0], parts[1].strip()
    entry = await service.resolve_arcade(event.group_id, name, by_alias=False)
    if entry is None:
        await add_alias.finish(
            f"店名 '{name}' 不在群聊中或为机厅别名，请先添加该机厅或使用该机厅本名"
        )
    if await store.add_alias(event.group_id, entry.id, alias):
        await add_alias.finish(f"已成功为 '{entry.name}' 添加别名 '{alias}'")
    await add_alias.finish(f"别名 '{alias}' 已存在，请使用其他别名")


@delete_alias.handle()
@handle_errors()
async def _(
    bot: Bot,
    event: GroupMessageEvent,
    args: Message = CommandArg(),
):
    parts = str(args).strip().split(maxsplit=1)
    if len(parts) != 2:
        await delete_alias.finish("格式错误：删除机厅别名 <店名/序号> <别名/序号>")
    if await store.get_group(event.group_id) is None:
        await delete_alias.finish(_NOT_OPEN)
    if not await _is_admin(bot, event):
        await delete_alias.finish("只有管理员能够删除机厅别名")
    name, alias_ref = parts[0], parts[1].strip()
    entry = await service.resolve_arcade(event.group_id, name, by_alias=False)
    if entry is None:
        await delete_alias.finish(
            f"店名 '{name}' 不在群聊中或为机厅别名，请先添加该机厅或使用该机厅本名"
        )
    aliases = await store.list_aliases(event.group_id, entry.id)
    alias = await service.resolve_from_list(aliases, alias_ref)
    if alias is None:
        await delete_alias.finish(f"别名 '{alias_ref}' 不存在，请检查输入的别名")
    await store.remove_alias(event.group_id, entry.id, alias)
    await delete_alias.finish(f"已成功删除 '{entry.name}' 的别名 '{alias}'")


@get_alias.handle()
@handle_errors()
async def _(
    event: GroupMessageEvent,
    args: Message = CommandArg(),
):
    text = str(args).strip()
    if not text:
        session.start(
            session.KIND_ASK,
            event.group_id,
            event.get_user_id(),
            topic=session.TOPIC_ALIAS_QUERY,
        )
        await get_alias.finish("请输入要查询别名的机厅名称/序号：")
    if await store.get_group(event.group_id) is None:
        await get_alias.finish("本群尚未开通相关功能，请联系群主或管理员添加群聊")
    await get_alias.finish(await service.alias_list_reply(event.group_id, text))


# ---- 地图 ----

add_map = on_command("添加机厅地图", priority=10, block=True)
delete_map = on_command(
    "删除机厅地图", aliases={"移除机厅地图"}, priority=10, block=True
)
get_map = on_command("机厅地图", aliases={"音游地图"}, priority=10, block=True)


@add_map.handle()
@handle_errors()
async def _(
    bot: Bot,
    event: GroupMessageEvent,
    args: Message = CommandArg(),
):
    parts = str(args).strip().split(maxsplit=1)
    if len(parts) != 2:
        await add_map.finish("格式错误：添加机厅地图 <机厅名称/序号> <网址>")
    if await store.get_group(event.group_id) is None:
        await add_map.finish(_NOT_OPEN)
    # 上游添加地图漏了权限检查（帮助文案却标注「管理」），此处按文案补上
    if not await _is_admin(bot, event):
        await add_map.finish("只有管理员能够添加机厅地图")
    name, url = parts[0], parts[1].strip()
    entry = await service.resolve_arcade(event.group_id, name, by_alias=False)
    if entry is None:
        await add_map.finish(
            f"机厅 '{name}' 不在群聊中或为机厅别名，请先添加该机厅或使用该机厅本名"
        )
    if await store.add_map(event.group_id, entry.id, url):
        await add_map.finish(f"已成功为 '{entry.name}' 添加机厅地图网址 '{url}'")
    await add_map.finish(f"网址 '{url}' 已存在于机厅地图中")


@delete_map.handle()
@handle_errors()
async def _(
    bot: Bot,
    event: GroupMessageEvent,
    args: Message = CommandArg(),
):
    parts = str(args).strip().split(maxsplit=1)
    if len(parts) != 2:
        await delete_map.finish("格式错误：删除机厅地图 <机厅名称/序号> <网址/序号>")
    if await store.get_group(event.group_id) is None:
        await delete_map.finish(_NOT_OPEN)
    if not await _is_admin(bot, event):
        await delete_map.finish("只有管理员能够删除机厅地图")
    name, url_ref = parts[0], parts[1].strip()
    entry = await service.resolve_arcade(event.group_id, name, by_alias=False)
    if entry is None:
        await delete_map.finish(
            f"机厅 '{name}' 不在群聊中或为机厅别名，请先添加该机厅或使用该机厅本名"
        )
    maps = await store.list_maps(event.group_id, entry.id)
    if not maps:
        await delete_map.finish(f"机厅 '{entry.name}' 没有添加过任何地图网址")
    url = await service.resolve_from_list(maps, url_ref)
    if url is None:
        await delete_map.finish(f"网址 '{url_ref}' 不在机厅地图中")
    await store.remove_map(event.group_id, entry.id, url)
    await delete_map.finish(f"已成功从 '{entry.name}' 删除机厅地图网址 '{url}'")


@get_map.handle()
@handle_errors()
async def _(
    event: GroupMessageEvent,
    args: Message = CommandArg(),
):
    text = str(args).strip()
    if not text:
        session.start(
            session.KIND_ASK,
            event.group_id,
            event.get_user_id(),
            topic=session.TOPIC_MAP_QUERY,
        )
        await get_map.finish("请输入要查询地图的机厅名称/序号：")
    if await store.get_group(event.group_id) is None:
        await get_map.finish(_NOT_OPEN)
    await get_map.finish(await service.map_list_reply(event.group_id, text))


# ---- 人数上报与查询 ----


async def _count_update_rule(state: T_State, event: MessageEvent) -> bool:
    """人数上报形态门禁：群已开通 + 能解析出机厅；解析结果挂 state。"""
    if not isinstance(event, GroupMessageEvent):
        return False
    parsed = service.parse_count(event.raw_message.strip())
    if parsed is None:
        return False
    name, op, num = parsed
    if await store.get_group(event.group_id) is None:
        return False
    entry = await service.resolve_arcade(event.group_id, name)
    if entry is None:
        return False
    state["_awmc_arcade_count"] = (entry, op, num)
    return True


async def _count_query_rule(state: T_State, event: MessageEvent) -> bool:
    """「XX几/几人/j」查询门禁：同上报门禁，后缀剥名。"""
    if not isinstance(event, GroupMessageEvent):
        return False
    text = event.raw_message.strip()
    for suffix in ("几人", "几", "j"):
        if text.endswith(suffix):
            name = text[: -len(suffix)].strip()
            break
    else:
        return False
    if not name or await store.get_group(event.group_id) is None:
        return False
    entry = await service.resolve_arcade(event.group_id, name)
    if entry is None:
        return False
    state["_awmc_arcade_count"] = entry
    return True


count_update = on_message(priority=100, block=False, rule=_count_update_rule)
count_query = on_message(priority=100, block=False, rule=_count_query_rule)


@count_update.handle()
@handle_errors()
async def _(
    bot: Bot,
    event: GroupMessageEvent,
    state: T_State,
):
    entry, op, num = state["_awmc_arcade_count"]
    cfg = await store.get_group(event.group_id)
    reply = await service.apply_count_update(
        entry,
        op,
        num,
        event.sender.nickname or str(event.user_id),
        silent=bool(cfg and cfg.silent),
    )
    if reply is not None:
        await count_update.finish(reply)
    # 静默模式吞掉确认回复；block=False 不拦截其他插件


@count_query.handle()
@handle_errors()
async def _(
    bot: Bot,
    event: GroupMessageEvent,
    state: T_State,
):
    entry: ArcadeEntry = state["_awmc_arcade_count"]
    await count_query.finish(await service.count_query_reply(entry))


# ---- 排卡 ----

go_on = on_command("上机", priority=10, block=True)
get_in = on_command("排卡", priority=10, block=True)
get_run = on_command("退勤", priority=10, block=True)
show_list = on_command("排卡现状", priority=10, block=True)
put_off = on_command("延后", priority=10, block=True)
shut_down = on_command("闭店", priority=10, block=True)


@go_on.handle()
@handle_errors()
async def _(event: GroupMessageEvent):
    position = await store.get_queue_position(event.group_id, event.get_user_id())
    if position is None:
        await go_on.finish("您尚未排卡")
    entry, item = position
    queue = await store.list_queue(event.group_id, entry.id)
    if queue[0].id != item.id:
        await go_on.finish("暂时未到您,请耐心等待")
    if len(queue) == 1:
        await go_on.finish(f"收到,{entry.name}机厅人数1人,您可以爽霸啦")
    await store.rotate_queue(event.group_id, entry.id)
    await go_on.finish(
        f"收到，已将{entry.name}机厅中{item.nickname}"
        f"移至最后一位,下一位上机的是{queue[1].nickname},当前一共有{len(queue)}人"
    )


@get_in.handle()
@handle_errors()
async def _(
    event: GroupMessageEvent,
    args: Message = CommandArg(),
):
    name = str(args).strip()
    if not name:
        await get_in.finish("请输入机厅名称")
    if await store.get_queue_position(event.group_id, event.get_user_id()):
        await get_in.finish("您已加入或正在其他机厅排卡")
    entry = await service.resolve_arcade(event.group_id, name)
    if entry is None:
        await get_in.finish("没有该机厅，请使用添加机厅功能添加")
    await store.join_queue(
        event.group_id, entry.id, event.get_user_id(), event.sender.nickname
    )
    queue = await store.list_queue(event.group_id, entry.id)
    await get_in.finish(f"收到，您已加入排卡。当前您位于第{len(queue)}位。")


@get_run.handle()
@handle_errors()
async def _(event: GroupMessageEvent):
    position = await store.get_queue_position(event.group_id, event.get_user_id())
    if position is None:
        await get_run.finish("您未加入排卡")
    entry, item = position
    await store.leave_queue(event.group_id, event.get_user_id())
    await get_run.finish(f"{item.nickname}从{entry.name}退勤成功")


@show_list.handle()
@handle_errors()
async def _(
    event: GroupMessageEvent,
    args: Message = CommandArg(),
):
    name = str(args).strip()
    if not name:
        await show_list.finish("请输入机厅名称")
    entry = await service.resolve_arcade(event.group_id, name)
    if entry is None:
        await show_list.finish("没有该机厅，若需要可使用添加机厅功能")
    queue = await store.list_queue(event.group_id, entry.id)
    lines = [f"第{i}位：{item.nickname}" for i, item in enumerate(queue, 1)]
    await show_list.finish(f"{entry.name}机厅排卡如下：\n" + "\n".join(lines))


@put_off.handle()
@handle_errors()
async def _(event: GroupMessageEvent):
    position = await store.get_queue_position(event.group_id, event.get_user_id())
    if position is None:
        await put_off.finish("您尚未排卡")
    entry, item = position
    queue = await store.list_queue(event.group_id, entry.id)
    index = next(i for i, q in enumerate(queue) if q.id == item.id)
    if index + 1 >= len(queue):
        await put_off.finish("您无需延后")
    nxt = queue[index + 1]
    await store.delay_in_queue(event.group_id, event.get_user_id())
    await put_off.finish(
        f"收到，已将{entry.name}机厅中{item.nickname}与{nxt.nickname}调换位置"
    )


@shut_down.handle()
@handle_errors()
async def _(
    bot: Bot,
    event: GroupMessageEvent,
    args: Message = CommandArg(),
):
    name = str(args).strip()
    if not name:
        await shut_down.finish("请输入机厅名称")
    if not await _is_admin(bot, event):
        await shut_down.finish("只有管理员能够闭店")
    entry = await service.resolve_arcade(event.group_id, name)
    if entry is None:
        await shut_down.finish("没有该机厅，若需要可使用添加机厅功能")
    await store.clear_queue(event.group_id, entry.id)
    await shut_down.finish("闭店成功，当前排队 0 人")


# ---- 会话消费（搜索选择 / 缺参追问） ----

# 消息以这些指令头开头时视为新指令：丢弃旧追问会话，不吞指令
_COMMAND_HEADS = (
    "添加群聊",
    "删除群聊",
    "静默监听模式",
    "静默模式",
    "监听模式",
    "关闭静默监听模式",
    "关闭静默模式",
    "关闭监听模式",
    "添加机厅别名",
    "添加机厅地图",
    "添加机厅",
    "删除机厅别名",
    "删除机厅地图",
    "删除机厅",
    "移除机厅别名",
    "移除机厅地图",
    "移除机厅",
    "机厅列表",
    "机厅人数",
    "机厅几人",
    "机厅别名",
    "机厅地图",
    "机厅help",
    "机厅帮助",
    "音游地图",
    "群机厅",
    "上机",
    "排卡现状",
    "排卡",
    "退勤",
    "延后",
    "闭店",
    "jtj",
    "mai",
)
_SEARCH_CHOICES = frozenset("123456")


async def _session_rule(state: T_State, event: MessageEvent) -> bool:
    """会话消费门禁：搜索会话只吃 1-6；追问会话吃任意非指令消息。"""
    if not isinstance(event, GroupMessageEvent):
        return False
    text = event.raw_message.strip()
    if text.startswith(_COMMAND_HEADS):
        session.pop(event.group_id, event.get_user_id())  # 新指令进入，丢会话
        return False
    pending = session.get(event.group_id, event.get_user_id())
    if pending is None:
        return False
    if pending.kind == session.KIND_SEARCH and text not in _SEARCH_CHOICES:
        return False
    state["_awmc_arcade_pending"] = pending
    return True


session_consumer = on_message(priority=0, block=True, rule=_session_rule)


@session_consumer.handle()
@handle_errors()
async def _(
    bot: Bot,
    event: GroupMessageEvent,
    state: T_State,
):
    pending: session.PendingSession = state["_awmc_arcade_pending"]
    text = event.raw_message.strip()
    group_id = event.group_id
    user_id = event.get_user_id()
    if pending.kind == session.KIND_SEARCH:
        # 选择结果与收尾由 continue_search 全权处理（含内部 pop）
        reply = await service.continue_search(group_id, user_id, text)
        if reply is not None:
            await session_consumer.finish(reply)
        return  # 无效选择静默吞掉（与上游一致）
    session.pop(group_id, user_id)
    if pending.topic == session.TOPIC_ADD:
        await session_consumer.finish(
            await service.begin_add_arcade(group_id, user_id, text)
        )
    if pending.topic == session.TOPIC_DELETE:
        await session_consumer.finish(await service.delete_arcade_reply(group_id, text))
    if pending.topic == session.TOPIC_ALIAS_QUERY:
        await session_consumer.finish(await service.alias_list_reply(group_id, text))
    if pending.topic == session.TOPIC_MAP_QUERY:
        await session_consumer.finish(await service.map_list_reply(group_id, text))


# ---- 位置监听（附近机厅） ----


async def _location_rule(state: T_State, event: MessageEvent) -> bool:
    """位置分享消息门禁：解析 CQ json 里的经纬度挂 state。"""
    for seg in event.message:
        if seg.type != "json":
            continue
        try:
            cq = json.loads(seg.data["data"])
            location = cq.get("meta", {}).get("Location.Search", {})
            lat = float(location.get("lat", 0))
            lon = float(location.get("lng", 0))
        except Exception:
            continue
        if lat and lon:
            state["_awmc_arcade_location"] = (
                lat,
                lon,
                location.get("name") or "未知位置",
            )
            return True
    return False


location_listener = on_message(priority=100, block=False, rule=_location_rule)


@location_listener.handle()
@handle_errors()
async def _(
    bot: Bot,
    event: MessageEvent,
    state: T_State,
):
    lat, lon, name = state["_awmc_arcade_location"]
    data, web_url = await nearcade.discover(
        lat, lon, radius=plugin_config.awmc_arcade_nearby_radius_km, name=name
    )
    reply = service.discover_reply(data, web_url)
    if reply is not None:
        await location_listener.finish(reply)


# ---- 帮助（放最后定义便于引用完整指令面） ----

HELP_TEXT = (
    "机厅人数:\n"
    "[<机厅名>++/--] 机厅的人数+1/-1\n"
    "[<机厅名>+num/-num] 机厅的人数+num/-num\n"
    "[<机厅名>=num/<机厅名>num] 机厅的人数重置为num\n"
    "[<机厅名>几/几人/j] 展示机厅当前的人数信息\n"
    "[mai/机厅人数] 展示当日已更新的所有机厅的人数列表\n"
    "群聊管理:\n"
    "[添加群聊] (管理)将群聊添加到名单中\n"
    "[删除群聊] (管理)从名单中删除指定的群聊\n"
    "机厅管理:\n"
    "[添加机厅] (管理)将机厅添加到群聊\n"
    "[删除机厅] (管理)从群聊中删除指定的机厅\n"
    "[机厅列表] 展示当前机厅列表\n"
    "[添加机厅别名 <机厅名> <别名>] (管理)为机厅添加别名\n"
    "[删除机厅别名 <机厅名> <别名/序号>] (管理)移除机厅的别名\n"
    "[机厅别名 <机厅名>] 展示机厅别名\n"
    "[添加机厅地图 <机厅名> <地图URL>] (管理)添加机厅地图信息\n"
    "[删除机厅地图 <机厅名> <地图URL/序号>] (管理)移除机厅地图信息\n"
    "[机厅地图 <机厅名>] 展示机厅音游地图\n"
    "排卡功能:\n"
    "[上机] 将当前第一位排队的移至最后\n"
    "[排卡] 加入排队队列\n"
    "[退勤] 从排队队列中退出\n"
    "[排卡现状] 展示当前排队队列的情况\n"
    "[延后] 将自己延后一位\n"
    "[闭店] (管理)清空排队队列\n"
    "索引支持:\n"
    "机厅名、别名、地图URL均可用序号代替 (使用 机厅列表 命令查看)\n"
    "示例：删除机厅别名 1 2 (删除第1个机厅的第2个别名)\n"
    "项目地址:\n"
    "https://github.com/shinyashen/nonebot-plugin-awmc-arcade\n"
)

arcade_help = on_command("机厅help", aliases={"机厅帮助"}, priority=100, block=True)


@arcade_help.handle()
async def _():
    await arcade_help.finish(HELP_TEXT)
