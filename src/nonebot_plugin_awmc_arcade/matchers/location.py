"""位置监听域：群内发送位置消息发现附近机厅（仅群聊）。"""

import re
import json

from nonebot import on_message
from nonebot.typing import T_State
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import MessageEvent, GroupMessageEvent
from nonebot_plugin_awmc_helper.core.utils import handle_errors

from .. import service, nearcade
from ..config import plugin_config


async def _location_rule(state: T_State, event: MessageEvent) -> bool:
    """位置分享门禁：四种实测形态都能提取坐标（详见 service 位置解析注记）。

    依次尝试：OB11 标准 location 段 → json 段（旧版 Location.Search 卡片 /
    新版 tuwen.lua 图文卡）→ 纯文本里的高德短链与 QQ 地图链接（短链需
    跟随 302，仅当文本含相关域名才发起请求）。
    """
    if not isinstance(event, GroupMessageEvent):
        return False
    for seg in event.message:
        if seg.type == "location":
            data = seg.data or {}
            try:
                lat = float(data.get("lat", 0))
                lng = float(data.get("lon", 0) or data.get("lng", 0))
            except (TypeError, ValueError):
                continue
            if lat and lng:
                state["_awmc_arcade_location"] = (
                    lat,
                    lng,
                    data.get("title") or "未知位置",
                )
                return True
        if seg.type != "json":
            continue
        try:
            obj = json.loads(seg.data["data"])
        except Exception:
            continue
        coords = await service.coords_from_card(obj)
        if coords:
            lat, lng, name = coords
            state["_awmc_arcade_location"] = (lat, lng, name or "未知位置")
            return True
    plain = event.message.extract_plain_text()
    if "amap.com" in plain or "map.wap.qq.com" in plain:
        coords = await service.coords_from_text(plain)
        if coords:
            lat, lng, name = coords
            state["_awmc_arcade_location"] = (lat, lng, name or "未知位置")
            return True
    if "goo.gl" in plain or "google.com" in plain:
        m = re.search(
            r"https?://(?:maps\.app\.goo\.gl|goo\.gl|www\.google\.com/maps"
            r"|maps\.google\.com)/\S+",
            plain,
        )
        if m:
            target = m.group(0)
            if "goo.gl" in target:
                target = await nearcade.resolve_redirect(target) or target
            coords = service.coords_from_google_url(target)
            if coords:
                lat, lng, name = coords
                state["_awmc_arcade_location"] = (lat, lng, name or "未知位置")
                return True
    if "map.baidu.com" in plain:
        m = re.search(r"https?://(?:j\.map\.baidu\.com|map\.baidu\.com)/\S+", plain)
        if m:
            target = m.group(0)
            if "j.map.baidu.com" in target:
                target = await nearcade.resolve_redirect(target) or target
            coords = service.coords_from_baidu_url(target)
            if coords:
                lat, lng, name = coords
                state["_awmc_arcade_location"] = (lat, lng, name or "未知位置")
                return True
    return False


location_listener = on_message(priority=100, block=False, rule=_location_rule)


@location_listener.handle()
@handle_errors()
async def _(
    bot: Bot,
    event: GroupMessageEvent,
    state: T_State,
):
    lat, lon, name = state["_awmc_arcade_location"]
    data, web_url = await nearcade.discover(
        lat, lon, radius=plugin_config.awmc_arcade_nearby_radius_km, name=name
    )
    reply = service.discover_reply(data, web_url)
    if reply is not None:
        await location_listener.finish(reply)
