"""
工具函数模块
"""

from nonebot.permission import SUPERUSER
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.adapters.onebot.v11.permission import GROUP_ADMIN, GROUP_OWNER
from .config import data_json

async def is_superuser_or_admin(bot: Bot, event: GroupMessageEvent) -> bool:
    """
    检查用户是否为超级用户或群管理员
    
    Args:
        bot: Bot实例
        event: 群消息事件
        
    Returns:
        bool: 如果用户是超级用户或群管理员则返回True，否则返回False
    """
    return await (SUPERUSER | GROUP_ADMIN | GROUP_OWNER)(bot, event)


def resolve_arcade_name(input_name: str, group_id: str) -> str:
    """
    解析机厅名称，支持序号和名称两种输入方式
    
    Args:
        input_name: 用户输入的机厅名称或序号
        group_id: 群组ID
        
    Returns:
        解析后的机厅名称，如果无法解析则返回原输入
    """
    # 如果群组不存在，直接返回原输入
    if group_id not in data_json:
        return input_name
    
    # 尝试解析为序号
    try:
        index = int(input_name) - 1  # 序号从1开始，数组索引从0开始
        arcade_list = list(data_json[group_id].keys())
        if 0 <= index < len(arcade_list):
            return arcade_list[index]
    except ValueError:
        # 不是数字，直接返回原输入作为机厅名称
        pass
    
    return input_name


def resolve_alias_by_index(arcade_name: str, alias_input: str, group_id: str) -> str:
    """
    解析别名，支持序号和别名两种输入方式
    
    Args:
        arcade_name: 机厅名称
        alias_input: 用户输入的别名或序号
        group_id: 群组ID
    
    Returns:
        解析后的别名，如果无法解析则返回原输入
    """
    if group_id not in data_json or arcade_name not in data_json[group_id]:
        return alias_input
    
    arcade_data = data_json[group_id][arcade_name]
    if 'alias_list' not in arcade_data or not arcade_data['alias_list']:
        return alias_input
    
    # 尝试解析为序号
    try:
        index = int(alias_input) - 1  # 序号从1开始，数组索引从0开始
        aliases = arcade_data['alias_list']
        if 0 <= index < len(aliases):
            return aliases[index]
    except ValueError:
        # 不是数字，直接返回原输入作为别名
        pass
    
    return alias_input


def resolve_map_by_index(arcade_name: str, map_input: str, group_id: str) -> str:
    """
    解析地图URL，支持序号和URL两种输入方式
    
    Args:
        arcade_name: 机厅名称
        map_input: 用户输入的地图URL或序号
        group_id: 群组ID
    
    Returns:
        解析后的地图URL，如果无法解析则返回原输入
    """
    if group_id not in data_json or arcade_name not in data_json[group_id]:
        return map_input
    
    arcade_data = data_json[group_id][arcade_name]
    if 'map' not in arcade_data or not arcade_data['map']:
        return map_input
    
    # 尝试解析为序号
    try:
        index = int(map_input) - 1  # 序号从1开始，数组索引从0开始
        maps = arcade_data['map']
        if 0 <= index < len(maps):
            return maps[index]
    except ValueError:
        # 不是数字，直接返回原输入作为URL
        pass
    
    return map_input


def format_shop_info(shop: dict, index: int) -> str:
    """格式化机厅信息显示"""
    name = shop.get('name', '未知机厅')
    address = shop.get('address', {}).get('detailed', '地址未知')
    games_info = []
    
    for game in shop.get('games', []):
        game_name = game.get('name', '未知游戏')
        quantity = game.get('quantity', 0)
        if quantity > 0:
            games_info.append(f"{game_name} ({quantity}台)")
    
    games_str = " | ".join(games_info[:2]) if games_info else "游戏信息未知"
    
    return f"{index}️⃣ {name}\n   📍 {address}\n   🎮 {games_str}"


def get_shop_url(shop: dict) -> str:
    """获取机厅的nearcade URL"""
    source = shop.get('source', 'bemanicn')
    shop_id = shop.get('id')
    if shop_id:
        return f"https://nearcade.cn/shops/{source}/{shop_id}"
    return ""