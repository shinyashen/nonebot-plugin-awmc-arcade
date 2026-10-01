"""域逻辑测试：人数解析换算、云同步编排、搜索会话流转（Nearcade 用 respx 拦截）。

店铺/地区/发现列表数据取自 tests/data/nearcade/ 真实快照（2026-09-29 取材，
来源见其 meta.json）：真实店「天空之城（雨花万象店）」（Nearcade id 14656，
maimai DX 2 台）。云端人数为「当前态」，合并/对齐场景的构造 total 均已注明。
插件相关导入一律函数内进行（收集期不触发插件加载链）。
"""

import respx
from httpx import Response
from conftest import nearcade_snapshot

BASE = "https://nearcade.cn"

# 真实店铺详情原文（「天空之城（雨花万象店）」，搜索候选与详情同源同形）
DETAIL = nearcade_snapshot("shop_tenshi_detail.json")
TENSHI = DETAIL["shop"]
# 真实出勤响应（total=0：采集时无人上报）
TENSHI_ATT = nearcade_snapshot("shop_tenshi_attendance.json")
# 真实搜索页 1 裁剪条目（翻页候选用；totalCount=7265 为捕获原文）
SEARCH_PAGE1 = nearcade_snapshot("search_page1.json")
SEARCH_TOTAL = SEARCH_PAGE1["totalCount"]
# 真实地区树片段（顶层国家原文 + 省/市级真实行政区划）
REGIONS = nearcade_snapshot("regions_fragments.json")
# 真实发现列表（雨花万象天地坐标附近）：天空之城 36 米 / 宝贝王 143 米
DISCOVER_SHOPS = nearcade_snapshot("discover_yuhua.json")["shops"]

TENSHI_ID = str(TENSHI["id"])  # "14656"


def _routes(attendance: int | None = None, upload_status: int = 200):
    """常用 Nearcade 路由组；attendance=None 用真实快照（total=0），
    传值为构造的云端人数（合并/等待场景，见 meta.json 构造注记）。"""
    att = TENSHI_ATT if attendance is None else {"total": attendance}
    respx.get(f"{BASE}/api/shops/{TENSHI_ID}/attendance").respond(json=att)
    respx.get(f"{BASE}/api/shops/{TENSHI_ID}").respond(json=DETAIL)
    respx.post(f"{BASE}/api/shops/{TENSHI_ID}/attendance").mock(
        return_value=Response(upload_status, json={})
    )


async def _arcade_with_shop(**kw):
    from nonebot_plugin_awmc_arcade.store import store

    await store.add_group(876)
    # 本地店名保持合成（群内自定义名），云端关联指向真实店铺 14656
    entry = await store.add_arcade(876, "测试店", shop_id=TENSHI_ID)
    assert entry is not None
    return entry


def test_maimai_game_of_title_id():
    """机种识别 titleId 优先、name 仅兜底（titleId=1 已对上游
    src/lib/constants.ts 核实为 maimai_dx；name 是展示文案可能改名）。"""
    from nonebot_plugin_awmc_arcade import service

    # titleId 命中（真实快照形态）
    cout, gid = service.maimai_game_of(
        [{"titleId": 1, "name": "whatever", "gameId": 7, "quantity": 3}]
    )
    assert (cout, gid) == (3, 7)
    # 无 titleId 时 name 兜底（旧缓存数据/上游回退字段缺失）
    cout, gid = service.maimai_game_of(
        [{"name": "maimai DX", "gameId": 8, "quantity": 2}]
    )
    assert (cout, gid) == (2, 8)
    # 纯太鼓店：不取 games[0] 兜底
    cout, gid = service.maimai_game_of(
        [{"titleId": 5, "name": "太鼓の達人", "gameId": 999, "quantity": 1}]
    )
    assert gid is None


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
    # 纯数字串不是人数上报：裸数字重置要求名字含非数字字符，
    # 否则任意数字串被拆成「序号+人数」误触（211 → 2号厅=11）
    assert service.parse_count("211") is None
    assert service.parse_count("12") is None
    assert service.parse_count("999999") is None
    # 序号定位配显式操作符不受限
    assert service.parse_count("2=11") == ("2", service.OP_SET, 11)
    assert service.parse_count("2+11") == ("2", service.OP_INC, 11)


