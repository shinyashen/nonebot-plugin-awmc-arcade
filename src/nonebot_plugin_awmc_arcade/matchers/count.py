"""人数域：名++/--/+n/-n/=n/裸数字 上报（Nearcade 云同步）、几/几人/j 查询、
mai/机厅人数 当日更新列表。仅群聊——上报者是群成员本人，私聊无语义。"""

from nonebot import on_message, on_fullmatch
from nonebot.typing import T_State
from nonebot.adapters.onebot.v11 import MessageEvent, GroupMessageEvent
from nonebot_plugin_awmc_helper.core.utils import handle_errors

from .. import service
from ..store import ArcadeEntry, store
from ._common import _NOT_OPEN


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
# 天然群维度：不在私聊扩权面内（见包 __init__ 口径说明），私聊无响应属有意
updated_list = on_fullmatch(
    ("mai", "机厅人数", "jtj", "机厅几人"), priority=10, block=True
)


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


@updated_list.handle()
@handle_errors()
async def _(event: GroupMessageEvent):
    if await store.get_group(event.group_id) is None:
        await updated_list.finish(_NOT_OPEN)
    await updated_list.finish(await service.updated_today_reply(event.group_id))
