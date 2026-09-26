"""指令层测试：权限门禁、指令链路与会话消费（Nearcade 用 respx 拦截）。

带时间戳的回复文案在 service 层测试中以子串断言；本层只做可精确
比对的端到端链路。
"""

import json

import respx
from httpx import Response
from nonebug import App

# 插件相关导入一律函数内进行（收集期不触发插件加载链）

BASE = "https://nearcade.cn"

SHOP = {
    "id": 123,
    "source": "bemanicn",
    "name": "Nearcade店",
    "address": {"detailed": "某路1号"},
    "games": [{"name": "maimai DX", "quantity": 4, "gameId": 77}],
}

SHOP_MENU = (
    "🔍 找到 1 个相关机厅：\n\n"
    "1. Nearcade店\n   📍 某路1号\n   🎮 maimai DX（4台）\n\n"
    "回复序号 选择对应机厅\n"
    "「原名」 直接添加「近」\n"
    "「取消」 放弃操作"
)


async def _open_group_with_arcade(name: str = "测试店", shop: bool = True):
    from nonebot_plugin_awmc_arcade.store import store

    await store.add_group(87654321)
    entry = await store.add_arcade(
        87654321,
        name,
        created_by="u0",
        **({"shop_id": "123", "source": "bemanicn"} if shop else {}),
    )
    assert entry is not None
    return entry


def _event(raw: str, *, role: str = "member", user_id: int = 12345678, nickname="test"):
    from fake import fake_group_message_event_v11
    from nonebot.adapters.onebot.v11 import Message
    from nonebot.adapters.onebot.v11.event import Sender

    sender = Sender(card="", nickname=nickname, role=role)
    return fake_group_message_event_v11(
        message=Message(raw), raw_message=raw, user_id=user_id, sender=sender
    )


async def _reply(app: App, matcher, event, reply: str) -> None:
    """断言单事件回复（本插件一律纯文本、不 at）。"""
    import nonebot
    from nonebot.adapters.onebot.v11 import Bot
    from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

    async with app.test_matcher(matcher) as ctx:
        bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter))
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, reply, result=None, bot=bot)


async def _no_reply(app: App, matcher, event) -> None:
    """断言事件静默通过（无回复）。"""
    import nonebot
    from nonebot.adapters.onebot.v11 import Bot
    from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

    async with app.test_matcher(matcher) as ctx:
        bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter))
        ctx.receive_event(bot, event)


async def test_group_manage_permission(app: App):
    """成员被拒、管理员/群主可开通、重复开通提示。"""
    from nonebot_plugin_awmc_arcade import add_group, delete_group

    await _reply(app, add_group, _event("添加群聊"), "只有管理员能够添加群聊")
    await _reply(
        app, add_group, _event("添加群聊", role="admin"), "已添加当前群聊到名单中"
    )
    await _reply(app, add_group, _event("添加群聊", role="owner"), "当前群聊已在名单中")
    await _reply(
        app,
        delete_group,
        _event("删除群聊", role="admin"),
        "已从名单中删除当前群聊",
    )
    await _reply(
        app,
        delete_group,
        _event("删除群聊", role="admin"),
        "当前群聊不在名单中，无法删除",
    )


async def test_arcade_manage_flow(app: App, monkeypatch):
    """未开通群拒绝；搜索会话添加机厅；权限与序号删除。"""
    import nonebot_plugin_awmc_arcade.matchers as arcade_matchers
    from nonebot_plugin_awmc_arcade import add_arcade, delete_arcade, session_consumer

    # 协议端不支持合并转发 → 菜单降级为单条文本
    async def _forward_fail(bot, entries, **kwargs):
        return False

    monkeypatch.setattr(arcade_matchers, "try_send_forward", _forward_fail)

    await _reply(
        app,
        add_arcade,
        _event("添加机厅 某店", role="admin"),
        "本群尚未开通排卡功能,请联系群主或管理员添加群聊",
    )
    await _open_group_with_arcade()

    with respx.mock:
        respx.get(f"{BASE}/api/shops").mock(
            return_value=Response(200, json={"shops": [SHOP], "totalCount": 1})
        )
        await _reply(app, add_arcade, _event("添加机厅 近", role="admin"), SHOP_MENU)
    await _reply(
        app,
        session_consumer,
        _event("1"),
        "✅ 已添加机厅：Nearcade店\n"
        "🔗 详情链接：https://nearcade.cn/shops/123\n🗺️ 已添加机厅地图",
    )

    await _reply(app, delete_arcade, _event("删除机厅 2"), "只有管理员能够删除机厅")
    await _reply(
        app,
        delete_arcade,
        _event("删除机厅 2", role="admin"),
        "已从群聊名单中删除机厅：Nearcade店",
    )
    await _reply(
        app,
        delete_arcade,
        _event("删除机厅 1", role="admin"),
        "已从群聊名单中删除机厅：测试店",
    )


