import os
import json
from pathlib import Path

import pytest
import nonebot
from pytest_asyncio import is_async_test
from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

if Path(".env.dev").exists():
    os.environ["ENVIRONMENT"] = "dev"
else:
    os.environ["ENVIRONMENT"] = "test"


def nearcade_snapshot(name: str) -> dict:
    """读取 Nearcade 真实数据快照（tests/data/nearcade/，2026-09-29 取材）。

    来源 URL、裁剪口径与构造场景见该目录 meta.json。
    """
    path = Path(__file__).parent / "data" / "nearcade" / name
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _clear_shop_games_cache():
    """清 service 店铺机种 TTL 缓存：同店铺 id 在多用例间复用 mock 详情，
    不清会串味（缓存以 monotonic 计时，测试执行远快于 5 分钟 TTL）。"""
    from nonebot_plugin_awmc_arcade import service

    service._shop_games_cache.clear()
    yield
    service._shop_games_cache.clear()


def pytest_collection_modifyitems(items: list[pytest.Item]):
    pytest_asyncio_tests = (item for item in items if is_async_test(item))
    session_scope_marker = pytest.mark.asyncio(loop_scope="session")
    for async_test in pytest_asyncio_tests:
        async_test.add_marker(session_scope_marker, append=False)


@pytest.fixture(scope="session", autouse=True)
async def after_nonebot_init(after_nonebot_init: None, tmp_path_factory, worker_id):
    # 加载适配器
    driver = nonebot.get_driver()
    driver.register_adapter(OnebotV11Adapter)

    # 加载插件（[tool.nonebot]：本插件及其依赖链）
    nonebot.load_from_toml("pyproject.toml")

    # db 钉到 per-worker 会话级临时文件。必须在**这里**做（依赖链保证先于
    # nonebug_init 的 lifespan startup）：本插件 on_startup（_startup:
    # init_store + ensure_daily_reset）随 lifespan 在每个 xdist worker 的
    # session 开始时并发触发，_db_file 未重定向时同打 localstore 真实目录
    # ——并发 create_all 撞 already-exists 被容错吞掉后建表中断（no such
    # table: meta_row）、get/set_meta 交错（UNIQUE meta_row.key）。2026-10-05
    # CI 实测；本地不复现因真实库已有当天日清标记，ensure_daily_reset 短路。
    # 每用例的隔离仍由 function 级 _stores 重定向独立 tmp（2026-09-28 传分
    # 仓同款解法）。
    from nonebot_plugin_awmc_arcade.store import set_db_file

    base = tmp_path_factory.mktemp(f"awmc-db-{worker_id}")
    await set_db_file(base / "arcade-session.db")


@pytest.fixture(autouse=True)
async def _stores(tmp_path):
    """库文件重定向 + 清空内存会话/缓存（每用例独立）。

    会话表（TtlSessionStore）无 clear 接口，整体重建实例；管理员缓存
    （TtlCache）走自带 clear。
    """
    from nonebot_plugin_awmc_helper.core.session_store import TtlSessionStore

    from nonebot_plugin_awmc_arcade import session
    from nonebot_plugin_awmc_arcade.store import init_store, set_db_file
    from nonebot_plugin_awmc_arcade.matchers._common import _admin_cache

    await set_db_file(tmp_path / "arcade.db")
    await init_store()
    yield
    await set_db_file(None)
    session._sessions = TtlSessionStore()
    session._manage = TtlSessionStore()
    _admin_cache.clear()
