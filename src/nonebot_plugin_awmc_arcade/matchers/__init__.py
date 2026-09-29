"""指令入口包：按域拆分的 matcher 模块（装配层 import 本包即完成注册）。

原单文件 matchers.py（千行巨石）按域拆分：每个域模块自行定义 matcher 与
handler（import 即注册），本包 ``__init__`` 汇总导出全部 matcher 与
HELP_TEXT（nonebug 测试按包属性取用）。本文件只做装配式 re-export，
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

from .help import HELP_TEXT, arcade_help
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
    "HELP_TEXT",
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
