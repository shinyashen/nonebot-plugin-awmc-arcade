"""TTL 会话表：Nearcade 搜索选择 / 无参指令的参数追问。

按 awmc 生态约定，会话型交互不用 nonebot 的 ``got``——这里维护一张
「(群, 用户) → 待续动作」的内存表，配 priority=0 的 on_message 消费
matcher（定义在 matchers.py，本模块无注册副作用）。

同一 (群, 用户) 同时只保留一个会话，新会话覆盖旧会话；过期惰性清除。
"""

import time
from dataclasses import field, dataclass

from .config import plugin_config

# 会话类别
KIND_SEARCH = "search"  # 添加机厅的 Nearcade 搜索结果选择（回复 1-6）
KIND_ASK = "ask"  # 指令缺参追问（下一条消息即参数）

# ask 类别下的续接动作
TOPIC_ADD = "add_arcade"
TOPIC_DELETE = "del_arcade"
TOPIC_ALIAS_QUERY = "alias_query"
TOPIC_MAP_QUERY = "map_query"


@dataclass
class PendingSession:
    kind: str
    topic: str = ""  # KIND_ASK 时的续接动作
    group_id: int = 0
    user_id: str = ""
    payload: dict = field(default_factory=dict)
    expire_at: float = 0.0


_sessions: dict[tuple[int, str], PendingSession] = {}


def start(
    kind: str,
    group_id: int,
    user_id: str,
    *,
    topic: str = "",
    payload: dict | None = None,
) -> None:
    """开启/覆盖会话（附带惰性清扫过期项）。"""
    now = time.time()
    for key in [k for k, v in _sessions.items() if v.expire_at <= now]:
        del _sessions[key]
    _sessions[(group_id, user_id)] = PendingSession(
        kind=kind,
        topic=topic,
        group_id=group_id,
        user_id=user_id,
        payload=payload or {},
        expire_at=now + plugin_config.awmc_arcade_session_ttl,
    )


def get(group_id: int, user_id: str) -> PendingSession | None:
    """取会话（过期即清除并返回 None）。"""
    session = _sessions.get((group_id, user_id))
    if session is None:
        return None
    if session.expire_at <= time.time():
        del _sessions[(group_id, user_id)]
        return None
    return session


def pop(group_id: int, user_id: str) -> PendingSession | None:
    """取会话并结束（无论是否过期）。"""
    return _sessions.pop((group_id, user_id), None)


# ---- 私聊扩权：「管理群 <群号>」工作上下文 ----
# 按用户维度记忆私聊管理目标，后续指令免带群号；TTL 独立于交互会话
# （config.awmc_arcade_manage_ttl，默认 30 分钟）。

_manage: dict[str, tuple[int, float]] = {}


def set_manage_group(user_id: str, group_id: int, ttl: int) -> None:
    """设置/覆盖管理目标群。"""
    _manage[user_id] = (group_id, time.time() + ttl)


def get_manage_group(user_id: str) -> int | None:
    """取管理目标群（过期即清除并返回 None）。"""
    item = _manage.get(user_id)
    if item is None:
        return None
    if item[1] <= time.time():
        del _manage[user_id]
        return None
    return item[0]


def clear_manage_group(user_id: str) -> bool:
    """清除管理目标群，返回是否存在。"""
    return _manage.pop(user_id, None) is not None
