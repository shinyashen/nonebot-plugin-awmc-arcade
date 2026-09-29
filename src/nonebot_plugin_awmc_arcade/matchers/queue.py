"""线上排卡域：上机 / 排卡 / 退勤 / 排卡现状（支持私聊查询）/ 延后 / 闭店。"""

from nonebot import on_command
from nonebot.params import CommandArg
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import Message, MessageEvent, GroupMessageEvent
from nonebot_plugin_awmc_helper.core.utils import handle_errors

from .. import service
from ..store import store
from ._common import _target_group

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
