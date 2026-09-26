"""awmc-arcade：舞萌DX 机厅人数上报 / 附近机厅 / 线上排卡 / Nearcade 云同步。

awmc-helper 生态第三方插件，由 YuuzukiRin/nonebot_plugin_mai_arcade v0.2.0
按 awmc 生态约定迁移（替代主插件内置 awmc.arcade，二者指令大量同名，
只能启用其一——停用内置时把 ``arcade`` 写入主插件 ``awmc_disabled_plugins``）：

- 数据：进程内 JSON 全局 dict → 独立 SQLite（``store.py``，sqlmodel 全异步）；
- HTTP：同步 ``http.client``（阻塞事件循环）→ 共享 ``httpx.AsyncClient``
  （``nearcade.py``）；
- 会话：``got``/``pause`` 与裸 1-6 正则（会吞全群单个数字消息）→ TTL 会话表
  + priority=0 消费 matcher（``session.py``）；
- 静默监听模式持久化（上游 block_group 为内存 set，重启即丢），语义收敛为
  「只吞人数上报确认，查询照常回答」；
- 排卡队列按用户 id 记账（上游按昵称，重名/改名会错位），展示仍用入队昵称；
- 分层：本装配层零业务，指令入口集中在 ``matchers.py``，域逻辑在
  ``service.py``。

启动时建表并补偿错过的每日清零；每日 0 点清空当日人数流水（apscheduler）。
"""

from nonebot import require, get_driver
from nonebot.plugin import PluginMetadata

from .config import Config

__version__ = "0.3.0"

__plugin_meta__ = PluginMetadata(
    name="awmc-arcade",
    description=(
        "awmc-helper 生态第三方插件：舞萌DX 机厅人数上报、附近机厅查找、"
        "线上排卡、Nearcade 云同步（替代内置 awmc.arcade）"
    ),
    usage="发送 机厅help 查看完整指令说明；启用本插件请停用主插件内置 awmc.arcade",
    type="application",
    homepage="https://github.com/shinyashen/nonebot-plugin-awmc-arcade",
    config=Config,
    # OneBot v11 专插：位置监听解析 CQ json，权限走 OB11 GROUP_ADMIN/OWNER
    supported_adapters={"~onebot.v11"},
)

from . import nearcade
from .store import init_store
from .service import daily_reset, ensure_daily_reset

_driver = get_driver()


@_driver.on_startup
async def _startup() -> None:
    await init_store()
    await ensure_daily_reset()


@_driver.on_shutdown
async def _shutdown() -> None:
    await nearcade.aclose()


# 指令入口装配放元数据之后（nonebug 按包属性取用以下 matcher）
from .matchers import (  # noqa: F401
    go_on,
    get_in,
    add_map,
    get_map,
    get_run,
    put_off,
    add_alias,
    add_group,
    get_alias,
    show_list,
    shut_down,
    silent_on,
    add_arcade,
    delete_map,
    silent_off,
    arcade_help,
    count_query,
    show_arcade,
    count_update,
    delete_alias,
    delete_group,
    updated_list,
    delete_arcade,
    session_consumer,
    location_listener,
)

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

scheduler.add_job(daily_reset, "cron", hour=0, minute=0)