async def test_count_update_flow(app: App):
    """人数上报云同步、越界拒绝、非机厅消息不触发。"""
    from nonebot_plugin_awmc_arcade import count_query, count_update

    entry = await _open_group_with_arcade()
    with respx.mock:
        respx.get(f"{BASE}/api/shops/123/attendance").respond(json={"total": 5})
        respx.get(f"{BASE}/api/shops/123").respond(
            json={"shop": {"games": SHOP["games"]}}
        )
        respx.post(f"{BASE}/api/shops/123/attendance").mock(
            return_value=Response(200, json={})
        )
        await _reply(
            app,
            count_update,
            _event("测试店+2"),
            "感谢使用，机厅人数已上传 Nearcade\n📍 测试店  人数已更新为 7\n"
            "🕹️ 机台数量：4 台（每轮 8 人）\n\n✅ 无需等待，快去出勤吧！",
        )

    from nonebot_plugin_awmc_arcade.store import store

    assert await store.current_count(87654321, entry.key) == 7

    await _reply(app, count_update, _event("测试店+99"), "检测到非法数值，拒绝更新")
    await _no_reply(app, count_update, _event("随便聊聊"))
    await _no_reply(app, count_query, _event("别家店几"))


async def test_silent_mode_suppresses_update(app: App, monkeypatch):
    """静默模式（SUPERUSER）：上报不回复。"""
    import nonebot

    from nonebot_plugin_awmc_arcade import silent_on, count_update

    await _open_group_with_arcade()
    monkeypatch.setattr(nonebot.get_driver().config, "superusers", {"12345678"})
    await _reply(
        app,
        silent_on,
        _event("静默监听模式"),
        "已开启静默监听模式：人数上报不再回复，仅同步云端",
    )

    with respx.mock:
        respx.get(f"{BASE}/api/shops/123/attendance").respond(json={"total": 0})
        respx.get(f"{BASE}/api/shops/123").respond(
            json={"shop": {"games": SHOP["games"]}}
        )
        respx.post(f"{BASE}/api/shops/123/attendance").mock(
            return_value=Response(200, json={})
        )
        await _no_reply(app, count_update, _event("测试店+1"))


async def test_queue_flow(app: App):
    """排卡 → 现状 → 延后 → 上机轮转 → 退勤全链路。"""
    from nonebot_plugin_awmc_arcade import go_on, get_in, get_run, put_off, show_list

    await _open_group_with_arcade()
    await _reply(
        app,
        get_in,
        _event("排卡 测试店", user_id=111),
        "收到，您已加入排卡。当前您位于第1位。",
    )
    await _reply(
        app,
        get_in,
        _event("排卡 测试店", user_id=222, nickname="乙"),
        "收到，您已加入排卡。当前您位于第2位。",
    )
    await _reply(
        app,
        show_list,
        _event("排卡现状 测试店"),
        "测试店机厅排卡如下：\n第1位：test\n第2位：乙",
    )
    await _reply(
        app,
        put_off,
        _event("延后", user_id=111),
        "收到，已将测试店机厅中test与乙调换位置",
    )
    await _reply(
        app,
        go_on,
        _event("上机", user_id=222),
        "收到，已将测试店机厅中乙移至最后一位,下一位上机的是test,当前一共有2人",
    )
    await _reply(app, get_run, _event("退勤", user_id=222), "乙从测试店退勤成功")
    await _reply(app, get_run, _event("退勤", user_id=222), "您未加入排卡")


