"""TTL 会话表：Nearcade 搜索选择 / 无参指令的参数追问。

会话表本体是 core 泛型 :class:`TtlSessionStore`（与主插件绑定回填同底座），
本模块只定义业务会话结构与模块级门面；消费 matcher 定义在 matchers.py，
本模块无注册副作用。
"""

import time
from dataclasses import field, dataclass

from nonebot_plugin_awmc_helper.core.session_store import TtlSession, TtlSessionStore

from .config import plugin_config

# 会话类别
KIND_SEARCH = "search"  # 添加机厅的搜索结果选择（序号 / 更多 / 原名 / 取消）
KIND_ASK = "ask"  # 指令缺参追问（下一条消息即参数）

# ask 类别下的续接动作
TOPIC_ADD = "add_arcade"
TOPIC_DELETE = "del_arcade"
TOPIC_ALIAS_QUERY = "alias_query"
TOPIC_MAP_QUERY = "map_query"


@dataclass
class PendingSession(TtlSession):
    kind: str
    topic: str = ""  # KIND_ASK 时的续接动作
    payload: dict = field(default_factory=dict)


_sessions: TtlSessionStore[tuple[int, str], PendingSession] = TtlSessionStore()


def start(
    kind: str,
    group_id: int,
    user_id: str,
    *,
    topic: str = "",
    payload: dict | None = None,
) -> None:
    """开启/覆盖会话（附带惰性清扫过期项）。"""
    _sessions.start(
        (group_id, user_id),
        PendingSession(
            kind=kind,
            topic=topic,
            payload=payload or {},
            expire_at=time.monotonic() + plugin_config.awmc_arcade_session_ttl,
        ),
    )


def get(group_id: int, user_id: str) -> PendingSession | None:
    """取会话（过期即清除并返回 None）。"""
    return _sessions.get((group_id, user_id))


def pop(group_id: int, user_id: str) -> PendingSession | None:
    """取会话并结束（无论是否过期）。"""
    return _sessions.pop((group_id, user_id))


def any_active() -> bool:
    """是否存在任何活跃会话（供 priority=0 消费规则先行短路，
    免每条群消息都做命令头扫描）。"""
    return _sessions.any_active()


# ---- 私聊扩权：「管理群 <群号>」工作上下文 ----
# 按用户维度记忆私聊管理目标，后续指令免带群号；TTL 独立于交互会话
# （config.awmc_arcade_manage_ttl，默认 30 分钟）。复用 core 泛型
# TtlSessionStore（monotonic 口径），此前手写 time.time() 墙钟 dict。


@dataclass
class ManageTarget(TtlSession):
    """私聊管理目标群（expire_at 由 set_manage_group 按 ttl 填写）。"""

    group_id: int


_manage: TtlSessionStore[str, ManageTarget] = TtlSessionStore()


def set_manage_group(user_id: str, group_id: int, ttl: int) -> None:
    """设置/覆盖管理目标群。"""
    _manage.start(
        user_id, ManageTarget(group_id=group_id, expire_at=time.monotonic() + ttl)
    )


def get_manage_group(user_id: str) -> int | None:
    """取管理目标群（过期即清除并返回 None）。"""
    return target.group_id if (target := _manage.get(user_id)) else None


def clear_manage_group(user_id: str) -> bool:
    """清除管理目标群，返回是否存在。"""
    return _manage.pop(user_id) is not None
