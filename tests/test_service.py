"""域逻辑测试：人数解析换算、云同步编排、搜索会话流转（Nearcade 用 respx 拦截）。

插件相关导入一律函数内进行（收集期不触发插件加载链）。
"""

import respx
from httpx import Response

BASE = "https://nearcade.cn"

SHOP = {
    "id": 123,
    "source": "bemanicn",
    "name": "Nearcade店",
    "address": {"detailed": "某路1号"},
    "games": [{"name": "maimai DX", "quantity": 4, "gameId": 77}],
}


def _routes(attendance: int | None = 5, upload_status: int = 200):
    """ ""常用 Nearcade 路由组；attendance=None 表示出勤接口 500。"""
    respx.get(f"{BASE}/api/shops/123/attendance").respond(
        json={"total": attendance}
    )
    respx.get(f"{BASE}/api/shops/123").respond(
        json={"shop": {"games": SHOP["games"]}}
    )
    respx.post(f"{BASE}/api/shops/123/attendance").mock(
        return_value=Response(upload_status, json={})
    )


async def _arcade_with_shop(**kw):
    from nonebot_plugin_awmc_arcade.store import store

    await store.add_group(876)
    entry = await store.add_arcade(876, "测试店", shop_id="123", source="bemanicn")
    assert entry is not None
    return entry


def test_parse_count_matrix():
    from nonebot_plugin_awmc_arcade import service

    assert service.parse_count("测试店++") == ("测试店", service.OP_INC, None)
    assert service.parse_count("测试店+3") == ("测试店", service.OP_INC, 3)
    assert service.parse_count("测试店--") == ("测试店", service.OP_DEC, None)
    assert service.parse_count("测试店-2") == ("测试店", service.OP_DEC, 2)
    assert service.parse_count("测试店=5") == ("测试店", service.OP_SET, 5)
    assert service.parse_count("测试店5") == ("测试店", service.OP_SET, 5)
    assert service.parse_count("测试店==5") == ("测试店", service.OP_SET, 5)
    # 非上报形态
    assert service.parse_count("测试店") is None
    assert service.parse_count("测试店=") is None
    assert service.parse_count("你好呀") is None


async def test_compute_count_bounds():
    from nonebot_plugin_awmc_arcade import service

    entry = await _arcade_with_shop()
    assert await service.compute_count(entry, service.OP_INC, 3) == (0, 3, 3)
    assert await service.compute_count(entry, service.OP_SET, 99) == (0, 99, 99)
    # 越界与超步长
    assert isinstance(await service.compute_count(entry, service.OP_DEC, 3), str)
    assert isinstance(await service.compute_count(entry, service.OP_INC, 51), str)
    assert isinstance(await service.compute_count(entry, service.OP_SET, 101), str)


async def test_resolve_arcade_index_name_alias():
    from nonebot_plugin_awmc_arcade import service
    from nonebot_plugin_awmc_arcade.store import store

    entry = await _arcade_with_shop()
    await store.add_alias(876, entry.key, "甲店")
    r1 = await service.resolve_arcade(876, "1")
    r2 = await service.resolve_arcade(876, "测试店")
    r3 = await service.resolve_arcade(876, "甲店")
    assert r1 is not None
    assert r2 is not None
    assert r3 is not None
    assert r1.id == entry.key
    assert r2.id == entry.key
    assert r3.id == entry.key
    # 管理操作不接受别名
    assert await service.resolve_arcade(876, "甲店", by_alias=False) is None
    assert await service.resolve_arcade(876, "不存在") is None


async def test_apply_count_local_only():
    from nonebot_plugin_awmc_arcade import service
    from nonebot_plugin_awmc_arcade.store import store

    await store.add_group(876)
    entry = await store.add_arcade(876, "测试店")  # 无店铺关联
    assert entry is not None
    with respx.mock:
        reply = await service.apply_count_update(
            entry, service.OP_INC, 2, "小明", silent=False
        )
    assert reply is not None
    assert "当前人数更新为 2" in reply
    assert "小明" in reply
    assert await store.current_count(876, entry.key) == 2
    # 越界错误文案
    reply = await service.apply_count_update(
        entry, service.OP_SET, 999, "小明", silent=False
    )
    assert reply is not None
    assert "拒绝更新" in reply


