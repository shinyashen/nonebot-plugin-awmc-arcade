"""指令层测试：权限门禁、指令链路与会话消费（Nearcade 用 respx 拦截）。

店铺/发现列表数据取自 tests/data/nearcade/ 真实快照（2026-09-29 取材，
来源见其 meta.json）：真实店「天空之城（雨花万象店）」（Nearcade id 14656，
maimai DX 2 台）；位置消息形态为南京雨花万象天地真实抓包样本。
本地店名「测试店」、QQ/群号保持合成（个人标识与数据真实性无关）。
带时间戳的回复文案在 service 层测试中以子串断言；本层只做可精确
比对的端到端链路。
"""

import json

import respx
from httpx import Response
from nonebug import App
from conftest import nearcade_snapshot

# 插件相关导入一律函数内进行（收集期不触发插件加载链）

BASE = "https://nearcade.cn"

# 真实店铺详情原文（「天空之城（雨花万象店）」，搜索候选与详情同源同形）
DETAIL = nearcade_snapshot("shop_tenshi_detail.json")
TENSHI = DETAIL["shop"]
TENSHI_ID = str(TENSHI["id"])  # "14656"
# 真实出勤响应（total=0：采集时无人上报）
TENSHI_ATT = nearcade_snapshot("shop_tenshi_attendance.json")
# 真实发现列表首条（雨花万象天地坐标附近 36 米，即同一家店）
DISCOVER_SHOP = nearcade_snapshot("discover_yuhua.json")["shops"][0]
_DISCOVER_LINE = (
    f"🎮 {DISCOVER_SHOP['name']}（{DISCOVER_SHOP['distance'] * 1000:.0f}米）\n"
    f"📍 {DISCOVER_SHOP['address']['detailed']}\n\n👉 更多详情请点开："
)

SHOP = TENSHI
SHOP_MENU = (
    "🔍 找到 1 个相关机厅：\n\n"
    f"1. {TENSHI['name']}\n   📍 {TENSHI['address']['detailed']}\n"
    "   🎮 maimai DX（2台）\n\n"
    "回复序号 选择对应机厅\n"
    "「原名」 直接添加「天空之城」\n"
    "「取消」 放弃操作"
)