async def test_alias_and_map_commands(app: App):
    """别名/地图增删查与序号操作；成员被拒。"""
    from nonebot_plugin_awmc_arcade import (
        add_map,
        get_map,
        add_alias,
        get_alias,
        delete_map,
        delete_alias,
    )

    await _open_group_with_arcade()
    await _reply(
        app,
        add_alias,
        _event("添加机厅别名 测试店 甲"),
        "只有管理员能够添加机厅别名",
    )
    await _reply(
        app,
        add_alias,
        _event("添加机厅别名 测试店 甲", role="admin"),
        "已成功为 '测试店' 添加别名 '甲'",
    )
    await _reply(
        app,
        add_alias,
        _event("添加机厅别名 1 乙", role="owner"),
        "已成功为 '测试店' 添加别名 '乙'",
    )
    await _reply(
        app,
        add_alias,
        _event("添加机厅别名 测试店 甲", role="admin"),
        "别名 '甲' 已存在，请使用其他别名",
    )
    await _reply(
        app,
        get_alias,
        _event("机厅别名 测试店"),
        "机厅「测试店」的别名列表如下：\n1. 甲\n2. 乙",
    )
    await _reply(
        app,
        delete_alias,
        _event("删除机厅别名 测试店 1", role="admin"),
        "已成功删除 '测试店' 的别名 '甲'",
    )
    await _reply(
        app,
        delete_alias,
        _event("删除机厅别名 测试店 甲", role="admin"),
        "别名 '甲' 不存在，请检查输入的别名",
    )

    await _reply(
        app,
        add_map,
        _event("添加机厅地图 测试店 https://example.com/m", role="admin"),
        "已成功为 '测试店' 添加机厅地图网址 'https://example.com/m'",
    )
    await _reply(
        app,
        get_map,
        _event("机厅地图 测试店"),
        "机厅「测试店」的音游地图网址如下：\n1. https://example.com/m",
    )
    await _reply(
        app,
        delete_map,
        _event("删除机厅地图 测试店 1", role="admin"),
        "已成功从 '测试店' 删除机厅地图网址 'https://example.com/m'",
    )


async def test_updated_list_empty(app: App):
    """当日更新列表（空态；带数据的文案在 service 层覆盖）。"""
    from nonebot_plugin_awmc_arcade import updated_list

    await _open_group_with_arcade()
    await _reply(
        app,
        updated_list,
        _event("jtj"),
        "📋 今日机厅人数更新情况\n\n暂无更新记录\n您可以爽霸机了",
    )


async def test_help(app: App, monkeypatch):
    """帮助默认走合并转发；转发不可用时降级纯文本。

    core.forward 本身由主插件侧测试覆盖，这里只打桩验证本插件的两态分流。
    """
    import nonebot_plugin_awmc_arcade.matchers as arcade_matchers
    from nonebot_plugin_awmc_arcade import arcade_help
    from nonebot_plugin_awmc_arcade.matchers import HELP_TEXT

    async def forward_ok(bot, entries, **kwargs):
        return True

    async def forward_fail(bot, entries, **kwargs):
        return False

    monkeypatch.setattr(arcade_matchers, "try_send_forward", forward_ok)
    await _no_reply(app, arcade_help, _event("机厅help"))
    monkeypatch.setattr(arcade_matchers, "try_send_forward", forward_fail)
    await _reply(app, arcade_help, _event("机厅help"), HELP_TEXT)


async def test_location_listener(app: App):
    """位置消息 → 附近机厅。"""
    from fake import fake_group_message_event_v11
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    from nonebot_plugin_awmc_arcade import location_listener

    cq = json.dumps(
        {"meta": {"Location.Search": {"lat": 31.2, "lng": 121.4, "name": "某地"}}}
    )
    event = fake_group_message_event_v11(
        message=Message([MessageSegment.json(cq)]),
        raw_message=f"[CQ:json,data={cq}]",
    )

    with respx.mock:
        respx.get(f"{BASE}/api/discover").mock(
            return_value=Response(
                200,
                json={
                    "shops": [
                        {"name": "A店", "distance": 0.5, "address": {"detailed": "路1"}}
                    ]
                },
            )
        )
        await _reply(
            app,
            location_listener,
            event,
            "🎮 A店（500米）\n📍 路1\n\n👉 更多详情请点开："
            "https://nearcade.cn/discover?latitude=31.2&longitude=121.4"
            "&radius=10&name=%E6%9F%90%E5%9C%B0",
        )


async def test_ask_session_consumes_next_message(app: App):
    """缺参追问：下一句即参数；会话结束后不再吞消息。"""
    from nonebot_plugin_awmc_arcade import delete_arcade, session_consumer

    await _open_group_with_arcade()
    await _reply(
        app,
        delete_arcade,
        _event("删除机厅", role="admin"),
        "请输入要删除的机厅名称/序号：",
    )
    await _reply(
        app,
        session_consumer,
        _event("测试店", role="admin"),
        "已从群聊名单中删除机厅：测试店",
    )
    await _no_reply(app, session_consumer, _event("测试店"))


