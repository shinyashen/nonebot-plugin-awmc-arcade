"""帮助域：机厅help / 机厅帮助（长文合并转发，降级纯文本）。"""

from nonebot import on_command
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot_plugin_awmc_helper.core.utils import handle_errors

from ._common import _forward_or_text

HELP_TEXT = (
    "机厅人数:\n"
    "[<机厅名>++/--] 机厅的人数+1/-1\n"
    "[<机厅名>+num/-num] 机厅的人数+num/-num\n"
    "[<机厅名>=num/<机厅名>num] 机厅的人数重置为num\n"
    "[<机厅名>几/几人/j] 展示机厅当前的人数信息\n"
    "[mai/机厅人数] 展示当日已更新的所有机厅的人数列表\n"
    "群聊管理:\n"
    "[添加群聊] (管理)将群聊添加到名单中\n"
    "[删除群聊] (管理)从名单中删除指定的群聊\n"
    "机厅管理:\n"
    "[添加机厅 <店名> [@地区…]] (管理)将机厅添加到群聊（@南京 按地区过滤重名）\n"
    "[删除机厅] (管理)从群聊中删除指定的机厅\n"
    "[机厅列表] 展示当前机厅列表\n"
    "[添加机厅别名 <机厅名> <别名>] (管理)为机厅添加别名\n"
    "[删除机厅别名 <机厅名> <别名/序号>] (管理)移除机厅的别名\n"
    "[机厅别名 <机厅名>] 展示机厅别名\n"
    "[添加机厅地图 <机厅名> <地图URL>] (管理)添加机厅地图信息\n"
    "[删除机厅地图 <机厅名> <地图URL/序号>] (管理)移除机厅地图信息\n"
    "[机厅地图 <机厅名>] 展示机厅音游地图\n"
    "排卡功能:\n"
    "[上机] 将当前第一位排队的移至最后\n"
    "[排卡] 加入排队队列\n"
    "[退勤] 从排队队列中退出\n"
    "[排卡现状] 展示当前排队队列的情况\n"
    "[延后] 将自己延后一位\n"
    "[闭店] (管理)清空排队队列\n"
    "私聊管理（SUPERUSER 或目标群管理员）:\n"
    "[管理群 <群号>] 设置私聊管理目标（默认30分钟内有效）\n"
    "[管理群] 查看当前目标；[管理群 取消] 清除\n"
    "设置后私聊直接发上述管理/查询指令即可作用于目标群，"
    "或在指令开头带群号临时指定（如 添加机厅 123456 某店）\n"
    "索引支持:\n"
    "机厅名、别名、地图URL均可用序号代替 (使用 机厅列表 命令查看)\n"
    "示例：删除机厅别名 1 2 (删除第1个机厅的第2个别名)\n"
    "项目地址:\n"
    "https://github.com/shinyashen/nonebot-plugin-awmc-arcade\n"
)

arcade_help = on_command("机厅help", aliases={"机厅帮助"}, priority=100, block=True)


@arcade_help.handle()
@handle_errors()
async def _(bot: Bot, event: MessageEvent):
    # 长文合并转发（点开查看，避免群内刷屏）；协议端不支持或发送失败时降级纯文本
    await _forward_or_text(bot, event, arcade_help, [HELP_TEXT], HELP_TEXT)
