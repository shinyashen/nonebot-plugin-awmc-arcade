"""私聊扩权测试：管理群上下文、目标群解析、身份校验与指令链路。

店铺数据取自 tests/data/nearcade/ 真实快照（2026-09-29 取材，来源见其
meta.json）：真实店「天空之城（雨花万象店）」（Nearcade id 14656）。
QQ 号/群号保持合成（个人标识与数据真实性无关）。

SU 直通不触发 API；群管理员经 get_group_member_info 直查（300 秒缓存）；
成员被拒；bot 不在目标群时给友好提示。
"""

from collections.abc import Sequence

from nonebug import App
from conftest import nearcade_snapshot

BASE = "https://nearcade.cn"

# 真实店铺详情原文（「天空之城（雨花万象店）」，搜索候选与详情同源同形）
TENSHI = nearcade_snapshot("shop_tenshi_detail.json")["shop"]
TENSHI_ID = str(TENSHI["id"])  # "14656"

SHOP = TENSHI
SHOP_MENU = (
    "🔍 找到 1 个相关机厅：\n\n"
    f"1. {TENSHI['name']}\n   📍 {TENSHI['address']['detailed']}\n"
    "   🎮 maimai DX（2台）\n\n"
    "回复序号 选择对应机厅\n"
    "「原名」 直接添加「天空之城」\n"
    "「取消」 放弃操作"
)


def _private_event(raw: str, user_id: int = 10):
    from fake import fake_private_message_event_v11
    from nonebot.adapters.onebot.v11 import Message

    return fake_private_message_event_v11(
        message=Message(raw), raw_message=raw, user_id=user_id
    )


def _member_info(role: str, user_id: int) -> dict:
    return {"user_id": user_id, "role": role, "card": "", "nickname": "t"}


async def _run(
    app: App,
    matcher,
    event,
    reply: str | None = None,
    apis: Sequence[tuple] = (),
):
    """执行单事件：apis 依次声明期望的 API 调用 (name, data, result, exception)。"""
    import nonebot
    from nonebot.adapters.onebot.v11 import Bot
    from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

    async with app.test_matcher(matcher) as ctx:
        bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter))
        for name, data, result, *rest in apis:
            ctx.should_call_api(
                name, data, result=result, exception=rest[0] if rest else None
            )
        ctx.receive_event(bot, event)
        if reply is not None:
            ctx.should_call_send(event, reply, result=None, bot=bot)


async def test_manage_group_context(app: App):
    """管理群：设置/查看/清除；仅私聊可用。"""
    from nonebot_plugin_awmc_arcade import session, manage_group_cmd

    await _run(
        app,
        manage_group_cmd,
        _private_event("管理群"),
        "尚未设置管理目标：发送 管理群 <群号>",
    )
    await _run(
        app,
        manage_group_cmd,
        _private_event("管理群 123456"),
        "已将管理目标设为 123456 群（30 分钟内私聊指令默认作用于该群）",
    )
    assert session.get_manage_group("10") == 123456
    await _run(
        app, manage_group_cmd, _private_event("管理群"), "当前管理目标：123456 群"
    )
    await _run(app, manage_group_cmd, _private_event("管理群 取消"), "已清除管理目标")
    assert session.get_manage_group("10") is None


async def test_private_group_manage_superuser(app: App, monkeypatch):
    """SU 直通：不触发成员信息查询。"""
    import nonebot

    from nonebot_plugin_awmc_arcade import add_group, delete_group

    monkeypatch.setattr(nonebot.get_driver().config, "superusers", {"10"})
    await _run(
        app, add_group, _private_event("添加群聊 123456"), "已添加当前群聊到名单中"
    )
    await _run(app, add_group, _private_event("添加群聊 123456"), "当前群聊已在名单中")
    await _run(
        app, delete_group, _private_event("删除群聊 123456"), "已从名单中删除当前群聊"
    )
    await _run(
        app,
        delete_group,
        _private_event("删除群聊 123456"),
        "当前群聊不在名单中，无法删除",
    )


