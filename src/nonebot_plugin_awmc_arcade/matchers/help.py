"""帮助域：机厅help / 机厅帮助（M10 帮助注册表驱动）。

指令说明全部迁入主插件帮助注册表（matchers/__init__ 声明块）；本触发词
渲染注册表「排卡」类别页（长文合并转发，降级纯文本）——单源，主插件
「舞萌帮助 排卡」与本指令内容一致。
"""

from nonebot import on_command
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot_plugin_awmc_helper.core.help import (
    CategoryPage,
    page_text,
    page_entries,
    help_registry,
)
from nonebot_plugin_awmc_helper.core.utils import handle_errors

from ._common import _forward_or_text

arcade_help = on_command("机厅help", aliases={"机厅帮助"}, priority=100, block=True)


@arcade_help.handle()
@handle_errors()
async def _(bot: Bot, event: MessageEvent):
    page = CategoryPage(help_registry.categories["arcade"])
    # 长文合并转发（点开查看，避免群内刷屏）；协议端不支持或发送失败时降级纯文本
    await _forward_or_text(
        bot,
        event,
        arcade_help,
        page_entries(help_registry, page),
        page_text(help_registry, page),
    )
