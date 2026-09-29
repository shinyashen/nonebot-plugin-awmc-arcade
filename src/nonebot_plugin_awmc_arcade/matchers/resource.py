"""机厅别名 / 机厅地图域：两组同构 handler 收敛为参数化增删查流程。

「列表资源」指挂在机厅下、按添加顺序可用序号操作的字符串列表——别名与
地图网址除资源名词、文案与存取函数外逐行同构，经 :class:`_ListResource`
参数集装配（T-6）；用户可见文案与拆分前逐字一致。
"""

from dataclasses import dataclass
from collections.abc import Callable, Awaitable

from nonebot import on_command
from nonebot.params import CommandArg
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot_plugin_awmc_helper.core.utils import handle_errors

from .. import service, session
from ..store import store
from ._common import _target_group

# 存取函数口径：群号 + 机厅 id（+ 资源值）
_Lister = Callable[[int, int], Awaitable[list[str]]]
_Adder = Callable[[int, int, str], Awaitable[bool]]
_Remover = Callable[[int, int, str], Awaitable[bool]]
_Replier = Callable[[int, str], Awaitable[str]]


@dataclass(frozen=True)
class _ListResource:
    """别名/地图增删查三指令的差异化参数（文案含 {占位} 由 handler 填充）。"""

    add_cmd: str  # 添加指令头
    delete_cmd: str  # 删除指令头
    delete_aliases: set[str]  # 删除指令别名
    query_cmd: str  # 查询指令头
    query_aliases: set[str]  # 查询指令别名
    topic: str  # 查询缺参追问的续接动作
    ask_query: str  # 查询缺参提示
    add_format: str  # 添加格式错误
    delete_format: str  # 删除格式错误
    deny_add: str
    deny_delete: str
    not_found: str  # 机厅定位失败（含 {name}）
    add_success: str  # 含 {entry} {value}
    add_dup: str  # 含 {value}
    delete_missing: str  # 资源值不存在（含 {ref}）
    delete_success: str  # 含 {entry} {value}
    delete_empty: str | None  # 列表为空提示（含 {entry}）；别名无此分支
    list_items: _Lister
    add_item: _Adder
    remove_item: _Remover
    list_reply: _Replier


_ALIAS = _ListResource(
    add_cmd="添加机厅别名",
    delete_cmd="删除机厅别名",
    delete_aliases={"移除机厅别名"},
    query_cmd="机厅别名",
    query_aliases=set(),
    topic=session.TOPIC_ALIAS_QUERY,
    ask_query="请输入要查询别名的机厅名称/序号：",
    add_format="格式错误：添加机厅别名 <店名/序号> <别名>",
    delete_format="格式错误：删除机厅别名 <店名/序号> <别名/序号>",
    deny_add="只有管理员能够添加机厅别名",
    deny_delete="只有管理员能够删除机厅别名",
    not_found="店名 '{name}' 不在群聊中或为机厅别名，请先添加该机厅或使用该机厅本名",
    add_success="已成功为 '{entry}' 添加别名 '{value}'",
    add_dup="别名 '{value}' 已存在，请使用其他别名",
    delete_missing="别名 '{ref}' 不存在，请检查输入的别名",
    delete_success="已成功删除 '{entry}' 的别名 '{value}'",
    delete_empty=None,
    list_items=store.list_aliases,
    add_item=store.add_alias,
    remove_item=store.remove_alias,
    list_reply=service.alias_list_reply,
)

_MAP = _ListResource(
    add_cmd="添加机厅地图",
    delete_cmd="删除机厅地图",
    delete_aliases={"移除机厅地图"},
    query_cmd="机厅地图",
    query_aliases={"音游地图"},
    topic=session.TOPIC_MAP_QUERY,
    ask_query="请输入要查询地图的机厅名称/序号：",
    add_format="格式错误：添加机厅地图 <机厅名称/序号> <网址>",
    delete_format="格式错误：删除机厅地图 <机厅名称/序号> <网址/序号>",
    deny_add="只有管理员能够添加机厅地图",
    deny_delete="只有管理员能够删除机厅地图",
    not_found="机厅 '{name}' 不在群聊中或为机厅别名，请先添加该机厅或使用该机厅本名",
    add_success="已成功为 '{entry}' 添加机厅地图网址 '{value}'",
    add_dup="网址 '{value}' 已存在于机厅地图中",
    delete_missing="网址 '{ref}' 不在机厅地图中",
    delete_success="已成功从 '{entry}' 删除机厅地图网址 '{value}'",
    delete_empty="机厅 '{entry}' 没有添加过任何地图网址",
    list_items=store.list_maps,
    add_item=store.add_map,
    remove_item=store.remove_map,
    list_reply=service.map_list_reply,
)


def _build_resource(spec: _ListResource):
    """按资源参数装配「添加 / 删除 / 查询」三个 matcher 与 handler。"""
    add = on_command(spec.add_cmd, priority=10, block=True)
    delete = on_command(
        spec.delete_cmd, aliases=spec.delete_aliases, priority=10, block=True
    )
    query = on_command(
        spec.query_cmd, aliases=spec.query_aliases, priority=10, block=True
    )

    @add.handle()
    @handle_errors()
    async def _add_handler(
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
            deny=spec.deny_add,
        )
        if isinstance(ctx, str):
            await add.finish(ctx)
        gid, rest, _ = ctx
        parts = rest.split(maxsplit=1)
        if len(parts) != 2:
            await add.finish(spec.add_format)
        name, value = parts[0], parts[1].strip()
        entry = await service.resolve_arcade(gid, name, by_alias=False)
        if entry is None:
            await add.finish(spec.not_found.format(name=name))
        if await spec.add_item(gid, entry.key, value):
            await add.finish(spec.add_success.format(entry=entry.name, value=value))
        await add.finish(spec.add_dup.format(value=value))

    @delete.handle()
    @handle_errors()
    async def _delete_handler(
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
            deny=spec.deny_delete,
        )
        if isinstance(ctx, str):
            await delete.finish(ctx)
        gid, rest, _ = ctx
        parts = rest.split(maxsplit=1)
        if len(parts) != 2:
            await delete.finish(spec.delete_format)
        name, ref = parts[0], parts[1].strip()
        entry = await service.resolve_arcade(gid, name, by_alias=False)
        if entry is None:
            await delete.finish(spec.not_found.format(name=name))
        items = await spec.list_items(gid, entry.key)
        if spec.delete_empty and not items:
            await delete.finish(spec.delete_empty.format(entry=entry.name))
        value = await service.resolve_from_list(items, ref)
        if value is None:
            await delete.finish(spec.delete_missing.format(ref=ref))
        await spec.remove_item(gid, entry.key, value)
        await delete.finish(spec.delete_success.format(entry=entry.name, value=value))

    @query.handle()
    @handle_errors()
    async def _query_handler(
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
            await query.finish(ctx)
        gid, text, scope = ctx
        if not text:
            session.start(
                session.KIND_ASK,
                scope,
                event.get_user_id(),
                topic=spec.topic,
                payload={"group_id": gid},
            )
            await query.finish(spec.ask_query)
        await query.finish(await spec.list_reply(gid, text))

    return add, delete, query


add_alias, delete_alias, get_alias = _build_resource(_ALIAS)
add_map, delete_map, get_map = _build_resource(_MAP)
