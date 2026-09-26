"""插件配置项（.env 按 pydantic 字段名大写书写，前缀 ``AWMC_ARCADE_``）。"""

from nonebot import get_plugin_config
from pydantic import BaseModel


class SmartTipRule(BaseModel):
    """等待时间提示规则：预计等待 ≤ max_minutes 分钟时展示 tip。

    规则列表按 max_minutes 升序匹配，首条命中即生效；
    max_minutes=0 表示无需等待。
    """

    max_minutes: int
    tip: str


class Config(BaseModel):
    # Nearcade 开发者 API 令牌（人数上传用）。默认值为上游项目公开的
    # 开发令牌，仅保证开箱可用；正式部署建议申请自己的令牌替换。
    awmc_arcade_nearcade_api_token: str = (
        "nk_eimMHQaX7F6g0LlLg6ihhweRQTyLxUTVKHuIdijadC"
    )
    # 等待时间提示规则（按 max_minutes 升序）
    awmc_arcade_smart_tips: list[SmartTipRule] = [
        SmartTipRule(max_minutes=0, tip="✅ 无需等待，快去出勤吧！"),
        SmartTipRule(max_minutes=20, tip="✅ 舞萌启动！"),
        SmartTipRule(max_minutes=40, tip="🕰️ 小排队还能忍"),
        SmartTipRule(max_minutes=90, tip="💀 DBD，纯折磨，建议换店"),
        SmartTipRule(max_minutes=9999, tip="🪦 建议回家（或者明天再来）"),
    ]
    # 单次人数变更上限（±，超过拒绝上报）
    awmc_arcade_max_delta: int = 50
    # 人数合法区间 [0, max]
    awmc_arcade_max_count: int = 100
    # 单轮游玩时长（分钟）：等待时间 = 排队轮数 × 该值
    awmc_arcade_per_round_minutes: int = 16
    # 附近机厅发现半径（公里）
    awmc_arcade_nearby_radius_km: int = 10
    # 追问/搜索选择会话的 TTL（秒），超时未回应自动失效
    awmc_arcade_session_ttl: int = 120


plugin_config: Config = get_plugin_config(Config)
