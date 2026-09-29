"""群管理域：添加/删除群聊（管理员）、管理群上下文（仅私聊）、静默监听模式。"""

from nonebot import on_command
from nonebot.params import CommandArg
from nonebot.adapters import Bot
from nonebot.permission import SUPERUSER
from nonebot.adapters.onebot.v11 import Message, MessageEvent, GroupMessageEvent
from nonebot_plugin_awmc_helper.core.utils import handle_errors

from .. import session
from ..store import store
from ..config import plugin_config
from ._common import _NOT_OPEN, _target_group

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