async def test_compute_count_bounds():
    from nonebot_plugin_awmc_arcade import service

    entry = await _arcade_with_shop()
    assert await service.compute_count(entry, service.OP_INC, 3) == (0, 3, 3)
    assert await service.compute_count(entry, service.OP_SET, 99) == (0, 99, 99)
    # 越界与超步长
    assert isinstance(await service.compute_count(entry, service.OP_DEC, 3), str)
    assert isinstance(await service.compute_count(entry, service.OP_INC, 51), str)
    assert isinstance(await service.compute_count(entry, service.OP_SET, 101), str)


async def test_compute_count_explicit_zero_delta():
    """显式 +0/-0 是「无变化」的合法路径，不再被 falsy 判定当作 +1。"""
    from nonebot_plugin_awmc_arcade import service

    entry = await _arcade_with_shop()
    assert await service.compute_count(entry, service.OP_INC, 0) == (0, 0, 0)
    assert await service.compute_count(entry, service.OP_DEC, 0) == (0, 0, 0)
    # ++（无数字）仍按 +1 处理
    assert await service.compute_count(entry, service.OP_INC, None) == (0, 1, 1)


def test_card_jump_urls_news_shapes():
    """news 为 dict 形态时包成单项列表复用逐项解析（jumpUrl/url 键都收）。"""
    from nonebot_plugin_awmc_arcade.service import _card_jump_urls

    # dict news + dict jumpUrl（含 url 键）：此前静默漏收
    assert _card_jump_urls(
        {"meta": {"news": {"jumpUrl": {"url": "https://a.example/x"}}}}
    ) == ["https://a.example/x"]
    # dict news 顶层 url 键：此前静默漏收
    assert _card_jump_urls({"meta": {"news": {"url": "https://b.example/y"}}}) == [
        "https://b.example/y"
    ]
    # dict news + 字符串 jumpUrl（真实样本形态，行为不变）
    assert _card_jump_urls(
        {"meta": {"news": {"jumpUrl": "https://surl.amap.com/abc"}}}
    ) == ["https://surl.amap.com/abc"]
    # 列表形态行为不变
    assert _card_jump_urls(
        {"meta": {"news": [{"jumpUrl": {"url": "https://c.example/z"}}]}}
    ) == ["https://c.example/z"]
    assert _card_jump_urls({}) == []


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


def test_shop_id_from_url_domain_limited():
    """店铺 id 只认 nearcade.cn/shops/ 全局路径（域名限定）。

    此前的「路径数字后缀」启发式会把任意以数字结尾的网址解析成店铺 id，
    读数合并/上传会写进别人店铺。
    """
    from nonebot_plugin_awmc_arcade import service

    assert service.shop_id_from_url("https://nearcade.cn/shops/14656") == "14656"
    assert service.shop_id_from_url("https://nearcade.cn/shops/14656/") == "14656"
    assert service.shop_id_from_url("nearcade.cn/shops/14656") == "14656"
    # 普通以数字结尾的非 nearcade 链接不再被误解析
    assert service.shop_id_from_url("https://example.com/map/123") is None
    assert service.shop_id_from_url("https://nearcade.cn/discover?x=14656") is None
    # 按数据源隔离的编号路径不认（与全局 id 无关，见 nearcade.py 注记）
    assert service.shop_id_from_url("https://nearcade.cn/shops/bemanicn/123") is None


