"""线上排卡域：上机 / 排卡 / 退勤 / 排卡现状（支持私聊查询）/ 延后 / 闭店。"""

import asyncio

from nonebot import on_command
from nonebot.params import CommandArg
from sqlalchemy.exc import IntegrityError
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import Message, MessageEvent, GroupMessageEvent
from nonebot_plugin_awmc_helper.core.utils import handle_errors

from .. import service
from ..store import store
from ._common import _target_group

# 上游文案照抄（YuuzukiRin/nonebot_plugin_mai_arcade handlers/queue.py）。
# 「没有该机厅」在 upstream 即为两句：排卡是「请使用添加机厅功能添加」、
# 排卡现状/闭店是「若需要可使用添加机厅功能」——上游原文差异，保留双
# 常量不擅自统一
_QUEUE_ASK_NAME = "请输入机厅名称"
_QUEUE_NO_ARCADE_JOIN = "没有该机厅，请使用添加机厅功能添加"
_QUEUE_NO_ARCADE = "没有该机厅，若需要可使用添加机厅功能"

go_on = on_command("上机", priority=10, block=True)
get_in = on_command("排卡", priority=10, block=True)
get_run = on_command("退勤", priority=10, block=True)
show_list = on_command("排卡现状", priority=10, block=True)
put_off = on_command("延后", priority=10, block=True)
shut_down = on_command("闭店", priority=10, block=True)

# 每（群, 机厅）一把锁；量级受群数×机厅数约束（单机部署 <千），泄漏无害，
# 不做空闲回收——回收竞态复杂度不划算（与 service._COUNT_LOCKS 同款）
_QUEUE_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}


def _queue_lock(group_id: int, arcade_id: int) -> asyncio.Lock:
    lock = _QUEUE_LOCKS.get((group_id, arcade_id))
    if lock is None:
        lock = _QUEUE_LOCKS[(group_id, arcade_id)] = asyncio.Lock()
    return lock


@go_on.handle()
@handle_errors()
async def _(event: GroupMessageEvent):
    position = await store.get_queue_position(event.group_id, event.get_user_id())
    if position is None:
        await go_on.finish("您尚未排卡")
    entry, item = position
    # 队首判定 → 轮转之间存在 await 窗口：并发上机会按同一快照双重轮转，
    # 与 service._COUNT_LOCKS 同款按（群, 机厅）加锁串行
    async with _queue_lock(event.group_id, entry.key):
        queue = await store.list_queue(event.group_id, entry.key)
        if queue[0].id != item.id:
            await go_on.finish("暂时未到您,请耐心等待")
        if len(queue) == 1:
            # 上游原文照抄：「您可以爽霸啦」与其余两处爽霸文案本就不同
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
        await get_in.finish(_QUEUE_ASK_NAME)
    if await store.get_queue_position(event.group_id, event.get_user_id()):
        await get_in.finish("您已加入或正在其他机厅排卡")
    entry = await service.resolve_arcade(event.group_id, name)
    if entry is None:
        await get_in.finish(_QUEUE_NO_ARCADE_JOIN)
    try:
        await store.join_queue(
            event.group_id,
            entry.key,
            event.get_user_id(),
            event.sender.nickname or str(event.user_id),
        )
    except IntegrityError:
        # 查重 → 插入两步间存在 await 窗口：并发排卡撞 (群, 用户) 唯一约束，
        # 按预查重同文案回复（文案与上游一致）
        await get_in.finish("您已加入或正在其他机厅排卡")
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
        deny="",
        private_admin_required=True,
    )
    if isinstance(ctx, str):
        await show_list.finish(ctx)
    gid, name, _ = ctx
    if not name:
        await show_list.finish(_QUEUE_ASK_NAME)
    entry = await service.resolve_arcade(gid, name)
    if entry is None:
        await show_list.finish(_QUEUE_NO_ARCADE)
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
    # 邻位快照 → 交换之间存在 await 窗口：并发延后会按同一邻位各换一次
    # （队序错乱），与 service._COUNT_LOCKS 同款按（群, 机厅）加锁串行
    async with _queue_lock(event.group_id, entry.key):
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
        await shut_down.finish(_QUEUE_ASK_NAME)
    entry = await service.resolve_arcade(gid, name)
    if entry is None:
        await shut_down.finish(_QUEUE_NO_ARCADE)
    cleared = await store.clear_queue(gid, entry.key)
    await shut_down.finish(f"闭店成功，已清空排队 {cleared} 人")