async def test_private_group_manage_admin_verified(app: App, monkeypatch):
    """目标群管理员经成员信息直查放行；结果短缓存（第二条指令不再查询）。"""
    import nonebot

    from nonebot_plugin_awmc_arcade import add_group, delete_group

    monkeypatch.setattr(nonebot.get_driver().config, "superusers", set())
    info = _member_info("admin", 111)
    await _run(
        app,
        add_group,
        _private_event("添加群聊 123456", user_id=111),
        "已添加当前群聊到名单中",
        apis=[
            (
                "get_group_member_info",
                {"group_id": 123456, "user_id": 111, "no_cache": True},
                info,
            )
        ],
    )
    # 缓存生效：删除群聊不再查询
    await _run(
        app,
        delete_group,
        _private_event("删除群聊 123456", user_id=111),
        "已从名单中删除当前群聊",
    )


async def test_private_denied_member(app: App, monkeypatch):
    import nonebot

    from nonebot_plugin_awmc_arcade import add_group

    monkeypatch.setattr(nonebot.get_driver().config, "superusers", set())
    await _run(
        app,
        add_group,
        _private_event("添加群聊 123456", user_id=222),
        "权限不足：你不是该群的管理员",
        apis=[
            (
                "get_group_member_info",
                {"group_id": 123456, "user_id": 222, "no_cache": True},
                _member_info("member", 222),
            )
        ],
    )


async def test_private_verify_api_failure(app: App, monkeypatch):
    """bot 不在目标群（API 失败）给友好提示。"""
    import nonebot

    from nonebot_plugin_awmc_arcade import add_group

    monkeypatch.setattr(nonebot.get_driver().config, "superusers", set())
    await _run(
        app,
        add_group,
        _private_event("添加群聊 123456", user_id=222),
        "身份校验失败：bot 可能不在该群",
        apis=[
            (
                "get_group_member_info",
                {"group_id": 123456, "user_id": 222, "no_cache": True},
                None,
                RuntimeError("not in group"),
            )
        ],
    )


async def test_private_no_target(app: App):
    """未设上下文且无前导群号时提示设置方式。"""
    from nonebot_plugin_awmc_arcade import show_arcade

    await _run(
        app,
        show_arcade,
        _private_event("机厅列表"),
        "请先用 管理群 <群号> 设置管理目标，或在指令开头带上群号",
    )


async def test_private_add_arcade_chain(app: App, monkeypatch):
    """私聊全链路：管理群上下文 → 添加机厅搜索菜单 → 选择 1 落库目标群。"""
    import respx
    import nonebot
    from httpx import Response

    import nonebot_plugin_awmc_arcade.matchers as arcade_matchers
    from nonebot_plugin_awmc_arcade import (
        add_arcade,
        manage_group_cmd,
        session_consumer,
    )
    from nonebot_plugin_awmc_arcade.store import store

    async def _forward_fail(bot, entries, **kwargs):
        return False

    monkeypatch.setattr(arcade_matchers, "try_send_forward", _forward_fail)
    monkeypatch.setattr(nonebot.get_driver().config, "superusers", {"10"})

    await store.add_group(123456)
    await _run(
        app,
        manage_group_cmd,
        _private_event("管理群 123456"),
        "已将管理目标设为 123456 群（30 分钟内私聊指令默认作用于该群）",
    )

    with respx.mock:
        respx.get(f"{BASE}/api/shops").mock(
            return_value=Response(200, json={"shops": [SHOP], "totalCount": 1})
        )
        await _run(app, add_arcade, _private_event("添加机厅 天空之城"), SHOP_MENU)
    await _run(
        app,
        session_consumer,
        _private_event("1"),
        f"✅ 已添加机厅：{SHOP['name']}\n"
        f"🔗 详情链接：https://nearcade.cn/shops/{TENSHI_ID}\n🗺️ 已添加机厅地图",
    )

    entry = await store.get_arcade_by_name(123456, SHOP["name"])
    assert entry is not None
    assert entry.id is not None


