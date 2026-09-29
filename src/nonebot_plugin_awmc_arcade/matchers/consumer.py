"""会话消费域：搜索选择 / 缺参追问的统一消费 matcher（priority=0）。

命令头由各命令域模块显式汇总派生（:func:`_collect_command_heads`）——
新增命令域模块须登记进 ``_COMMAND_MODULES``，否则其指令在追问期会被
静默吞掉。
"""

from nonebot import get_driver, on_message
from nonebot.typing import T_State
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import MessageEvent, GroupMessageEvent
from nonebot_plugin_awmc_helper.core.utils import handle_errors

from . import help, count, queue, arcade, manage, resource
from .. import service, session
from ._common import _NO_TARGET, _scope, _reply_search_menu

# 命令类域模块（on_message 域的 count 上报查询 / location / consumer 自身
# 不含命令头，无需纳入；help 虽是 on_command 也一并汇总）
_COMMAND_MODULES = (manage, arcade, resource, count, queue, help)


def _collect_command_heads() -> frozenset[str]:
    """从各命令域模块全部 on_command/on_fullmatch matcher 的 rule 提取命令头。

    会话消费者（priority=0）用它识别「新指令进入」——单文件时代与各
    on_command 手工双写，新增指令漏登记会被静默吞掉，故收敛为单一来源
    派生；拆包后改为显式汇总各域模块。
    rule 内保存的是裸命令头（command_start 前缀由 TrieRule 在预处理阶段
    匹配），这里按 driver.config.command_start 笛卡尔补前缀：部署配
    {"/"} 时追问期也能识别 /添加群聊 等带前缀变体；含 ""（如测试环境
    {"", "/"}）时裸头同样在集合里，行为不变。
    """
    from nonebot.rule import CommandRule, FullmatchRule

    starts = get_driver().config.command_start or {""}
    heads: set[str] = set()
    for mod in _COMMAND_MODULES:
        for name in dir(mod):
            obj = getattr(mod, name)
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


# 消息以指令头开头时视为新指令：丢弃旧追问会话，不吞指令
_COMMAND_HEADS = _collect_command_heads()
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