@respx.mock
async def test_apply_count_cloud_merge_and_upload():
    from nonebot_plugin_awmc_arcade import service
    from nonebot_plugin_awmc_arcade.store import store

    entry = await _arcade_with_shop()
    _routes(attendance=5)

    # 本地 0，云端 5：+2 合并为 5+2（7 < 每轮 8 人，无需等待）
    reply = await service.apply_count_update(
        entry, service.OP_INC, 2, "小明", silent=False
    )
    assert reply is not None
    assert await store.current_count(876, entry.key) == 7
    assert "感谢使用，机厅人数已上传 Nearcade" in reply
    assert "无需等待" in reply
    await store.reset_count(876, entry.key, 0, None)

    # 队列等待估算：设为 10（每轮 8 人 → 2 人排队）
    respx.get(f"{BASE}/api/shops/123/attendance").respond(json={"total": 10})
    reply = await service.apply_count_update(
        entry, service.OP_SET, 10, "小明", silent=False
    )
    assert reply is not None
    assert "预计等待" in reply
    assert "每轮 8 人" in reply

    # 显式设置为绝对值：云端不同也不合并
    respx.get(f"{BASE}/api/shops/123/attendance").respond(json={"total": 99})
    await service.apply_count_update(entry, service.OP_SET, 3, "小明", silent=False)
    assert await store.current_count(876, entry.key) == 3

    # 静默模式吞掉确认
    reply = await service.apply_count_update(
        entry, service.OP_INC, 1, "小明", silent=True
    )
    assert reply is None


@respx.mock
async def test_apply_count_upload_failures():
    from nonebot_plugin_awmc_arcade import service

    entry = await _arcade_with_shop()
    respx.get(f"{BASE}/api/shops/123/attendance").respond(json={"total": 0})
    respx.get(f"{BASE}/api/shops/123").respond(
        json={"shop": {"games": SHOP["games"]}}
    )
    post = respx.post(f"{BASE}/api/shops/123/attendance").mock(
        return_value=Response(400, json={})
    )
    reply = await service.apply_count_update(
        entry, service.OP_SET, 4, "小明", silent=False
    )
    assert reply is not None
    assert "关门了" in reply

    post.mock(return_value=Response(500, text="boom"))
    reply = await service.apply_count_update(
        entry, service.OP_SET, 4, "小明", silent=False
    )
    assert reply is not None
    assert "上传失败" in reply


@respx.mock
async def test_count_query_and_cloud_align():
    from nonebot_plugin_awmc_arcade import service
    from nonebot_plugin_awmc_arcade.store import store

    entry = await _arcade_with_shop()
    # 无记录且云端一致：未更新文案
    respx.get(f"{BASE}/api/shops/123/attendance").respond(json={"total": 0})
    assert "今日人数尚未更新" in await service.count_query_reply(entry)

    # 云端 6：对齐后展示 6 人，最后更新人标记 Nearcade
    respx.get(f"{BASE}/api/shops/123/attendance").respond(json={"total": 6})
    reply = await service.count_query_reply(entry)
    assert "人数为 6" in reply
    assert "Nearcade" in reply
    assert await store.current_count(876, entry.key) == 6


async def test_updated_today_reply():
    from nonebot_plugin_awmc_arcade import service
    from nonebot_plugin_awmc_arcade.store import store

    await store.add_group(876)
    entry = await _arcade_with_shop()
    assert "暂无更新记录" in await service.updated_today_reply(876)
    await store.add_count_log(876, entry.key, 4, "小明")
    reply = await service.updated_today_reply(876)
    assert "[测试店] 4人" in reply
    assert "小明" in reply


