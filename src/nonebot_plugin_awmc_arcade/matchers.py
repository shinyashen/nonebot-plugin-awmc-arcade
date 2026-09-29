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
排除项：``updated_list``（mai/机厅人数 当日更新列表）天然群维度，不在
扩权面内，私聊无响应属有意。人数上报、排卡操作与位置监听保持仅群聊
（排队/上报者是群成员本人，私聊无语义）。

会话型交互（搜索选择 1-6、指令缺参追问）走 TTL 会话表，由 priority=0 的
``session_consumer`` 统一消费，不用 ``got``（awmc 生态约定）。
"""

import re
import sys
import json
import time

from nonebot import logger, get_driver, on_command, on_message, on_fullmatch
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
    if len(_admin_cache) > _ADMIN_CACHE_MAX:
        # 惰性全表清扫：键空间 = 管理操作(群, 用户) 组合，过期项仅同键再访问时
        # 才清除，低频管理场景攒得住，超界整轮清一次
        for k in [k for k, v in _admin_cache.items() if v[1] <= now]:
            del _admin_cache[k]
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
_ADMIN_CACHE_MAX = 512  # 缓存键上界（超过触发惰性清扫）
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
async def _(event: MessageEvent, args: Message = CommandArg()):
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
        # 静默只吞上传结果回复：非法数值/无店铺关联/无上传通道等错误提示
        # 保留（否则问题被无声吞掉），文案与实际行为对齐
        await silent_on.finish(
            "已开启静默监听模式：人数上报仅省略成功回复，错误提示保留"
        )
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
    await _reply_search_menu(
        bot,
        event,
        add_arcade,
        await service.begin_add_arcade(scope, gid, event.get_user_id(), name),
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
    # 参数从剥掉前导群号后的文本切分：私聊「添加机厅别名 123456 店A 别B」
    # 的首段 123456 是目标群号而非店名
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
    gid, rest, _ = ctx
    parts = rest.split(maxsplit=1)
    if len(parts) != 2:
        await add_alias.finish("格式错误：添加机厅别名 <店名/序号> <别名>")
    name, alias = parts[0], parts[1].strip()
    entry = await service.resolve_arcade(gid, name, by_alias=False)
    if entry is None:
        await add_alias.finish(
            f"店名 '{name}' 不在群聊中或为机厅别名，请先添加该机厅或使用该机厅本名"
        )
    if await store.add_alias(gid, entry.key, alias):
        await add_alias.finish(f"已成功为 '{entry.name}' 添加别名 '{alias}'")
    await add_alias.finish(f"别名 '{alias}' 已存在，请使用其他别名")


@delete_alias.handle()
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
        deny="只有管理员能够删除机厅别名",
    )
    if isinstance(ctx, str):
        await delete_alias.finish(ctx)
    gid, rest, _ = ctx
    parts = rest.split(maxsplit=1)
    if len(parts) != 2:
        await delete_alias.finish("格式错误：删除机厅别名 <店名/序号> <别名/序号>")
    name, alias_ref = parts[0], parts[1].strip()
    entry = await service.resolve_arcade(gid, name, by_alias=False)
    if entry is None:
        await delete_alias.finish(
            f"店名 '{name}' 不在群聊中或为机厅别名，请先添加该机厅或使用该机厅本名"
        )
    aliases = await store.list_aliases(gid, entry.key)
    alias = await service.resolve_from_list(aliases, alias_ref)
    if alias is None:
        await delete_alias.finish(f"别名 '{alias_ref}' 不存在，请检查输入的别名")
    await store.remove_alias(gid, entry.key, alias)
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
    gid, rest, _ = ctx
    parts = rest.split(maxsplit=1)
    if len(parts) != 2:
        await add_map.finish("格式错误：添加机厅地图 <机厅名称/序号> <网址>")
    name, url = parts[0], parts[1].strip()
    entry = await service.resolve_arcade(gid, name, by_alias=False)
    if entry is None:
        await add_map.finish(
            f"机厅 '{name}' 不在群聊中或为机厅别名，请先添加该机厅或使用该机厅本名"
        )
    if await store.add_map(gid, entry.key, url):
        await add_map.finish(f"已成功为 '{entry.name}' 添加机厅地图网址 '{url}'")
    await add_map.finish(f"网址 '{url}' 已存在于机厅地图中")


@delete_map.handle()
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
        deny="只有管理员能够删除机厅地图",
    )
    if isinstance(ctx, str):
        await delete_map.finish(ctx)
    gid, rest, _ = ctx
    parts = rest.split(maxsplit=1)
    if len(parts) != 2:
        await delete_map.finish("格式错误：删除机厅地图 <机厅名称/序号> <网址/序号>")
    name, url_ref = parts[0], parts[1].strip()
    entry = await service.resolve_arcade(gid, name, by_alias=False)
    if entry is None:
        await delete_map.finish(
            f"机厅 '{name}' 不在群聊中或为机厅别名，请先添加该机厅或使用该机厅本名"
        )
    maps = await store.list_maps(gid, entry.key)
    if not maps:
        await delete_map.finish(f"机厅 '{entry.name}' 没有添加过任何地图网址")
    url = await service.resolve_from_list(maps, url_ref)
    if url is None:
        await delete_map.finish(f"网址 '{url_ref}' 不在机厅地图中")
    await store.remove_map(gid, entry.key, url)
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
    parsed = service.parse_count(event.message.extract_plain_text().strip())
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
    text = event.message.extract_plain_text().strip()
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
    # 静默模式吞掉上传结果回复；block=False 不拦截其他插件


@count_query.handle()
@handle_errors()
async def _(
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
    queue = await store.list_queue(event.group_id, entry.key)
    if queue[0].id != item.id:
        await go_on.finish("暂时未到您,请耐心等待")
    if len(queue) == 1:
        await go_on.finish(f"收到,{entry.name}机厅人数1人,您可以爽霸啦")
    await store.rotate_queue(event.group_id, entry.key)
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
        event.group_id,
        entry.key,
        event.get_user_id(),
        event.sender.nickname or str(event.user_id),
    )
    queue = await store.list_queue(event.group_id, entry.key)
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
    queue = await store.list_queue(gid, entry.key)
    lines = [f"第{i}位：{item.nickname}" for i, item in enumerate(queue, 1)]
    await show_list.finish(f"{entry.name}机厅排卡如下：\n" + "\n".join(lines))


@put_off.handle()
@handle_errors()
async def _(event: GroupMessageEvent):
    position = await store.get_queue_position(event.group_id, event.get_user_id())
    if position is None:
        await put_off.finish("您尚未排卡")
    entry, item = position
    queue = await store.list_queue(event.group_id, entry.key)
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
    cleared = await store.clear_queue(gid, entry.key)
    await shut_down.finish(f"闭店成功，已清空排队 {cleared} 人")


# ---- 会话消费（搜索选择 / 缺参追问） ----

# 消息以指令头开头时视为新指令：丢弃旧追问会话，不吞指令。
# 命令头从本模块全部 on_command/on_fullmatch 的 rule 派生（单一来源，见文件尾
# _collect_command_heads）；本常量在模块加载完成后才求值，_session_rule 运行时读取
_COMMAND_HEADS: frozenset[str] = frozenset()
_SEARCH_ACTIONS = frozenset(service.SEARCH_ACTIONS)


def _valid_search_choice(text: str, pending: session.PendingSession) -> bool:
    """搜索会话可消费的输入：动作词，或 1~已列出数量的序号。

    超范围/无关数字不消费（不吞消息，其余 matcher 照常处理）。
    """
    if text in _SEARCH_ACTIONS:
        # 地区全量检索（无关键词）的菜单没有「原名」动作：不消费不吞消息
        if text == "原名" and not pending.payload.get("query"):
            return False
        return True
    if text.isdigit():
        return 1 <= int(text) <= len(pending.payload.get("shops", []))
    return False


async def _session_rule(state: T_State, event: MessageEvent) -> bool:
    """会话消费门禁：搜索会话吃动作词与有效序号；追问会话吃任意非指令消息。"""
    text = event.message.extract_plain_text().strip()
    if any(text.startswith(h) for h in _COMMAND_HEADS):
        session.pop(_scope(event), event.get_user_id())  # 新指令进入，丢会话
        return False
    pending = session.get(_scope(event), event.get_user_id())
    if pending is None:
        return False
    if pending.kind == session.KIND_SEARCH and not _valid_search_choice(text, pending):
        return False
    state["_awmc_arcade_pending"] = pending
    return True


async def _reply_search_menu(bot: Bot, event: MessageEvent, matcher, reply) -> None:
    """回复添加机厅流程：AddMenu 走合并转发（失败降级单条文本），其余直接发。"""
    if isinstance(reply, service.AddMenu):
        if await try_send_forward(
            bot,
            reply.nodes,
            group_id=getattr(event, "group_id", None),
            user_id=event.get_user_id(),
        ):
            return
        await matcher.finish(reply.text)
        return
    await matcher.finish(reply)


session_consumer = on_message(priority=0, block=True, rule=_session_rule)


@session_consumer.handle()
@handle_errors()
async def _(
    bot: Bot,
    event: MessageEvent,
    state: T_State,
):
    pending: session.PendingSession = state["_awmc_arcade_pending"]
    text = event.message.extract_plain_text().strip()
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
            await _reply_search_menu(bot, event, session_consumer, reply)
        return  # 无效选择静默吞掉（与上游一致）
    session.pop(scope, user_id)
    if gid is None:
        await session_consumer.finish(_NO_TARGET)
    if pending.topic == session.TOPIC_ADD:
        await _reply_search_menu(
            bot,
            event,
            session_consumer,
            await service.begin_add_arcade(scope, gid, user_id, text),
        )
    if pending.topic == session.TOPIC_DELETE:
        await session_consumer.finish(await service.delete_arcade_reply(gid, text))
    if pending.topic == session.TOPIC_ALIAS_QUERY:
        await session_consumer.finish(await service.alias_list_reply(gid, text))
    if pending.topic == session.TOPIC_MAP_QUERY:
        await session_consumer.finish(await service.map_list_reply(gid, text))


# ---- 位置监听（附近机厅，仅群聊） ----


async def _location_rule(state: T_State, event: MessageEvent) -> bool:
    """位置分享门禁：四种实测形态都能提取坐标（详见 service 位置解析注记）。

    依次尝试：OB11 标准 location 段 → json 段（旧版 Location.Search 卡片 /
    新版 tuwen.lua 图文卡）→ 纯文本里的高德短链与 QQ 地图链接（短链需
    跟随 302，仅当文本含相关域名才发起请求）。
    """
    if not isinstance(event, GroupMessageEvent):
        return False
    for seg in event.message:
        if seg.type == "location":
            data = seg.data or {}
            try:
                lat = float(data.get("lat", 0))
                lng = float(data.get("lon", 0) or data.get("lng", 0))
            except (TypeError, ValueError):
                continue
            if lat and lng:
                state["_awmc_arcade_location"] = (
                    lat,
                    lng,
                    data.get("title") or "未知位置",
                )
                return True
        if seg.type != "json":
            continue
        try:
            obj = json.loads(seg.data["data"])
        except Exception:
            continue
        coords = await service.coords_from_card(obj)
        if coords:
            lat, lng, name = coords
            state["_awmc_arcade_location"] = (lat, lng, name or "未知位置")
            return True
    plain = event.message.extract_plain_text()
    if "amap.com" in plain or "map.wap.qq.com" in plain:
        coords = await service.coords_from_text(plain)
        if coords:
            lat, lng, name = coords
            state["_awmc_arcade_location"] = (lat, lng, name or "未知位置")
            return True
    if "goo.gl" in plain or "google.com" in plain:
        m = re.search(
            r"https?://(?:maps\.app\.goo\.gl|goo\.gl|www\.google\.com/maps"
            r"|maps\.google\.com)/\S+",
            plain,
        )
        if m:
            target = m.group(0)
            if "goo.gl" in target:
                target = await nearcade.resolve_redirect(target) or target
            coords = service.coords_from_google_url(target)
            if coords:
                lat, lng, name = coords
                state["_awmc_arcade_location"] = (lat, lng, name or "未知位置")
                return True
    if "map.baidu.com" in plain:
        m = re.search(r"https?://(?:j\.map\.baidu\.com|map\.baidu\.com)/\S+", plain)
        if m:
            target = m.group(0)
            if "j.map.baidu.com" in target:
                target = await nearcade.resolve_redirect(target) or target
            coords = service.coords_from_baidu_url(target)
            if coords:
                lat, lng, name = coords
                state["_awmc_arcade_location"] = (lat, lng, name or "未知位置")
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
    "[添加机厅 <店名> [@地区…]] (管理)将机厅添加到群聊（@南京 按地区过滤重名）\n"
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


def _collect_command_heads() -> frozenset[str]:
    """从本模块全部 on_command/on_fullmatch matcher 的 rule 提取命令头。

    会话消费者（priority=0）用它识别「新指令进入」——此前与各 on_command
    手工双写，新增指令漏登记会被静默吞掉，故收敛为单一来源派生。
    rule 内保存的是裸命令头（command_start 前缀由 TrieRule 在预处理阶段
    匹配），这里按 driver.config.command_start 笛卡尔补前缀：部署配
    {"/"} 时追问期也能识别 /添加群聊 等带前缀变体；含 ""（如测试环境
    {"", "/"}）时裸头同样在集合里，行为不变。
    """
    from nonebot.rule import CommandRule, FullmatchRule

    starts = get_driver().config.command_start or {""}
    heads: set[str] = set()
    for name in dir(sys.modules[__name__]):
        obj = getattr(sys.modules[__name__], name)
        if not isinstance(obj, type) or not hasattr(obj, "rule"):
            continue
        raw: set[str] = set()
        for dep in obj.rule.checkers:
            call = getattr(dep, "call", None)
            if isinstance(call, CommandRule):
                raw.update(cmd[0] for cmd in call.cmds)
            elif isinstance(call, FullmatchRule):
                raw.update(call.msg)
        heads.update(f"{start}{head}" for head in raw for start in starts)
    return frozenset(heads)


_COMMAND_HEADS = _collect_command_heads()
