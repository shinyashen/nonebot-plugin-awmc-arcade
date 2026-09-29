"""机厅管理域：添加机厅（Nearcade 搜索选择）/ 删除机厅 / 机厅列表。"""

from nonebot import on_command
from nonebot.params import CommandArg
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot_plugin_awmc_helper.core.utils import handle_errors

from .. import service, session
from ..store import store
from ._common import _target_group, _reply_search_menu

add_arcade = on_command("添加机厅", priority=10, block=True)
delete_arcade = on_command("删除机厅", aliases={"移除机厅"}, priority=10, block=True)
show_arcade = on_command("机厅列表", aliases={"群机厅"}, priority=10, block=True)


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
