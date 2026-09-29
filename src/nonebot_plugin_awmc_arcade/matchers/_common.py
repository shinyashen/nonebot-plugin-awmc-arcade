"""域间共享的指令层小辅助：私聊扩权门禁、目标群解析、转发降级发送。

多个域（群管理/机厅/别名地图/排卡/会话消费）共用的口径单列于此，避免域
模块之间互相 import；业务编排在 service、存储在 store，本模块无注册副作用。
"""

import re
import time

from nonebot import logger
from nonebot.adapters import Bot
from nonebot.permission import SUPERUSER
from nonebot.adapters.onebot.v11 import MessageEvent, GroupMessageEvent
from nonebot.adapters.onebot.v11.permission import GROUP_ADMIN, GROUP_OWNER
from nonebot_plugin_awmc_helper.core.forward import try_send_forward

from .. import service, session
from ..store import store

_NOT_OPEN = "本群尚未开通排卡功能,请联系群主或管理员添加群聊"
_NO_TARGET = "请先用 管理群 <群号> 设置管理目标，或在指令开头带上群号"

_ADMIN_CACHE_TTL = 300.0  # 管理员角色缓存秒数
_ADMIN_CACHE_MAX = 512  # 缓存键上界（超过触发惰性清扫）
_admin_cache: dict[tuple[int, str], tuple[bool, float]] = {}
_DENY_MANAGE = "权限不足：你不是该群的管理员"

# 私聊指令的可选前导群号：≥4 位与机厅序号参数（1-2 位）天然区分
_GROUP_PREFIX = re.compile(r"^(\d{4,})(?:\s+(.*))?$", re.DOTALL)


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


async def _forward_or_text(bot: Bot, event: MessageEvent, matcher, nodes, text) -> None:
    """合并转发发送（协议端不支持或发送失败时降级单条文本 finish）。

    本插件的转发出口收敛于此（与 help/搜索菜单共用），测试也只需桩这一个
    try_send_forward 引用点。
    """
    if await try_send_forward(
        bot,
        nodes,
        group_id=getattr(event, "group_id", None),
        user_id=event.get_user_id(),
    ):
        return
    await matcher.finish(text)


async def _reply_search_menu(bot: Bot, event: MessageEvent, matcher, reply) -> None:
    """回复添加机厅流程：AddMenu 走合并转发（失败降级单条文本），其余直接发。"""
    if isinstance(reply, service.AddMenu):
        await _forward_or_text(bot, event, matcher, reply.nodes, reply.text)
        return
    await matcher.finish(reply)
