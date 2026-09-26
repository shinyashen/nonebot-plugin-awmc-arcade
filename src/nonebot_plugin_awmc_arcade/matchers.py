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

私聊扩权（上游没有的能力）：SUPERUSER 或目标群管理员可在私聊执行上述
管理/查询类指令，目标群经「管理群 <群号>」上下文或指令前导群号指定，
身份经 get_group_member_info 直查校验——详见 :func:`_target_group`。
人数上报、排卡操作与位置监听保持仅群聊（排队/上报者是群成员本人，
私聊无语义）。

会话型交互（搜索选择 1-6、指令缺参追问）走 TTL 会话表，由 priority=0 的
``session_consumer`` 统一消费，不用 ``got``（awmc 生态约定）。
"""

import re
import json
import time

from nonebot import logger, on_command, on_message, on_fullmatch
from nonebot.params import CommandArg
from nonebot.typing import T_State
from nonebot.adapters import Bot
from nonebot.permission import SUPERUSER
from nonebot.adapters.onebot.v11 import Message, MessageEvent, GroupMessageEvent
from nonebot_plugin_awmc_helper.core.utils import handle_errors
from nonebot.adapters.onebot.v11.permission import GROUP_ADMIN, GROUP_OWNER
from nonebot_plugin_awmc_helper.core.forward import try_send_forward

from . import service, session, nearcade
from .store import ArcadeEntry, store
from .config import plugin_config

# ---- 私有小辅助 ----

_NOT_OPEN = "本群尚未开通排卡功能,请联系群主或管理员添加群聊"
_NO_TARGET = "请先用 管理群 <群号> 设置管理目标，或在指令开头带上群号"
_QUERY_DENY = "权限不足：仅该群管理员可查询"


async def _is_admin(bot: Bot, event: GroupMessageEvent) -> bool:
    """上游管理员口径：SUPERUSER ∨ 群管理员 ∨ 群主。"""
    return await (SUPERUSER | GROUP_ADMIN | GROUP_OWNER)(bot, event)


def _scope(event: MessageEvent) -> int:
    """会话表的键维度：群聊=群号，私聊统一 0（目标群另存 payload）。"""
    return event.group_id if isinstance(event, GroupMessageEvent) else 0


async def _is_target_admin(bot: Bot, group_id: int, user_id: str) -> str | None:
    """私聊扩权的身份校验：SUPERUSER 直通，否则直查目标群成员角色。

    ``get_group_member_info`` 结果缓存 300 秒（设置期连续操作免反复拉取）；
    返回 None 表示通过，否则为拒绝文案（bot 不在该群等 API 失败一并归入）。
    """
    if str(user_id) in bot.config.superusers:
        return None
    key = (group_id, str(user_id))
    now = time.monotonic()
    if cached := _admin_cache.get(key):
        if cached[1] > now:
            return None if cached[0] else _DENY_MANAGE
        del _admin_cache[key]
    try:
        info = await bot.call_api(
            "get_group_member_info",
            group_id=group_id,
            user_id=int(user_id),
            no_cache=True,
        )
        ok = info.get("role") in ("admin", "owner")
    except Exception:
        logger.warning(f"目标群管理员校验失败（群 {group_id}）", exc_info=True)
        return "身份校验失败：bot 可能不在该群"
    _admin_cache[key] = (ok, now + _ADMIN_CACHE_TTL)
    return None if ok else _DENY_MANAGE


_ADMIN_CACHE_TTL = 300.0  # 管理员角色缓存秒数
_admin_cache: dict[tuple[int, str], tuple[bool, float]] = {}
_DENY_MANAGE = "权限不足：你不是该群的管理员"

# 私聊指令的可选前导群号：≥4 位与机厅序号参数（1-2 位）天然区分
_GROUP_PREFIX = re.compile(r"^(\d{4,})(?:\s+(.*))?$", re.DOTALL)


def _extract_target(args_text: str) -> tuple[int | None, str]:
    """剥离私聊指令的前导群号，返回 (群号或 None, 其余参数)。"""
    m = _GROUP_PREFIX.match(args_text.strip())
    if m:
        return int(m.group(1)), (m.group(2) or "").strip()
    return None, args_text.strip()


async def _target_group(
    bot: Bot,
    event: MessageEvent,
    args_text: str,
    *,
    open_required: bool,
    group_admin_required: bool,
    deny: str = "只有管理员能够操作",
    private_admin_required: bool = True,
) -> tuple[int, str, int] | str:
    """群管理类指令的目标群统一解析与门禁。

    返回 (目标群号, 参数文本, 会话 scope)；校验失败返回错误文案。
    - 群聊：目标=当前群、原文即参数；按需校验开通态与群身份
      （先开通后权限，与上游次序一致）；
    - 私聊：前导群号 > 管理群 上下文；身份校验 SUPERUSER 直通，否则
      经 get_group_member_info 直查目标群角色。
    """
    if isinstance(event, GroupMessageEvent):
        gid = event.group_id
        if open_required and await store.get_group(gid) is None:
            return _NOT_OPEN
        if group_admin_required and not await _is_admin(bot, event):
            return deny
        return gid, args_text.strip(), gid
    uid = event.get_user_id()
    target, rest = _extract_target(args_text)
    if target is None:
        target = session.get_manage_group(uid)
        if target is None:
            return _NO_TARGET
    if private_admin_required:
        if err := await _is_target_admin(bot, target, uid):
            return err
    if open_required and await store.get_group(target) is None:
        return _NOT_OPEN
    return target, rest, 0


# ---- 群管理 ----

add_group = on_command("添加群聊", priority=10, block=True)
delete_group = on_command("删除群聊", priority=10, block=True)
manage_group_cmd = on_command("管理群", priority=10, block=True)
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
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    ctx = await _target_group(
        bot,
        event,
        str(args).strip(),
        open_required=False,
        group_admin_required=True,
        deny="只有管理员能够添加群聊",
    )
    if isinstance(ctx, str):
        await add_group.finish(ctx)
    gid = ctx[0]
    if await store.add_group(gid):
        await add_group.finish("已添加当前群聊到名单中")
    await add_group.finish("当前群聊已在名单中")


@delete_group.handle()
@handle_errors()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    ctx = await _target_group(
        bot,
        event,
        str(args).strip(),
        open_required=False,
        group_admin_required=True,
        deny="只有管理员能够删除群聊",
    )
    if isinstance(ctx, str):
        await delete_group.finish(ctx)
    gid = ctx[0]
    if await store.remove_group(gid):
        await delete_group.finish("已从名单中删除当前群聊")
    await delete_group.finish("当前群聊不在名单中，无法删除")


@manage_group_cmd.handle()
@handle_errors()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if isinstance(event, GroupMessageEvent):
        await manage_group_cmd.finish(
            "管理群 仅私聊可用：私聊 bot 后发送 管理群 <群号>"
        )
    uid = event.get_user_id()
    text = str(args).strip()
    if not text:
        current = session.get_manage_group(uid)
        await manage_group_cmd.finish(
            f"当前管理目标：{current} 群"
            if current
            else "尚未设置管理目标：发送 管理群 <群号>"
        )
    if text in ("取消", "清除", "删除"):
        if session.clear_manage_group(uid):
            await manage_group_cmd.finish("已清除管理目标")
        await manage_group_cmd.finish("当前没有管理目标")
    if not text.isdigit():
        await manage_group_cmd.finish("格式：管理群 <群号>")
    ttl = plugin_config.awmc_arcade_manage_ttl
    session.set_manage_group(uid, int(text), ttl)
    await manage_group_cmd.finish(
        f"已将管理目标设为 {text} 群（{ttl // 60} 分钟内私聊指令默认作用于该群）"
    )


@silent_on.handle()
@handle_errors()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    # matcher 已有 SUPERUSER 门禁：目标群解析无需再验身份
    ctx = await _target_group(
        bot,
        event,
        str(args).strip(),
        open_required=True,
        group_admin_required=False,
        private_admin_required=False,
    )
    if isinstance(ctx, str):
        await silent_on.finish(ctx)
    if await store.set_silent(ctx[0], True):
        await silent_on.finish("已开启静默监听模式：人数上报不再回复，仅同步云端")
    await silent_on.finish(_NOT_OPEN)


@silent_off.handle()
@handle_errors()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    ctx = await _target_group(
        bot,
        event,
        str(args).strip(),
        open_required=True,
        group_admin_required=False,
        private_admin_required=False,
    )
    if isinstance(ctx, str):
        await silent_off.finish(ctx)
    if await store.set_silent(ctx[0], False):
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
    event: MessageEvent,
    args: Message = CommandArg(),
):
    ctx = await _target_group(
        bot,
        event,
        str(args).strip(),
        open_required=True,
        group_admin_required=True,
        deny="只有管理员能够添加机厅",
    )
    if isinstance(ctx, str):
        await add_arcade.finish(ctx)
    gid, name, scope = ctx
    if not name:
        session.start(
            session.KIND_ASK,
            scope,
            event.get_user_id(),
            topic=session.TOPIC_ADD,
            payload={"group_id": gid},
        )
        await add_arcade.finish("请输入机厅名称：")
    await add_arcade.finish(
        await service.begin_add_arcade(scope, gid, event.get_user_id(), name)
    )


@delete_arcade.handle()
@handle_errors()
async def _(
    bot: Bot,
    event: MessageEvent,
    args: Message = CommandArg(),
):
    ctx = await _target_group(
        bot,
        event,
        str(args).strip(),
        open_required=True,
        group_admin_required=True,
        deny="只有管理员能够删除机厅",
    )
    if isinstance(ctx, str):
        await delete_arcade.finish(ctx)
    gid, name, scope = ctx
    if not name:
        session.start(
            session.KIND_ASK,
            scope,
            event.get_user_id(),
            topic=session.TOPIC_DELETE,
            payload={"group_id": gid},
        )
        await delete_arcade.finish("请输入要删除的机厅名称/序号：")
    await delete_arcade.finish(await service.delete_arcade_reply(gid, name))


@show_arcade.handle()
@handle_errors()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    ctx = await _target_group(
        bot,
        event,
        str(args).strip(),
        open_required=True,
        group_admin_required=False,
        private_admin_required=True,
    )
    if isinstance(ctx, str):
        await show_arcade.finish(ctx)
    entries = await store.list_arcades(ctx[0])
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
    event: MessageEvent,
    args: Message = CommandArg(),
):
    parts = str(args).strip().split(maxsplit=1)
    if len(parts) != 2:
        await add_alias.finish("格式错误：添加机厅别名 <店名/序号> <别名>")
    ctx = await _target_group(
        bot,
        event,
        str(args).strip(),
        open_required=True,
        group_admin_required=True,
        deny="只有管理员能够添加机厅别名",
    )
    if isinstance(ctx, str):
        await add_alias.finish(ctx)
    gid, _, _ = ctx
    name, alias = parts[0], parts[1].strip()
    entry = await service.resolve_arcade(gid, name, by_alias=False)
    if entry is None:
        await add_alias.finish(
            f"店名 '{name}' 不在群聊中或为机厅别名，请先添加该机厅或使用该机厅本名"
        )
    if await store.add_alias(gid, entry.id, alias):
        await add_alias.finish(f"已成功为 '{entry.name}' 添加别名 '{alias}'")
    await add_alias.finish(f"别名 '{alias}' 已存在，请使用其他别名")


@delete_alias.handle()
@handle_errors()
async def _(
    bot: Bot,
    event: MessageEvent,
    args: Message = CommandArg(),
):
    parts = str(args).strip().split(maxsplit=1)
    if len(parts) != 2:
        await delete_alias.finish("格式错误：删除机厅别名 <店名/序号> <别名/序号>")
    ctx = await _target_group(
        bot,
        event,
        str(args).strip(),
        open_required=True,
        group_admin_required=True,
        deny="只有管理员能够删除机厅别名",
    )
    if isinstance(ctx, str):
        await delete_alias.finish(ctx)
    gid, _, _ = ctx
    name, alias_ref = parts[0], parts[1].strip()
    entry = await service.resolve_arcade(gid, name, by_alias=False)
    if entry is None:
        await delete_alias.finish(
            f"店名 '{name}' 不在群聊中或为机厅别名，请先添加该机厅或使用该机厅本名"
        )
    aliases = await store.list_aliases(gid, entry.id)
    alias = await service.resolve_from_list(aliases, alias_ref)
    if alias is None:
        await delete_alias.finish(f"别名 '{alias_ref}' 不存在，请检查输入的别名")
    await store.remove_alias(gid, entry.id, alias)
    await delete_alias.finish(f"已成功删除 '{entry.name}' 的别名 '{alias}'")


@get_alias.handle()
@handle_errors()
async def _(
    bot: Bot,
    event: MessageEvent,
    args: Message = CommandArg(),
):
    ctx = await _target_group(
        bot,
        event,
        str(args).strip(),
        open_required=True,
        group_admin_required=False,
        private_admin_required=True,
    )
    if isinstance(ctx, str):
        await get_alias.finish(ctx)
    gid, text, scope = ctx
    if not text:
        session.start(
            session.KIND_ASK,
            scope,
            event.get_user_id(),
            topic=session.TOPIC_ALIAS_QUERY,
            payload={"group_id": gid},
        )
        await get_alias.finish("请输入要查询别名的机厅名称/序号：")
    await get_alias.finish(await service.alias_list_reply(gid, text))


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
    event: MessageEvent,
    args: Message = CommandArg(),
):
    parts = str(args).strip().split(maxsplit=1)
    if len(parts) != 2:
        await add_map.finish("格式错误：添加机厅地图 <机厅名称/序号> <网址>")
    ctx = await _target_group(
        bot,
        event,
        str(args).strip(),
        open_required=True,
        group_admin_required=True,
        deny="只有管理员能够添加机厅地图",
    )
    if isinstance(ctx, str):
        await add_map.finish(ctx)
    gid, _, _ = ctx
    name, url = parts[0], parts[1].strip()
    entry = await service.resolve_arcade(gid, name, by_alias=False)
    if entry is None:
        await add_map.finish(
            f"机厅 '{name}' 不在群聊中或为机厅别名，请先添加该机厅或使用该机厅本名"
        )
    if await store.add_map(gid, entry.id, url):
        await add_map.finish(f"已成功为 '{entry.name}' 添加机厅地图网址 '{url}'")
    await add_map.finish(f"网址 '{url}' 已存在于机厅地图中")


@delete_map.handle()
@handle_errors()
async def _(
    bot: Bot,
    event: MessageEvent,
    args: Message = CommandArg(),
):
    parts = str(args).strip().split(maxsplit=1)
    if len(parts) != 2:
        await delete_map.finish("格式错误：删除机厅地图 <机厅名称/序号> <网址/序号>")
    ctx = await _target_group(
        bot,
        event,
        str(args).strip(),
        open_required=True,
        group_admin_required=True,
        deny="只有管理员能够删除机厅地图",
    )
    if isinstance(ctx, str):
        await delete_map.finish(ctx)
    gid, _, _ = ctx
    name, url_ref = parts[0], parts[1].strip()
    entry = await service.resolve_arcade(gid, name, by_alias=False)
    if entry is None:
        await delete_map.finish(
            f"机厅 '{name}' 不在群聊中或为机厅别名，请先添加该机厅或使用该机厅本名"
        )
    maps = await store.list_maps(gid, entry.id)
    if not maps:
        await delete_map.finish(f"机厅 '{entry.name}' 没有添加过任何地图网址")
    url = await service.resolve_from_list(maps, url_ref)
    if url is None:
        await delete_map.finish(f"网址 '{url_ref}' 不在机厅地图中")
    await store.remove_map(gid, entry.id, url)
    await delete_map.finish(f"已成功从 '{entry.name}' 删除机厅地图网址 '{url}'")


@get_map.handle()
@handle_errors()
async def _(
    bot: Bot,
    event: MessageEvent,
    args: Message = CommandArg(),
):
    ctx = await _target_group(
        bot,
        event,
        str(args).strip(),
        open_required=True,
        group_admin_required=False,
        private_admin_required=True,
    )
    if isinstance(ctx, str):
        await get_map.finish(ctx)
    gid, text, scope = ctx
    if not text:
        session.start(
            session.KIND_ASK,
            scope,
            event.get_user_id(),
            topic=session.TOPIC_MAP_QUERY,
            payload={"group_id": gid},
        )
        await get_map.finish("请输入要查询地图的机厅名称/序号：")
    await get_map.finish(await service.map_list_reply(gid, text))


# ---- 人数上报与查询（仅群聊：上报者是群成员本人） ----


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


# ---- 排卡（队列操作仅群聊；排卡现状支持私聊查询） ----

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
    bot: Bot,
    event: MessageEvent,
    args: Message = CommandArg(),
):
    ctx = await _target_group(
        bot,
        event,
        str(args).strip(),
        open_required=False,
        group_admin_required=False,
        private_admin_required=True,
    )
    if isinstance(ctx, str):
        await show_list.finish(ctx)
    gid, name, _ = ctx
    if not name:
        await show_list.finish("请输入机厅名称")
    entry = await service.resolve_arcade(gid, name)
    if entry is None:
        await show_list.finish("没有该机厅，若需要可使用添加机厅功能")
    queue = await store.list_queue(gid, entry.id)
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
    event: MessageEvent,
    args: Message = CommandArg(),
):
    ctx = await _target_group(
        bot,
        event,
        str(args).strip(),
        open_required=True,
        group_admin_required=True,
        deny="只有管理员能够闭店",
    )
    if isinstance(ctx, str):
        await shut_down.finish(ctx)
    gid, name, _ = ctx
    if not name:
        await shut_down.finish("请输入机厅名称")
    entry = await service.resolve_arcade(gid, name)
    if entry is None:
        await shut_down.finish("没有该机厅，若需要可使用添加机厅功能")
    await store.clear_queue(gid, entry.id)
    await shut_down.finish("闭店成功，当前排队 0 人")


# ---- 会话消费（搜索选择 / 缺参追问） ----

# 消息以这些指令头开头时视为新指令：丢弃旧追问会话，不吞指令
_COMMAND_HEADS = (
    "添加群聊",
    "删除群聊",
    "管理群",
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
    text = event.raw_message.strip()
    if text.startswith(_COMMAND_HEADS):
        session.pop(_scope(event), event.get_user_id())  # 新指令进入，丢会话
        return False
    pending = session.get(_scope(event), event.get_user_id())
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
    event: MessageEvent,
    state: T_State,
):
    pending: session.PendingSession = state["_awmc_arcade_pending"]
    text = event.raw_message.strip()
    scope = _scope(event)
    user_id = event.get_user_id()
    # 落库目标群：随会话 payload 携带（私聊扩权时 ≠ 会话 scope）
    gid = pending.payload.get("group_id")
    if gid is None and isinstance(event, GroupMessageEvent):
        gid = event.group_id
    if pending.kind == session.KIND_SEARCH:
        # 选择结果与收尾由 continue_search 全权处理（含内部 pop）
        reply = await service.continue_search(scope, user_id, text)
        if reply is not None:
            await session_consumer.finish(reply)
        return  # 无效选择静默吞掉（与上游一致）
    session.pop(scope, user_id)
    if gid is None:
        await session_consumer.finish(_NO_TARGET)
    if pending.topic == session.TOPIC_ADD:
        await session_consumer.finish(
            await service.begin_add_arcade(scope, gid, user_id, text)
        )
    if pending.topic == session.TOPIC_DELETE:
        await session_consumer.finish(await service.delete_arcade_reply(gid, text))
    if pending.topic == session.TOPIC_ALIAS_QUERY:
        await session_consumer.finish(await service.alias_list_reply(gid, text))
    if pending.topic == session.TOPIC_MAP_QUERY:
        await session_consumer.finish(await service.map_list_reply(gid, text))


# ---- 位置监听（附近机厅，仅群聊） ----


async def _location_rule(state: T_State, event: MessageEvent) -> bool:
    """位置分享消息门禁：解析 CQ json 里的经纬度挂 state。"""
    if not isinstance(event, GroupMessageEvent):
        return False
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
    event: GroupMessageEvent,
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
    "私聊管理（SUPERUSER 或目标群管理员）:\n"
    "[管理群 <群号>] 设置私聊管理目标（默认30分钟内有效）\n"
    "[管理群] 查看当前目标；[管理群 取消] 清除\n"
    "设置后私聊直接发上述管理/查询指令即可作用于目标群，"
    "或在指令开头带群号临时指定（如 添加机厅 123456 某店）\n"
    "索引支持:\n"
    "机厅名、别名、地图URL均可用序号代替 (使用 机厅列表 命令查看)\n"
    "示例：删除机厅别名 1 2 (删除第1个机厅的第2个别名)\n"
    "项目地址:\n"
    "https://github.com/shinyashen/nonebot-plugin-awmc-arcade\n"
)

arcade_help = on_command("机厅help", aliases={"机厅帮助"}, priority=100, block=True)


@arcade_help.handle()
@handle_errors()
async def _(bot: Bot, event: MessageEvent):
    # 长文合并转发（点开查看，避免群内刷屏）；协议端不支持或发送失败时降级纯文本
    if await try_send_forward(
        bot,
        [HELP_TEXT],
        group_id=getattr(event, "group_id", None),
        user_id=event.get_user_id(),
    ):
        return
    await arcade_help.finish(HELP_TEXT)