async def test_shop_id_of_fallback_order():
    """店铺 id 兜底顺序：条目缓存 → nearcade_url 解析 → 地图扫描。"""
    from nonebot_plugin_awmc_arcade import service
    from nonebot_plugin_awmc_arcade.store import store

    await store.add_group(876)
    # 条目缓存优先
    e1 = await store.add_arcade(876, "店一", shop_id="14656")
    assert e1 is not None
    assert await service._shop_id_of(e1) == "14656"
    # shop_id 缺失时解析条目详情链接
    e2 = await store.add_arcade(876, "店二", shop_url="https://nearcade.cn/shops/14656")
    assert e2 is not None
    assert await service._shop_id_of(e2) == "14656"
    # 地图扫描兜底仍生效
    e3 = await store.add_arcade(876, "店三")
    assert e3 is not None
    await store.add_map(876, e3.key, "https://nearcade.cn/shops/14656")
    assert await service._shop_id_of(e3) == "14656"
    # 数字结尾的普通地图网址不再被误绑
    e4 = await store.add_arcade(876, "店四")
    assert e4 is not None
    await store.add_map(876, e4.key, "https://example.com/map/123")
    assert await service._shop_id_of(e4) is None


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
    # 构造云端 5（真实快照 total=0 为「无人」语义，合并需非零对照）
    _routes(attendance=5)

    # 本地 0，云端 5：+2 合并为 5+2=7；真实机台 2 台 → 每轮 4 人，
    # 7 人 = 3 人排队 → 预计等待 round(3/4*16)=12 分钟（0~1 轮）
    reply = await service.apply_count_update(
        entry, service.OP_INC, 2, "小明", silent=False
    )
    assert reply is not None
    assert await store.current_count(876, entry.key) == 7
    assert "感谢使用，机厅人数已上传 Nearcade" in reply
    assert "每轮 4 人" in reply
    assert "预计等待：约 12 分钟" in reply
    await store.reset_count(876, entry.key, 0, None)

    # 真实云端值（total=0）：+2 → 2 ≤ 每轮 4 人，无需等待
    respx.get(f"{BASE}/api/shops/{TENSHI_ID}/attendance").respond(json=TENSHI_ATT)
    reply = await service.apply_count_update(
        entry, service.OP_INC, 2, "小明", silent=False
    )
    assert reply is not None
    assert "无需等待" in reply

    # 队列等待估算：构造云端 10，显式设为 10（每轮 4 人 → 6 人排队）
    respx.get(f"{BASE}/api/shops/{TENSHI_ID}/attendance").respond(json={"total": 10})
    reply = await service.apply_count_update(
        entry, service.OP_SET, 10, "小明", silent=False
    )
    assert reply is not None
    assert "预计等待" in reply
    assert "每轮 4 人" in reply

    # 显式设置为绝对值：云端不同也不合并
    respx.get(f"{BASE}/api/shops/{TENSHI_ID}/attendance").respond(json={"total": 99})
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
    # 云端人数取真实快照（total=0）
    _routes(upload_status=400)
    reply = await service.apply_count_update(
        entry, service.OP_SET, 4, "小明", silent=False
    )
    assert reply is not None
    assert "关门了" in reply

    respx.post(f"{BASE}/api/shops/{TENSHI_ID}/attendance").mock(
        return_value=Response(500, text="boom")
    )
    reply = await service.apply_count_update(
        entry, service.OP_SET, 4, "小明", silent=False
    )
    assert reply is not None
    assert "上传失败" in reply


@respx.mock
async def test_apply_count_no_maimai_game_no_upload():
    """纯太鼓店（详情无 maimai DX 机种）：不取 games[0] 兜底上传。

    上传必须挂在 maimai DX 机种上——兜底取 games[0] 会把人数写进别的
    游戏的出勤计数；无该机种时走「未获取上传通道」分支，仅记录本群。
    """
    from nonebot_plugin_awmc_arcade import service

    entry = await _arcade_with_shop()
    taiko_detail = {
        "shop": {
            "id": int(TENSHI_ID),
            "name": TENSHI["name"],
            "games": [{"name": "太鼓の達人", "gameId": 999, "quantity": 1}],
        }
    }
    respx.get(f"{BASE}/api/shops/{TENSHI_ID}").respond(json=taiko_detail)
    post_route = respx.post(f"{BASE}/api/shops/{TENSHI_ID}/attendance").mock(
        return_value=Response(200, json={})
    )
    reply = await service.apply_count_update(
        entry, service.OP_SET, 4, "小明", silent=False
    )
    assert reply is not None
    assert "未能在 Nearcade 获取上传通道" in reply
    assert not post_route.called


@respx.mock
async def test_count_query_and_cloud_align():
    from nonebot_plugin_awmc_arcade import service
    from nonebot_plugin_awmc_arcade.store import store

    entry = await _arcade_with_shop()
    # 真实快照（云端 total=0 且本地无记录）：未更新文案
    respx.get(f"{BASE}/api/shops/{TENSHI_ID}/attendance").respond(json=TENSHI_ATT)
    assert "今日人数尚未更新" in await service.count_query_reply(entry)

    # 构造云端 6：对齐后展示 6 人（店铺详情真实 2 台 → 每轮 4 人），
    # 更新人标记 Nearcade
    respx.get(f"{BASE}/api/shops/{TENSHI_ID}/attendance").respond(json={"total": 6})
    respx.get(f"{BASE}/api/shops/{TENSHI_ID}").respond(json=DETAIL)
    reply = await service.count_query_reply(entry)
    assert "人数为 6" in reply
    assert "Nearcade" in reply
    assert "每轮 4 人" in reply
    assert await store.current_count(876, entry.key) == 6