@respx.mock
async def test_begin_add_arcade_search_flow():
    from nonebot_plugin_awmc_arcade import service, session
    from nonebot_plugin_awmc_arcade.store import store

    await store.add_group(876)
    route = respx.get(f"{BASE}/api/shops").mock(
        return_value=Response(200, json={"shops": [SHOP], "totalCount": 4})
    )

    reply = await service.begin_add_arcade(876, 876, "u1", "近")
    assert isinstance(reply, service.AddMenu)  # 挂会话 + 菜单
    assert route.called
    # 节点结构：标题 / 操作说明 / 逐店列表
    assert reply.nodes[0] == "🔍 找到 4 个相关机厅"  # 展示总数
    assert "「更多」" in reply.nodes[1]  # 还有 3 家未列出
    assert "回复序号" in reply.nodes[1]
    assert "「原名」" in reply.nodes[1]
    assert reply.nodes[2] == service.format_shop_info(SHOP, 1)
    assert "Nearcade店" in reply.text
    sess = session.get(876, "u1")
    assert sess is not None
    assert sess.kind == session.KIND_SEARCH

    # 翻页：total=4，已列 1 家 → 「更多」追加并延续序号
    page2 = {"shops": [dict(SHOP, id=456, name="第二页店")], "totalCount": 4}
    respx.get(f"{BASE}/api/shops").mock(
        return_value=Response(200, json={"shops": page2["shops"], "totalCount": 4})
    )
    reply = await service.continue_search(876, "u1", "更多")
    assert isinstance(reply, service.AddMenu)
    assert "2. 第二页店" in reply.text
    sess = session.get(876, "u1")
    assert sess is not None
    assert len(sess.payload["shops"]) == 2

    # 翻页尽头
    respx.get(f"{BASE}/api/shops").mock(
        return_value=Response(200, json={"shops": [], "totalCount": 4})
    )
    assert await service.continue_search(876, "u1", "更多") == "没有更多结果了"

    # 选择 2（第二页店）：落库 + 自动挂链接与地图
    respx.get(f"{BASE}/api/shops").mock(
        return_value=Response(200, json={"shops": [SHOP], "totalCount": 4})
    )
    session.start(
        session.KIND_SEARCH,
        876,
        "u1",
        payload={
            "group_id": 876,
            "shops": [SHOP, page2["shops"][0]],  # 「更多」追加后的累计列表
            "query": "近",
            "page": 2,
            "total": 4,
            "created_by": "u1",
        },
    )
    reply = await service.continue_search(876, "u1", "2")
    assert isinstance(reply, str)
    assert "已添加机厅：第二页店" in reply
    assert "已添加机厅地图" in reply
    entry = await store.get_arcade_by_name(876, "第二页店")
    assert entry is not None
    assert entry.nearcade_shop_id == "456"
    assert await store.list_maps(876, entry.key) == [
        service.shop_web_url(page2["shops"][0])
    ]
    assert session.get(876, "u1") is None  # 会话结束


@respx.mock
async def test_begin_add_arcade_no_result_direct():
    from nonebot_plugin_awmc_arcade import service
    from nonebot_plugin_awmc_arcade.store import store

    await store.add_group(876)
    respx.get(f"{BASE}/api/shops").mock(
        return_value=Response(200, json={"shops": [], "totalCount": 0})
    )
    reply = await service.begin_add_arcade(876, 876, "u1", "无名店")
    assert isinstance(reply, str)
    assert "已直接添加「无名店」" in reply
    assert await store.get_arcade_by_name(876, "无名店") is not None

    # 重复添加
    reply = await service.begin_add_arcade(876, 876, "u1", "无名店")
    assert isinstance(reply, str)
    assert "已在群聊中" in reply


