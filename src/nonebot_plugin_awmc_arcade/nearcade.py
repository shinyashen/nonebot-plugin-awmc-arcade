"""Nearcade（nearcade.cn）API 客户端（httpx 全异步）。

上游 mai_arcade 在人数上报链路里使用**同步** ``http.client``——会卡住
整个事件循环，本迁移统一改为共享的 ``httpx.AsyncClient``。
本模块无注册副作用；共享连接的关闭钩子由装配层注册。

接口面（均为上游实际用到的子集）：
- 店铺关键词搜索（添加机厅时的候选列表）
- 位置发现（附近机厅）
- 店铺详情（maimai DX 机台数、gameId）
- 出勤人数读取 / 上传（人数云同步）
"""

import httpx
from nonebot import logger
from nonebot_plugin_awmc_helper.core.http import build_smart_transport

BASE = "https://nearcade.cn"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NoneBot-Arcade-Plugin)",
    "Accept": "application/json",
}
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # 智能代理传输层：国内站直连优先、连接失败代理回退；
        # 主插件未配 AWMC_PROXY 时返回 None，行为与默认 transport 一致
        _client = httpx.AsyncClient(
            timeout=15, headers=_HEADERS, transport=build_smart_transport()
        )
    return _client


async def aclose() -> None:
    """关闭共享连接（装配层 on_shutdown 注册）。"""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def search_shops(keyword: str, page: int = 1, limit: int = 3) -> dict:
    """关键词搜索店铺；失败时返回空结果（调用方按「无结果」降级直加）。"""
    try:
        resp = await _http().get(
            f"{BASE}/api/shops",
            params={"q": keyword, "page": page, "limit": limit},
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "shops": data.get("shops", []),
            "totalCount": data.get("totalCount", 0),
        }
    except Exception as e:
        logger.warning(f"Nearcade 搜索失败：{e}")
        return {"shops": [], "totalCount": 0}


async def discover(
    lat: float, lon: float, radius: int = 10, name: str | None = None
) -> tuple[dict, str]:
    """按经纬度发现附近机厅，返回 (数据, 网页端同参链接)；失败返回空数据。"""
    params: dict[str, str] = {
        "latitude": str(lat),
        "longitude": str(lon),
        "radius": str(radius),
    }
    if name:
        params["name"] = name
    try:
        resp = await _http().get(f"{BASE}/api/discover", params=params)
        resp.raise_for_status()
        return resp.json(), f"{BASE}/discover?{httpx.QueryParams(params)}"
    except Exception as e:
        logger.warning(f"Nearcade 附近机厅查询失败：{e}")
        return {}, ""


async def get_shop(shop_id: str) -> dict | None:
    """店铺详情（games 含 gameId 与各机种 quantity）；失败返回 None。"""
    try:
        resp = await _http().get(f"{BASE}/api/shops/bemanicn/{shop_id}")
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"Nearcade 店铺详情获取失败（{shop_id}）：{e}")
        return None


async def get_attendance(shop_id: str) -> int | None:
    """云端当前出勤人数；失败返回 None。"""
    try:
        resp = await _http().get(f"{BASE}/api/shops/bemanicn/{shop_id}/attendance")
        resp.raise_for_status()
        return int(resp.json().get("total", 0))
    except Exception as e:
        logger.warning(f"Nearcade 出勤人数获取失败（{shop_id}）：{e}")
        return None


async def upload_attendance(
    shop_id: str, game_id: int, count: int, token: str
) -> tuple[int, str]:
    """上传出勤人数，返回 (HTTP 状态码, 响应文本)。"""
    try:
        resp = await _http().post(
            f"{BASE}/api/shops/bemanicn/{shop_id}/attendance",
            json={"games": [{"id": game_id, "currentAttendances": count}]},
            headers={"Authorization": f"Bearer {token}"},
        )
        return resp.status_code, resp.text
    except Exception as e:
        logger.warning(f"Nearcade 出勤人数上传失败（{shop_id}）：{e}")
        return -1, str(e)