async def test_count_query_alignment_holds_lock(monkeypatch):
    """云端对齐段持锁（回归）：查询读云端挂起期间完成的上报不被覆盖。

    时序复现：查询持锁读云端挂起 → +1 上报在对齐结束后才执行 →
    查询恢复后不得用挂起前取到的旧云端值 reset_count 覆盖上报结果。
    """
    import asyncio

    import respx

    from nonebot_plugin_awmc_arcade import service, nearcade
    from nonebot_plugin_awmc_arcade.store import store

    entry = await _arcade_with_shop()
    real_get_attendance = nearcade.get_attendance
    entered = asyncio.Event()  # 查询侧已进入云端读取（挂起点）
    release = asyncio.Event()  # 放行挂起的云端读取
    first_call = True

    async def slow_get_attendance(shop_id: str) -> int | None:
        nonlocal first_call
        if first_call:
            first_call = False
            entered.set()
            await release.wait()
        return await real_get_attendance(shop_id)

    monkeypatch.setattr(nearcade, "get_attendance", slow_get_attendance)

    with respx.mock:
        respx.get(f"{BASE}/api/shops/{TENSHI_ID}/attendance").respond(json=TENSHI_ATT)
        respx.get(f"{BASE}/api/shops/{TENSHI_ID}").respond(json=DETAIL)
        respx.post(f"{BASE}/api/shops/{TENSHI_ID}/attendance").mock(
            return_value=Response(200, json={})
        )
        query_task = asyncio.create_task(service.count_query_reply(entry))
        await asyncio.wait_for(entered.wait(), timeout=5)

        # 对齐持锁期间上报无法插队：本地账本保持 0
        apply_task = asyncio.create_task(
            service.apply_count_update(entry, service.OP_INC, 1, "小明", silent=False)
        )
        for _ in range(10):
            await asyncio.sleep(0)
        assert not apply_task.done()
        assert await store.current_count(876, entry.key) == 0

        # 放行：对齐读到旧云端值 0 与本地一致不回写，随后上报正常完成
        release.set()
        await asyncio.wait_for(apply_task, timeout=5)
        await asyncio.wait_for(query_task, timeout=5)

    assert await store.current_count(876, entry.key) == 1


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
    # 真实捕获：totalCount=7265（上游未按 q 过滤的全量口径，见 meta.json）
    route = respx.get(f"{BASE}/api/shops").mock(
        return_value=Response(200, json={"shops": [TENSHI], "totalCount": SEARCH_TOTAL})
    )

    reply = await service.begin_add_arcade(876, 876, "u1", "天空之城")
    assert isinstance(reply, service.AddMenu)  # 挂会话 + 菜单
    assert route.called
    # 节点结构：标题 / 操作说明 / 逐店列表
    assert reply.nodes[0] == f"🔍 找到 {SEARCH_TOTAL} 个相关机厅"  # 真实总数
    assert "「更多」" in reply.nodes[1]  # 还有更多未列出
    assert "回复序号" in reply.nodes[1]
    assert "「原名」" in reply.nodes[1]
    assert reply.nodes[2] == service.format_shop_info(TENSHI, 1)
    assert "天空之城（雨花万象店）" in reply.text
    sess = session.get(876, "u1")
    assert sess is not None
    assert sess.kind == session.KIND_SEARCH

    # 翻页（真实第二页无独立快照，复用页 1 真实条目充任后续页候选）：
    # 「更多」追加并延续序号
    page2_shop = SEARCH_PAGE1["shops"][0]  # 真实店「（天虹店）贵溪天空之星」
    respx.get(f"{BASE}/api/shops").mock(
        return_value=Response(
            200, json={"shops": [page2_shop], "totalCount": SEARCH_TOTAL}
        )
    )
    reply = await service.continue_search(876, "u1", "更多")
    assert isinstance(reply, service.AddMenu)
    assert f"2. {page2_shop['name']}" in reply.text
    sess = session.get(876, "u1")
    assert sess is not None
    assert len(sess.payload["shops"]) == 2

    # 翻页尽头
    respx.get(f"{BASE}/api/shops").mock(
        return_value=Response(200, json={"shops": [], "totalCount": SEARCH_TOTAL})
    )
    assert await service.continue_search(876, "u1", "更多") == "没有更多结果了"

    # 选择 2（真实店「贵溪天空之星」）：落库 + 自动挂链接与地图
    respx.get(f"{BASE}/api/shops").mock(
        return_value=Response(200, json={"shops": [TENSHI], "totalCount": SEARCH_TOTAL})
    )
    session.start(
        session.KIND_SEARCH,
        876,
        "u1",
        payload={
            "group_id": 876,
            "shops": [TENSHI, page2_shop],  # 「更多」追加后的累计列表
            "query": "天空之城",
            "page": 2,
            "total": SEARCH_TOTAL,
            "created_by": "u1",
        },
    )
    reply = await service.continue_search(876, "u1", "2")
    assert isinstance(reply, str)
    assert f"已添加机厅：{page2_shop['name']}" in reply
    assert "已添加机厅地图" in reply
    entry = await store.get_arcade_by_name(876, page2_shop["name"])
    assert entry is not None
    assert entry.nearcade_shop_id == str(page2_shop["id"])  # 真实 id 14768
    assert await store.list_maps(876, entry.key) == [service.shop_web_url(page2_shop)]
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
            "shops": [TENSHI],
            "query": "天空之城",
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
            "shops": [TENSHI],
            "query": "天空之城",
            "page": 1,
            "total": 1,
            "created_by": "u1",
        },
    )
    reply = await service.continue_search(876, "u1", "原名")
    assert isinstance(reply, str)
    assert "已添加机厅：天空之城" in reply
    assert await store.get_arcade_by_name(876, "天空之城") is not None
    # 无效选择不回复：超范围序号、未知词
    assert await service.continue_search(876, "u1", "9") is None
    assert await service.continue_search(876, "u1", "干嘛") is None