async def test_continue_search_cancel_and_direct():
    from nonebot_plugin_awmc_arcade import service, session
    from nonebot_plugin_awmc_arcade.store import store

    await store.add_group(876)
    session.start(
        session.KIND_SEARCH,
        876,
        "u1",
        payload={
            "group_id": 876,
            "shops": [SHOP],
            "query": "近",
            "page": 1,
            "total": 1,
            "created_by": "u1",
        },
    )
    reply = await service.continue_search(876, "u1", "取消")
    assert isinstance(reply, str)
    assert "已取消" in reply
    assert session.get(876, "u1") is None

    session.start(
        session.KIND_SEARCH,
        876,
        "u1",
        payload={
            "group_id": 876,
            "shops": [SHOP],
            "query": "近",
            "page": 1,
            "total": 1,
            "created_by": "u1",
        },
    )
    reply = await service.continue_search(876, "u1", "原名")
    assert isinstance(reply, str)
    assert "已添加机厅：近" in reply
    assert await store.get_arcade_by_name(876, "近") is not None
    # 无效选择不回复：超范围序号、未知词
    assert await service.continue_search(876, "u1", "9") is None
    assert await service.continue_search(876, "u1", "干嘛") is None


async def test_manage_replies():
    from nonebot_plugin_awmc_arcade import service
    from nonebot_plugin_awmc_arcade.store import store

    entry = await _arcade_with_shop()
    await store.add_alias(876, entry.key, "甲店")
    await store.add_map(876, entry.key, "https://nearcade.cn/shops/bemanicn/123")

    # 管理删除：本名/序号可定位，别名不可
    assert "不在群聊中或为机厅别名" in await service.delete_arcade_reply(876, "甲店")
    assert "已从群聊名单中删除机厅：测试店" in await service.delete_arcade_reply(
        876, "1"
    )

    e2 = await store.add_arcade(876, "店二")
    assert e2 is not None
    await store.add_alias(876, e2.key, "乙")
    reply = await service.alias_list_reply(876, "店二")
    assert reply is not None
    assert "别名列表" in reply
    assert "乙" in reply
    reply = await service.map_list_reply(876, "店二")
    assert reply is not None
    assert "尚未添加地图网址" in reply
    await store.add_map(876, e2.key, "https://example.com/map")
    reply = await service.map_list_reply(876, "店二")
    assert reply is not None
    assert "example.com/map" in reply
    reply = await service.alias_list_reply(876, "不存在")
    assert reply is not None
    assert "找不到" in reply

    # 列表内序号解析
    assert await service.resolve_from_list(["a", "b"], "2") == "b"
    assert await service.resolve_from_list(["a"], "x") is None


async def test_discover_reply():
    from nonebot_plugin_awmc_arcade import service

    data = {
        "shops": [
            {"name": "A店", "distance": 0.5, "address": {"detailed": "路1"}},
            {"name": "B店", "distance": "未知", "address": {}},
        ]
    }
    reply = service.discover_reply(data, f"{BASE}/discover?x=1")
    assert reply is not None
    assert "A店（500米）" in reply
    assert "B店（未知距离）" in reply
    assert service.discover_reply({"shops": []}, f"{BASE}/d") is not None
    assert service.discover_reply({"shops": []}, "") is None  # 查询失败静默


async def test_ensure_daily_reset_compensation():
    from nonebot_plugin_awmc_arcade import service
    from nonebot_plugin_awmc_arcade.store import store

    await store.add_group(876)
    entry = await _arcade_with_shop()
    await store.add_count_log(876, entry.key, 2, "u1")
    # 标记为昨天 → 补偿清零
    await store.set_meta(service._RESET_MARK, "2000-01-01")
    await service.ensure_daily_reset()
    assert await store.current_count(876, entry.key) == 0
    assert await store.get_meta(service._RESET_MARK) != "2000-01-01"
    # 当天已清 → 不重复处理
    await store.add_count_log(876, entry.key, 5, "u1")
    await service.ensure_daily_reset()
    assert await store.current_count(876, entry.key) == 5