async def _open_group_with_arcade(name: str = "测试店", shop: bool = True):
    from nonebot_plugin_awmc_arcade.store import store

    await store.add_group(87654321)
    entry = await store.add_arcade(
        87654321,
        name,
        created_by="u0",
        **({"shop_id": TENSHI_ID} if shop else {}),
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
            return_value=Response(200, json={"shops": [TENSHI], "totalCount": 1})
        )
        await _reply(
            app,
            add_arcade,
            _event("添加机厅 天空之城", role="admin"),
            SHOP_MENU,
        )
    await _reply(
        app,
        session_consumer,
        _event("1"),
        f"✅ 已添加机厅：{TENSHI['name']}\n"
        f"🔗 详情链接：https://nearcade.cn/shops/{TENSHI_ID}\n🗺️ 已添加机厅地图",
    )

    await _reply(app, delete_arcade, _event("删除机厅 2"), "只有管理员能够删除机厅")
    await _reply(
        app,
        delete_arcade,
        _event("删除机厅 2", role="admin"),
        f"已从群聊名单中删除机厅：{TENSHI['name']}",
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
        # 真实快照：云端 total=0、店铺详情 2 台（每轮 4 人）→ +2 合并为 2
        respx.get(f"{BASE}/api/shops/{TENSHI_ID}/attendance").respond(json=TENSHI_ATT)
        respx.get(f"{BASE}/api/shops/{TENSHI_ID}").respond(json=DETAIL)
        respx.post(f"{BASE}/api/shops/{TENSHI_ID}/attendance").mock(
            return_value=Response(200, json={})
        )
        await _reply(
            app,
            count_update,
            _event("测试店+2"),
            "感谢使用，机厅人数已上传 Nearcade\n📍 测试店  人数已更新为 2\n"
            "🕹️ 机台数量：2 台（每轮 4 人）\n\n✅ 无需等待，快去出勤吧！",
        )

    from nonebot_plugin_awmc_arcade.store import store

    assert await store.current_count(87654321, entry.key) == 2

    await _reply(app, count_update, _event("测试店+99"), "检测到非法数值，拒绝更新")
    await _no_reply(app, count_update, _event("随便聊聊"))
    # 纯数字串不触发人数上报（裸数字重置要求名字含非数字字符）
    await _no_reply(app, count_update, _event("211"))
    await _no_reply(app, count_update, _event("999999"))
    await _no_reply(app, count_query, _event("别家店几"))


async def test_silent_mode_suppresses_update(app: App, monkeypatch):
    """静默模式（SUPERUSER）：上报不回成功回复，错误提示保留。"""
    import nonebot

    from nonebot_plugin_awmc_arcade import silent_on, count_update

    await _open_group_with_arcade()
    monkeypatch.setattr(nonebot.get_driver().config, "superusers", {"12345678"})
    await _reply(
        app,
        silent_on,
        _event("静默监听模式"),
        "已开启静默监听模式：人数上报仅省略成功回复，错误提示保留",
    )

    with respx.mock:
        # 真实快照：云端 total=0、店铺详情 2 台
        respx.get(f"{BASE}/api/shops/{TENSHI_ID}/attendance").respond(json=TENSHI_ATT)
        respx.get(f"{BASE}/api/shops/{TENSHI_ID}").respond(json=DETAIL)
        respx.post(f"{BASE}/api/shops/{TENSHI_ID}/attendance").mock(
            return_value=Response(200, json={})
        )
        await _no_reply(app, count_update, _event("测试店+1"))


async def test_queue_flow(app: App):
    """排卡 → 现状 → 延后 → 上机轮转 → 退勤 → 闭店全链路。"""
    from nonebot_plugin_awmc_arcade import (
        go_on,
        get_in,
        get_run,
        put_off,
        show_list,
        shut_down,
    )

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

    # 闭店清队：回显真实清理人数（此前硬编码 0）
    await _reply(app, shut_down, _event("闭店 测试店"), "只有管理员能够闭店")
    await _reply(
        app,
        shut_down,
        _event("闭店 测试店", role="admin"),
        "闭店成功，已清空排队 1 人",
    )
    await _reply(app, show_list, _event("排卡现状 测试店"), "测试店机厅排卡如下：\n")


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
    """位置消息 → 附近机厅（真实卡片坐标与真实发现列表首条）。"""
    from urllib.parse import quote

    from fake import fake_group_message_event_v11
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    from nonebot_plugin_awmc_arcade import location_listener

    # 真实抓包坐标：南京雨花万象天地（与 tests/data/nearcade/discover 快照同源）
    cq = json.dumps(
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
    event = fake_group_message_event_v11(
        message=Message([MessageSegment.json(cq)]),
        raw_message=f"[CQ:json,data={cq}]",
    )

    with respx.mock:
        respx.get(f"{BASE}/api/discover").mock(
            return_value=Response(200, json={"shops": [DISCOVER_SHOP]})
        )
        await _reply(
            app,
            location_listener,
            event,
            f"{_DISCOVER_LINE}"
            "https://nearcade.cn/discover?latitude=31.960199&longitude=118.732845"
            f"&radius=10&name={quote('南京雨花万象天地')}",
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
    """位置消息六形态（南京雨花万象天地真实抓包样本）+ 无坐标形态静默忽略。"""
    import json
    from urllib.parse import quote

    import respx
    import nonebot
    from fake import fake_group_message_event_v11
    from httpx import Response
    from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
    from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

    from nonebot_plugin_awmc_arcade import service, location_listener

    def _classic_card() -> Message:
        return Message(
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
        )

    def _tuwen_qq_card() -> Message:
        card = {
            "app": "com.tencent.tuwen.lua",
            "meta": {
                "news": [
                    {
                        "title": "南京雨花万象天地",
                        "jumpUrl": {
                            "url": "https://map.wap.qq.com/online"
                            "/h5-poi-detail-out/index.html?coord=118.732845"
                            "%2C31.960199&n=%E5%8D%97%E4%BA%AC%E9%9B%A8%E8%8A%B1"
                            "%E4%B8%87%E8%B1%A1%E5%A4%A9%E5%9C%B0"
                        },
                    }
                ]
            },
        }
        return Message([MessageSegment.json(json.dumps(card))])

    gl_short = "https://maps.app.goo.gl/mtf6BjL2J6fw32Wm6?g_st=ac"
    gl_pin_target = (
        "https://www.google.com/maps/place/31.960625,118.736397/data="
        "!4m6!3m5!1s0!8m2!3d31.960625399999998!4d118.73639709999999!18m1!1e1"
    )
    gl_glat, gl_glng = service.wgs84_to_gcj02(31.960625399999998, 118.73639709999999)
    bd_marker = "https://map.baidu.com/marker?location=31.9666%2C118.7390&title=%E6%B5%8B%E8%AF%95%E5%BA%97"
    bd_glat, bd_glng = service.bd09_to_gcj02(31.9666, 118.7390)

    # (说明, 消息, mock 短链, mock 跳转目标, 期望纬度, 期望经度, 期望地点名)
    variants = [
        (
            "旧版卡片",
            _classic_card(),
            None,
            None,
            31.960199,
            118.732845,
            "南京雨花万象天地",
        ),
        (
            "新版图文卡",
            _tuwen_qq_card(),
            None,
            None,
            31.960199,
            118.732845,
            "南京雨花万象天地",
        ),
        (
            "高德短链文本",
            Message("南京雨花万象天地\nhttps://surl.amap.com/2kfnweW585X"),
            "https://surl.amap.com/2kfnweW585X",
            "https://wb.amap.com/?p=B0KU7A3X8U%2C31.960541%2C118.732649"
            "%2C%E5%8D%97%E4%BA%AC%E9%9B%A8%E8%8A%B1%E4%B8%87%E8%B1%A1%E5%A4%A9%E5%9C%B0",
            31.960541,
            118.732649,
            "南京雨花万象天地",
        ),
        (
            "QQ地图链接文本",
            Message(
                "https://map.wap.qq.com/online/h5-poi-detail-out/index.html"
                "?coord=118.732845%2C31.960199"
                "&n=%E5%8D%97%E4%BA%AC%E9%9B%A8%E8%8A%B1%E4%B8%87%E8%B1%A1%E5%A4%A9%E5%9C%B0"
            ),
            None,
            None,
            31.960199,
            118.732845,
            "南京雨花万象天地",
        ),
        (
            "谷歌图钉",
            Message(gl_short),
            gl_short,
            gl_pin_target,
            gl_glat,
            gl_glng,
            "未知位置",
        ),
        ("百度marker链接", Message(bd_marker), None, None, bd_glat, bd_glng, "测试店"),
    ]
    for shape, message, mock_url, mock_target, lat, lng, name in variants:
        event = fake_group_message_event_v11(message=message, raw_message=str(message))
        with respx.mock:
            discover = respx.get(f"{BASE}/api/discover").mock(
                return_value=Response(200, json={"shops": [DISCOVER_SHOP]})
            )
            if mock_url:
                respx.get(mock_url).mock(
                    return_value=Response(302, headers={"location": mock_target})
                )
            expected_reply = (
                f"{_DISCOVER_LINE}"
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


async def test_google_place_share_without_coords_ignored(app: App):
    """按地点名分享的谷歌短链（无坐标）：静默忽略。"""
    import respx
    import nonebot
    from httpx import Response
    from nonebot.adapters.onebot.v11 import Bot
    from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

    from nonebot_plugin_awmc_arcade import location_listener

    event = _event("https://maps.app.goo.gl/4T5EXqjprC6g8AJL9")
    with respx.mock:
        respx.get("https://maps.app.goo.gl/4T5EXqjprC6g8AJL9").mock(
            return_value=Response(
                302,
                headers={
                    "location": "https://www.google.com/maps/place"
                    "/%E6%B1%9F%E8%8B%8F%E7%9C%81%E5%8D%97%E4%BA%AC%E5%B8%82"
                    "%E9%9B%A8%E8%8A%B1%E5%8F%B0%E5%8C%BA%E5%A5%BD%E5%8F%88%E5%A4%9A"
                    "%E8%B6%85%E5%B8%82/data=!4m2!3m1!1s0x35b5"
                },
            )
        )
        async with app.test_matcher(location_listener) as ctx:
            bot = ctx.create_bot(
                base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter)
            )
            ctx.receive_event(bot, event)


async def test_tuwen_card_with_amap_string_jumpurl(app: App):
    """回归（真实卡片样本）：news 为字典、jumpUrl 为高德短链字符串。

    2026-09-27 群内实测：新版 QQ 位置卡片的 jumpUrl 是字符串形态的
    surl.amap.com 短链，需跟随 302 才能拿到坐标。
    """
    import respx
    import nonebot
    from fake import fake_group_message_event_v11
    from httpx import Response
    from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
    from nonebot.adapters.onebot.v11 import Adapter as OnebotV11Adapter

    from nonebot_plugin_awmc_arcade import location_listener

    card = json.dumps(
        {
            "app": "com.tencent.tuwen.lua",
            "bizsrc": "qqconnect.sdkshare",
            "meta": {
                "news": {
                    "app_type": 1,
                    "desc": (
                        "江苏省南京市雨花台区向秀路1号电话：(025)57088899查看详情>>"
                    ),
                    "jumpUrl": "https://surl.amap.com/UPNlzw17yd",
                    "tag": "百度地图",
                    "title": "南京雨花万象天地",
                }
            },
            "prompt": "[分享]南京雨花万象天地",
        }
    )
    event = fake_group_message_event_v11(
        message=Message([MessageSegment.json(card)]), raw_message="[CQ:json,data=...]"
    )
    expected_reply = (
        f"{_DISCOVER_LINE}"
        "https://nearcade.cn/discover?latitude=31.960541&longitude=118.732649"
        "&radius=10&name=%E5%8D%97%E4%BA%AC%E9%9B%A8%E8%8A%B1%E4%B8%87%E8%B1%A1%E5%A4%A9%E5%9C%B0"
    )
    with respx.mock:
        respx.get("https://surl.amap.com/UPNlzw17yd").mock(
            return_value=Response(
                302,
                headers={
                    "location": "https://wb.amap.com/?p=B0KU7A3X8U%2C31.960541"
                    "%2C118.732649%2C%E5%8D%97%E4%BA%AC%E9%9B%A8%E8%8A%B1%E4%B8%87"
                    "%E8%B1%A1%E5%A4%A9%E5%9C%B0%2C%E5%90%91%E7%A7%80%E8%B7%AF1%E5%8F%B7"
                },
            )
        )
        respx.get(f"{BASE}/api/discover").mock(
            return_value=Response(200, json={"shops": [DISCOVER_SHOP]})
        )
        async with app.test_matcher(location_listener) as ctx:
            bot = ctx.create_bot(
                base=Bot, adapter=nonebot.get_adapter(OnebotV11Adapter)
            )
            ctx.should_call_send(event, expected_reply, result=None, bot=bot)
            ctx.receive_event(bot, event)


def test_search_gate_rejects_yuanming_without_query():
    """地区全量检索（无关键词）的会话：「原名」不消费不吞消息。"""
    from nonebot_plugin_awmc_arcade import session
    from nonebot_plugin_awmc_arcade.matchers import _valid_search_choice

    session.start(
        session.KIND_SEARCH, 876, "u1", payload={"query": "", "shops": [SHOP]}
    )
    pending = session.get(876, "u1")
    assert pending is not None
    assert _valid_search_choice("原名", pending) is False
    assert _valid_search_choice("更多", pending) is True

    session.start(
        session.KIND_SEARCH, 876, "u1", payload={"query": "天空之城", "shops": [SHOP]}
    )
    pending = session.get(876, "u1")
    assert pending is not None
    assert _valid_search_choice("原名", pending) is True


def test_command_heads_include_command_start_prefixes():
    """命令头按 command_start 笛卡尔补前缀（回归）。

    rule 内只存裸命令头；测试环境 command_start={"", "/"}，派生结果须
    同时含裸头与「/」前缀变体——否则部署配 {"/"} 时追问期会吞掉
    /添加群聊 等带前缀的新指令。
    """
    from nonebot import get_driver

    from nonebot_plugin_awmc_arcade.matchers import _COMMAND_HEADS

    starts = get_driver().config.command_start
    assert "添加群聊" in _COMMAND_HEADS  # command_start 含 ""
    if "/" in starts:
        assert "/添加群聊" in _COMMAND_HEADS
    assert "机厅人数" in _COMMAND_HEADS  # on_fullmatch 同样纳入