async def test_private_delete_arcade_ask(app: App, monkeypatch):
    """私聊缺参追问：下一句即机厅名并作用于目标群。"""
    import nonebot

    from nonebot_plugin_awmc_arcade import delete_arcade, session_consumer
    from nonebot_plugin_awmc_arcade.store import store

    monkeypatch.setattr(nonebot.get_driver().config, "superusers", {"10"})
    await store.add_group(123456)
    entry = await store.add_arcade(123456, "测试店")
    assert entry is not None

    await _run(
        app,
        delete_arcade,
        _private_event("删除机厅 123456"),
        "请输入要删除的机厅名称/序号：",
    )
    await _run(
        app,
        session_consumer,
        _private_event("测试店"),
        "已从群聊名单中删除机厅：测试店",
    )
    assert await store.get_arcade(123456, entry.key) is None


async def test_private_silent_superuser(app: App, monkeypatch):
    """私聊静默模式（SU）：带前导群号作用于目标群。"""
    import nonebot

    from nonebot_plugin_awmc_arcade import silent_on
    from nonebot_plugin_awmc_arcade.store import store

    monkeypatch.setattr(nonebot.get_driver().config, "superusers", {"10"})
    await store.add_group(123456)
    await _run(
        app,
        silent_on,
        _private_event("静默监听模式 123456"),
        "已开启静默监听模式：人数上报仅省略成功回复，错误提示保留",
    )
    cfg = await store.get_group(123456)
    assert cfg is not None
    assert cfg.silent is True

    # 非 SU 私聊根本不触发 SUPERUSER matcher
    await _run(app, silent_on, _private_event("静默监听模式 123456", user_id=222))


async def test_private_not_open_group(app: App, monkeypatch):
    """目标群未开通：与群内同文案。"""
    import nonebot

    from nonebot_plugin_awmc_arcade import add_arcade

    monkeypatch.setattr(nonebot.get_driver().config, "superusers", {"10"})
    await _run(
        app,
        add_arcade,
        _private_event("添加机厅 123456 某店"),
        "本群尚未开通排卡功能,请联系群主或管理员添加群聊",
    )


async def test_private_prefix_overrides_context(app: App, monkeypatch):
    """前导群号优先于管理上下文（临时指定别的群）。"""
    import nonebot

    from nonebot_plugin_awmc_arcade import add_group, manage_group_cmd

    monkeypatch.setattr(nonebot.get_driver().config, "superusers", {"10"})
    await _run(
        app,
        manage_group_cmd,
        _private_event("管理群 111111"),
        "已将管理目标设为 111111 群（30 分钟内私聊指令默认作用于该群）",
    )
    await _run(
        app, add_group, _private_event("添加群聊 123456"), "已添加当前群聊到名单中"
    )
    from nonebot_plugin_awmc_arcade.store import store

    assert await store.get_group(123456) is not None


async def test_private_two_param_commands(app: App, monkeypatch):
    """前导群号 + 双参数指令（回归）：参数从剥掉群号后的文本切分。

    此前 parts 从原始 args 切分在前、前导群号剥离在后且 rest 被弃，
    「添加机厅别名 123456 店A 别B」会把群号当店名、后两段并成别名。
    """
    import nonebot

    from nonebot_plugin_awmc_arcade import add_map, add_alias
    from nonebot_plugin_awmc_arcade.store import store

    monkeypatch.setattr(nonebot.get_driver().config, "superusers", {"10"})
    await store.add_group(123456)
    entry = await store.add_arcade(123456, "测试店")
    assert entry is not None

    await _run(
        app,
        add_alias,
        _private_event("添加机厅别名 123456 测试店 别A"),
        "已成功为 '测试店' 添加别名 '别A'",
    )
    assert await store.list_aliases(123456, entry.key) == ["别A"]

    await _run(
        app,
        add_map,
        _private_event("添加机厅地图 123456 测试店 https://example.com/m"),
        "已成功为 '测试店' 添加机厅地图网址 'https://example.com/m'",
    )
    assert await store.list_maps(123456, entry.key) == ["https://example.com/m"]
