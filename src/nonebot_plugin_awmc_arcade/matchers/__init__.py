"""指令入口包：按域拆分的 matcher 模块（装配层 import 本包即完成注册）。

原单文件 matchers.py（千行巨石）按域拆分：每个域模块自行定义 matcher 与
handler（import 即注册），本包 ``__init__`` 汇总导出全部 matcher 与
本文件只做装配式 re-export（帮助文案已迁主插件注册表，声明块见文末），
零业务；业务编排在 service/store/nearcade。消息文案与上游 mai_arcade
保持一致（个别明显笔误顺手修正），指令集合与语义不增不减：

- manage：群管理——添加群聊 / 删除群聊（管理员）、管理群上下文（仅私聊）、
  静默监听模式开关（SUPERUSER）
- arcade：机厅管理——添加机厅（Nearcade 搜索选择）/ 删除机厅 / 机厅列表
- resource：机厅别名 / 机厅地图——两组同构 handler 收敛为参数化增删查流程
- count：人数——名++/--/+n/-n/=n/裸数字 上报（Nearcade 云同步）、
  名几/几人/j 查询、mai/机厅人数/jtj/机厅几人 当日更新列表
- queue：排卡——上机 / 排卡 / 退勤 / 排卡现状 / 延后 / 闭店（管理员）
- help：帮助
- consumer：会话消费（搜索选择、指令缺参追问）——priority=0 统一消费，
  不用 ``got``（awmc 生态约定）；命令头从各命令域模块显式汇总派生
- location：位置监听——群内发送位置消息发现附近机厅

私聊扩权（上游没有的能力）：SUPERUSER 或目标群管理员可在私聊执行上述
管理/查询类指令，目标群经「管理群 <群号>」上下文或指令前导群号指定，
身份经 get_group_member_info 直查校验——详见 ``_common._target_group``。
排除项：``updated_list``（mai/机厅人数 当日更新列表）天然群维度，不在
扩权面内，私聊无响应属有意。人数上报、排卡操作与位置监听保持仅群聊
（排队/上报者是群成员本人，私聊无语义）。

域模块间禁止互相 import（共享口径收敛 ``_common``）；唯一例外是 consumer
显式汇总命令域模块用于命令头派生。
"""

from .help import arcade_help
from .count import count_query, count_update, updated_list
from .queue import go_on, get_in, get_run, put_off, show_list, shut_down
from .arcade import add_arcade, show_arcade, delete_arcade
from .manage import add_group, silent_on, silent_off, delete_group, manage_group_cmd
from .consumer import session_consumer
from .location import location_listener
from .resource import (
    add_map,
    get_map,
    add_alias,
    get_alias,
    delete_map,
    delete_alias,
)

__all__ = [
    "add_alias",
    "add_arcade",
    "add_group",
    "add_map",
    "arcade_help",
    "count_query",
    "count_update",
    "delete_alias",
    "delete_arcade",
    "delete_group",
    "delete_map",
    "get_alias",
    "get_in",
    "get_map",
    "get_run",
    "go_on",
    "location_listener",
    "manage_group_cmd",
    "put_off",
    "session_consumer",
    "show_arcade",
    "show_list",
    "shut_down",
    "silent_off",
    "silent_on",
    "updated_list",
]


# ---------------------------------------------------------------- 帮助声明
# 指令说明单源：主插件 M10 帮助注册表（「排卡」类别）；机厅help 触发词渲染
# 同一类别页（help.py）。与主插件内置 awmc.arcade 同名指令（添加/删除机厅、
# 机厅几人等）二者只能启用其一，查找索引先到先得——内置块在前不致错乱。
# 「管理群」为私聊扩权入口（SUPERUSER 或目标群管理员，身份由 _target_group
# 经 get_group_member_info 校验）：不属 hidden 管理面，以 scope 标注展示。
from nonebot_plugin_awmc_helper.core.help import (
    Guide,
    GuideStep,
    CommandSpec,
    help_registry,
)

_ARCADE_PLUGIN = "awmc-arcade"