async def test_location_variants(app: App, monkeypatch):
    """位置消息四种实测形态：旧版卡片/新版图文卡/高德短链/QQ地图链接。

    以 discover 收到的经纬度为断言点（解析函数本身由纯逻辑保证）。
    """
    import json
    from urllib.parse import quote

    import nonebot
    import respx
    from httpx import Response
    from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter
    from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
    from fake import fake_group_message_event_v11

    from nonebot_plugin_awmc_arcade import location_listener

    tuwen_card = json.dumps(
        {
            "app": "com.tencent.tuwen.lua",
            "meta": {
                "news": [
                    {
                        "title": "南京雨花万象天地",
                        "jumpUrl": {
                            "url": "https://map.wap.qq.com/online/h5-poi-detail-out/index.html"
                            "?uid=1&coord=118.732845%2C31.960199"
                            "&n=%E5%8D%97%E4%BA%AC%E9%9B%A8%E8%8A%B1%E4%B8%87%E8%B1%A1%E5%A4%A9%E5%9C%B0"
                        },
                    }
                ]
            },
        }
    )
    amap_short = "https://surl.amap.com/2kfnweW585X"
    amap_location = (
        "https://wb.amap.com/?p=B0KU7A3X8U%2C31.960541%2C118.732649"
        "%2C%E5%8D%97%E4%BA%AC%E9%9B%A8%E8%8A%B1%E4%B8%87%E8%B1%A1%E5%A4%A9%E5%9C%B0"
        "%2C%E5%90%91%E7%A7%80%E8%B7%AF1%E5%8F%B7"
    )
    qq_poi = (
        "https://map.wap.qq.com/online/h5-poi-detail-out/index.html"
        "?coord=118.732845%2C31.960199&n=%E5%8D%97%E4%BA%AC%E9%9B%A8%E8%8A%B1%E4%B8%87%E8%B1%A1%E5%A4%A9%E5%9C%B0"
    )
    variants = {
        "旧版卡片": (
            Message(
                [
                    MessageSegment.json(
                        json.dumps(
                            {
                                "meta": {
                                    "Location.Search": {
                                        "lat": 31.960199,
                                        "lng": 118.732845,
                                        "name": "南京雨花万象天地",
                                    }
                                }
                            }
                        )
                    )
                ]
            ),
            31.960199,
            118.732845,
            "南京雨花万象天地",
        ),
        "新版图文卡": (
            Message([MessageSegment.json(tuwen_card)]),
            31.960199,
            118.732845,
            "南京雨花万象天地",
        ),
        "高德短链文本": (
            Message(f"南京雨花万象天地\n{amap_short}"),
            31.960541,
            118.732649,
            "南京雨花万象天地",
        ),
        "QQ地图链接文本": (
            Message(qq_poi),
            31.960199,
            118.732845,
            "南京雨花万象天地",
        ),
    }
    for shape, (message, lat, lng, name) in variants.items():
        print("VARIANT:", shape)
        event = fake_group_message_event_v11(message=message, raw_message=str(message))
        with respx.mock:
            discover = respx.get(f"{BASE}/api/discover").mock(
                return_value=Response(
                    200,
                    json={
                        "shops": [
                            {
                                "name": "A店",
                                "distance": 0.5,
                                "address": {"detailed": "路1"},
                            }
                        ]
                    },
                )
            )
            if shape == "高德短链文本":
                respx.get(amap_short).mock(
                    return_value=Response(302, headers={"location": amap_location})
                )
            expected_reply = (
                f"🎮 A店（500米）\n📍 路1\n\n👉 更多详情请点开："
                f"{BASE}/discover?latitude={lat}&longitude={lng}"
                f"&radius=10&name={quote(name)}"
            )
            async with app.test_matcher(location_listener) as ctx:
                bot = ctx.create_bot(
                    base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter)
                )
                ctx.should_call_send(event, expected_reply, result=None, bot=bot)
                ctx.receive_event(bot, event)
            params = discover.calls.last.request.url.params
            assert params["latitude"] == str(lat), shape
            assert params["longitude"] == str(lng), shape
            assert params["name"] == name, shape