async def test_manage_replies():
    from nonebot_plugin_awmc_arcade import service
    from nonebot_plugin_awmc_arcade.store import store

    entry = await _arcade_with_shop()
    await store.add_alias(876, entry.key, "甲店")
    await store.add_map(876, entry.key, f"https://nearcade.cn/shops/{TENSHI_ID}")

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
    assert service.resolve_from_list(["a", "b"], "2") == "b"
    assert service.resolve_from_list(["a"], "x") is None


async def test_discover_reply():
    from nonebot_plugin_awmc_arcade import service

    # 真实发现列表：天空之城 36 米 / 宝贝王 143 米（雨花万象天地坐标附近）
    tenshi, baby = DISCOVER_SHOPS[0], DISCOVER_SHOPS[1]
    assert tenshi["name"] == "天空之城（雨花万象店）"
    assert baby["name"] == "宝贝王（雨花万象店）"
    reply = service.discover_reply({"shops": [tenshi, baby]}, f"{BASE}/discover?x=1")
    assert reply is not None
    # 真实距离 0.03647km / 0.14327km
    assert f"🎮 {tenshi['name']}（36米）" in reply
    assert f"📍 {tenshi['address']['detailed']}" in reply
    assert f"🎮 {baby['name']}（143米）" in reply
    assert f"📍 {baby['address']['detailed']}" in reply

    # 非数值距离容错分支：真实条目距离均为数值，构造字符串距离覆盖（见 meta.json）
    weird = {"shops": [{"name": baby["name"], "distance": "未知", "address": {}}]}
    reply = service.discover_reply(weird, f"{BASE}/d")
    assert reply is not None
    assert "未知距离" in reply
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