help_registry.declare(
    plugin=_ARCADE_PLUGIN,
    title="机厅排卡",
    category="arcade",
    description="机厅人数上报 / 附近机厅 / 线上排卡 / Nearcade 云同步",
    commands=[
        # 群管理域
        CommandSpec(
            matcher=add_group,
            name="添加群聊",
            scope="群管",
            brief="将本群加入机厅插件名单（开通前提）",
        ),
        CommandSpec(
            matcher=delete_group,
            name="删除群聊",
            scope="群管",
            brief="将本群移出名单",
        ),
        CommandSpec(
            matcher=manage_group_cmd,
            name="管理群",
            scope="仅私聊 · SUPERUSER/目标群管",
            brief="设置私聊管理目标群（30 分钟内私聊指令默认作用于该群）",
            detail=(
                "格式：管理群 <群号>；无参查看当前目标，「管理群 取消」清除。\n"
                "设置后私聊直接发管理/查询指令即可作用于目标群，或在指令开头"
                "带群号临时指定（如 添加机厅 123456 某店）。"
            ),
        ),
        CommandSpec(
            matcher=silent_on,
            name="静默监听模式",
            aliases=("静默模式", "监听模式"),
            scope="SUPERUSER",
            hidden=True,
            brief="人数上报仅省略成功回复（错误提示保留）",
        ),
        CommandSpec(
            matcher=silent_off,
            name="关闭静默监听模式",
            aliases=("关闭静默模式", "关闭监听模式"),
            scope="SUPERUSER",
            hidden=True,
            brief="恢复正常回复",
        ),
        # 机厅管理域
        CommandSpec(
            matcher=add_arcade,
            name="添加机厅",
            scope="群管",
            brief="向本群添加机厅（重名可 @地区 过滤，Nearcade 搜索选择）",
            detail="格式：添加机厅 <店名> [@地区…]",
        ),
        CommandSpec(
            matcher=delete_arcade,
            name="删除机厅",
            aliases=("移除机厅",),
            scope="群管",
            brief="从本群删除指定机厅",
        ),
        CommandSpec(
            matcher=show_arcade,
            name="机厅列表",
            aliases=("群机厅",),
            brief="展示本群机厅列表（序号索引入口）",
        ),
        # 别名 / 地图域
        CommandSpec(
            matcher=add_alias,
            name="添加机厅别名",
            scope="群管",
            brief="为机厅添加别名",
            detail="格式：添加机厅别名 <店名/序号> <别名>",
        ),
        CommandSpec(
            matcher=delete_alias,
            name="删除机厅别名",
            aliases=("移除机厅别名",),
            scope="群管",
            brief="移除机厅别名",
            detail="格式：删除机厅别名 <店名/序号> <别名/序号>",
        ),
        CommandSpec(
            matcher=get_alias,
            name="机厅别名",
            brief="展示机厅别名",
            detail="格式：机厅别名 <店名/序号>",
        ),
        CommandSpec(
            matcher=add_map,
            name="添加机厅地图",
            scope="群管",
            brief="添加机厅地图网址",
            detail="格式：添加机厅地图 <机厅名称/序号> <网址>",
        ),
        CommandSpec(
            matcher=delete_map,
            name="删除机厅地图",
            aliases=("移除机厅地图",),
            scope="群管",
            brief="移除机厅地图信息",
            detail="格式：删除机厅地图 <机厅名称/序号> <网址/序号>",
        ),
        CommandSpec(
            matcher=get_map,
            name="机厅地图",
            aliases=("音游地图",),
            brief="展示机厅音游地图",
            detail="格式：机厅地图 <机厅名称/序号>",
        ),
        # 人数域
        CommandSpec(
            matcher=updated_list,
            name="机厅人数",
            aliases=("mai", "jtj", "机厅几人"),
            scope="仅群聊",
            brief="展示当日已更新的所有机厅人数列表",
        ),
        # 排卡队列域
        CommandSpec(
            matcher=go_on,
            name="上机",
            scope="仅群聊",
            brief="将当前第一位排队者移至最后",
        ),
        CommandSpec(
            matcher=get_in,
            name="排卡",
            scope="仅群聊",
            brief="加入排队队列",
        ),
        CommandSpec(
            matcher=get_run,
            name="退勤",
            scope="仅群聊",
            brief="从排队队列中退出",
        ),
        CommandSpec(
            matcher=show_list,
            name="排卡现状",
            scope="仅群聊",
            brief="展示当前排队队列",
        ),
        CommandSpec(
            matcher=put_off,
            name="延后",
            scope="仅群聊",
            brief="将自己延后一位",
        ),
        CommandSpec(
            matcher=shut_down,
            name="闭店",
            scope="群管 · 仅群聊",
            brief="清空排队队列",
        ),
    ],
)

help_registry.declare_guide(
    Guide(
        key="排卡上手",
        title="排卡上手",
        aliases=("机厅上手",),
        intro=(
            "在本群开通机厅人数与排卡功能并日常使用："
            "名单开通 → 添加机厅 → 人数上报 / 线上排卡。"
        ),
        prerequisites=(
            "管理类步骤需群管理员执行；本插件替代主插件内置 awmc.arcade，"
            "二者指令同名只能启用其一（awmc_disabled_plugins 停用内置）。"
        ),
        steps=[
            GuideStep(text="将本群加入名单（群管）：", commands=("添加群聊",)),
            GuideStep(
                text=(
                    "添加本群机厅（群管；重名可 @地区 过滤，"
                    "之后全部指令可用序号指代）："
                ),
                commands=("添加机厅", "机厅列表"),
            ),
            GuideStep(
                text="日常人数：上报 <机厅名>+1 / =3，查询 <机厅名>几，总览——",
                commands=("机厅人数",),
            ),
            GuideStep(
                text="线上排卡：",
                commands=("排卡", "排卡现状", "闭店"),
            ),
        ],
        source=_ARCADE_PLUGIN,
    )
)
