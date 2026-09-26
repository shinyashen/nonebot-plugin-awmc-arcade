import os
from pathlib import Path

import pytest
import nonebot
from pytest_asyncio import is_async_test
from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

if Path(".env.dev").exists():
    os.environ["ENVIRONMENT"] = "dev"
else:
    os.environ["ENVIRONMENT"] = "test"


def pytest_collection_modifyitems(items: list[pytest.Item]):
    pytest_asyncio_tests = (item for item in items if is_async_test(item))
    session_scope_marker = pytest.mark.asyncio(loop_scope="session")
    for async_test in pytest_asyncio_tests:
        async_test.add_marker(session_scope_marker, append=False)


@pytest.fixture(scope="session", autouse=True)
async def after_nonebot_init(after_nonebot_init: None):
    # 加载适配器
    driver = nonebot.get_driver()
    driver.register_adapter(OnebotV11Adapter)

    # 加载插件（[tool.nonebot]：本插件及其依赖链）
    nonebot.load_from_toml("pyproject.toml")


@pytest.fixture(autouse=True)
async def _stores(tmp_path):
    """库文件重定向 + 清空内存会话/缓存（每用例独立）。"""
    from nonebot_plugin_awmc_arcade import session
    from nonebot_plugin_awmc_arcade.store import init_store, set_db_file
    from nonebot_plugin_awmc_arcade.matchers import _admin_cache

    await set_db_file(tmp_path / "arcade.db")
    await init_store()
    yield
    await set_db_file(None)
    session._sessions.clear()
    session._manage.clear()
    _admin_cache.clear()
