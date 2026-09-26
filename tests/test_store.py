"""存储层测试：群开通级联、别名唯一、队列次序、人数流水与日清。

插件相关导入一律函数内进行（收集期不触发插件加载链）。
"""


async def _make_arcade(group: int = 876, name: str = "测试店"):
    from nonebot_plugin_awmc_arcade.store import store

    entry = await store.add_arcade(group, name, created_by="u1")
    assert entry is not None
    return entry


async def test_group_open_close_cascade():
    from nonebot_plugin_awmc_arcade.store import store

    assert await store.add_group(876) is True
    assert await store.add_group(876) is False  # 重复开通
    entry = await _make_arcade()
    await store.add_alias(876, entry.id, "甲")
    await store.add_count_log(876, entry.id, 3, "u1")
    await store.join_queue(876, entry.id, "u9", "昵称九")

    assert await store.remove_group(876) is True
    assert await store.get_group(876) is None
    assert await store.list_arcades(876) == []
    assert await store.list_aliases(876, entry.id) == []
    assert await store.current_count(876, entry.id) == 0
    assert await store.get_queue_position(876, "u9") is None


async def test_silent_persist():
    from nonebot_plugin_awmc_arcade.store import store

    await store.add_group(876)
    assert (await store.get_group(876)).silent is False
    assert await store.set_silent(876, True) is True
    assert (await store.get_group(876)).silent is True
    # 未开通群设置静默无效
    assert await store.set_silent(999, True) is False


async def test_arcade_unique_and_cascade():
    from nonebot_plugin_awmc_arcade.store import store

    await store.add_group(876)
    entry = await _make_arcade()
    assert await store.add_arcade(876, "测试店") is None  # 群内重名
    assert (await store.get_arcade_by_name(876, "测试店")).id == entry.id

    await store.delete_arcade(876, entry.id)
    assert await store.get_arcade_by_name(876, "测试店") is None


async def test_alias_unique_within_group():
    from nonebot_plugin_awmc_arcade.store import store

    await store.add_group(876)
    e1 = await _make_arcade(name="店一")
    e2 = await _make_arcade(name="店二")
    assert await store.add_alias(876, e1.id, "别名") is True
    # 群内唯一（跨机厅也不允许重名别名，保证解析无歧义）
    assert await store.add_alias(876, e2.id, "别名") is False
    found = await store.find_arcade_by_alias(876, "别名")
    assert found.id == e1.id


async def test_queue_order_operations():
    import sqlalchemy.exc

    from nonebot_plugin_awmc_arcade.store import store

    await store.add_group(876)
    entry = await _make_arcade()
    gid = entry.group_id
    for uid, nick in [("u1", "一"), ("u2", "二"), ("u3", "三")]:
        await store.join_queue(gid, entry.id, uid, nick)

    queue = await store.list_queue(gid, entry.id)
    assert [q.user_id for q in queue] == ["u1", "u2", "u3"]

    # 上机：队首移到队尾
    await store.rotate_queue(gid, entry.id)
    queue = await store.list_queue(gid, entry.id)
    assert [q.user_id for q in queue] == ["u2", "u3", "u1"]

    # 延后：u3 与 u1 交换
    item = await store.delay_in_queue(gid, "u3")
    assert item is not None
    queue = await store.list_queue(gid, entry.id)
    assert [q.user_id for q in queue] == ["u2", "u1", "u3"]

    # 队尾延后无效
    assert await store.delay_in_queue(gid, "u3") is None

    # 群内一人只能排一个机厅（唯一约束）
    e2 = await store.add_arcade(gid, "店二")
    try:
        await store.join_queue(gid, e2.id, "u1", "一")
        raised = False
    except sqlalchemy.exc.IntegrityError:
        raised = True
    assert raised is True

    # 闭店清队
    assert await store.clear_queue(gid, entry.id) == 3
    assert await store.list_queue(gid, entry.id) == []


async def test_count_log_and_reset():
    from nonebot_plugin_awmc_arcade.store import store

    await store.add_group(876)
    entry = await _make_arcade()
    gid, aid = entry.group_id, entry.id

    await store.add_count_log(gid, aid, 3, "u1")
    await store.add_count_log(gid, aid, -1, "u2")
    assert await store.current_count(gid, aid) == 2
    last = await store.last_count_update(gid, aid)
    assert last.delta == -1
    assert last.updated_by == "u2"

    # 绝对重置：清流水后记一行总值
    await store.reset_count(gid, aid, 7, "Nearcade")
    assert await store.current_count(gid, aid) == 7
    assert (await store.last_count_update(gid, aid)).updated_by == "Nearcade"

    # 日清
    assert await store.clear_day_logs() == 1
    assert await store.current_count(gid, aid) == 0


async def test_meta_and_session():
    from nonebot_plugin_awmc_arcade import session
    from nonebot_plugin_awmc_arcade.store import store

    await store.set_meta("k", "v")
    assert await store.get_meta("k") == "v"
    await store.set_meta("k", "v2")  # 覆盖
    assert await store.get_meta("k") == "v2"

    session.start(session.KIND_ASK, 1, "u", topic="t", payload={"a": 1})
    sess = session.get(1, "u")
    assert sess.topic == "t"
    assert sess.payload == {"a": 1}
    # 同键覆盖；弹出后清空
    session.start(session.KIND_SEARCH, 1, "u", payload={"b": 2})
    assert session.get(1, "u").kind == session.KIND_SEARCH
    session.pop(1, "u")
    assert session.get(1, "u") is None