@respx.mock
async def test_region_filtered_search():
    """@地区词解析（国名缺省回退中国→逐级下钻）、regionId 过滤与 7 天缓存。

    地区树用真实片段：中国（CN）→ 江苏省（CN-32）/ 辽宁省（CN-21）→
    南京市（CN-3201）/ 大连市（CN-2102）。
    """
    from nonebot_plugin_awmc_arcade import service, session
    from nonebot_plugin_awmc_arcade.store import store

    await store.add_group(876)

    def _regions(request):
        parent = request.url.params.get("parentId") or ""
        return Response(200, json=REGIONS.get(parent, []))

    regions_route = respx.get(f"{BASE}/api/regions").mock(side_effect=_regions)
    search_route = respx.get(f"{BASE}/api/shops").mock(
        return_value=Response(200, json={"shops": [TENSHI], "totalCount": 1})
    )

    reply = await service.begin_add_arcade(876, 876, "u1", "天空之城 @南京")
    assert isinstance(reply, service.AddMenu)
    assert "（江苏省/南京市）" in reply.nodes[0]
    sess = session.get(876, "u1")
    assert sess is not None
    assert sess.payload["region_id"] == "CN-3201"  # 南京市真实行政区划 id
    assert "regionId=CN-3201" in str(search_route.calls.last.request.url)

    # 地区树已缓存：第二位用户同地区查询不再触发 regions 请求
    first_count = regions_route.call_count
    reply = await service.begin_add_arcade(876, 876, "u2", "天空之城 @江苏 @南京")
    assert isinstance(reply, service.AddMenu)
    assert regions_route.call_count == first_count

    # 未知地区给可选子级提示（雨花台区为区级，超出省→市下钻范围）
    reply = await service.begin_add_arcade(876, 876, "u3", "天空之城 @雨花台")
    assert isinstance(reply, str)
    assert "未找到地区" in reply


@respx.mock
async def test_region_only_search_and_cap():
    """仅 @地区 检索：空关键词 + regionId；超上限不起会话并提示缩小。"""
    from nonebot_plugin_awmc_arcade import service, session
    from nonebot_plugin_awmc_arcade.store import store

    await store.add_group(876)

    def _regions(request):
        parent = request.url.params.get("parentId") or ""
        return Response(200, json=REGIONS.get(parent, []))

    respx.get(f"{BASE}/api/regions").mock(side_effect=_regions)
    # 候选总数构造小值（真实南京市全量数未捕获）：验证「更多」翻页分支
    search_route = respx.get(f"{BASE}/api/shops").mock(
        return_value=Response(200, json={"shops": [TENSHI], "totalCount": 7})
    )

    # 正常规模：挂会话出菜单，标题带地区路径、无「原名」动作
    reply = await service.begin_add_arcade(876, 876, "u1", "@南京")
    assert isinstance(reply, service.AddMenu)
    assert "（江苏省/南京市）" in reply.nodes[0]
    assert "「原名」" not in reply.nodes[1]
    assert "「更多」" in reply.nodes[1]
    assert "「取消」" in reply.nodes[1]
    url = str(search_route.calls.last.request.url)
    assert "regionId=CN-3201" in url
    sess = session.get(876, "u1")
    assert sess is not None
    assert sess.payload["query"] == ""

    # 地区会话里「原名」无效（静默吞、会话保留），「取消」正常收尾
    assert await service.continue_search(876, "u1", "原名") is None
    assert session.get(876, "u1") is not None
    assert await service.continue_search(876, "u1", "取消") == "❌ 已取消添加操作"

    # 空串（追问会话可能流入）：用法提示，不检索不落库
    reply = await service.begin_add_arcade(876, 876, "u4", "")
    assert isinstance(reply, str)
    assert "请输入机厅名称" in reply

    # 地区无收录（构造空搜索响应，见 meta.json）：不落空名机厅
    respx.get(f"{BASE}/api/shops").mock(
        return_value=Response(200, json={"shops": [], "totalCount": 0})
    )
    reply = await service.begin_add_arcade(876, 876, "u2", "@南京")
    assert isinstance(reply, str)
    assert "未收录" in reply
    assert await store.list_arcades(876) == []

    # 超上限（真实 totalCount=7265 > 50）：不起选择会话，
    # 转发文案带上限数值与真实下辖地区
    respx.get(f"{BASE}/api/shops").mock(
        return_value=Response(200, json={"shops": [TENSHI], "totalCount": SEARCH_TOTAL})
    )
    reply = await service.begin_add_arcade(876, 876, "u3", "@江苏")
    assert isinstance(reply, service.AddMenu)
    assert f"{SEARCH_TOTAL} 家机厅" in reply.text
    assert "上限（50 家）" in reply.text
    assert "「江苏省」下辖：南京市" in reply.text
    assert session.get(876, "u3") is None
