"""本插件自有存储（localstore 数据目录 ``awmc_arcade.db``）。

上游 mai_arcade 以进程内全局 dict + JSON 文件存数据（每群一份机厅表），
本迁移改为与 awmc 生态一致的 SQLite（sqlmodel + aiosqlite，全异步）。
领域模型保持上游的「每群独立机厅表」范式：机厅、别名、地图、排队队列、
当日人数日志均挂在群维度下，跨群互不可见。

表速览：
- ``group_cfg``：已开通排卡的群 + 静默模式开关（上游 block_group 为内存
  set，重启即丢；此处一并持久化修复）；
- ``arcade_entry``：群内机厅（名字唯一），缓存 Nearcade 店铺 id/链接与
  maimai 机台数；
- ``alias_entry``：机厅别名（群内唯一——上游仅机厅内唯一，存在跨机厅
  歧义，此处收紧）；
- ``map_entry``：机厅音游地图网址（按添加顺序）；
- ``queue_item``：线上排卡队列（群内一人只能排一个机厅，群名片展示）；
- ``count_log``：当日人数变更流水，当前人数 = 当日 delta 之和；
- ``meta_row``：KV（日清标记等）。
"""

from pathlib import Path
from datetime import datetime

from pydantic import NaiveDatetime
from sqlmodel import Field, SQLModel, col, delete, select
from sqlalchemy import UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from nonebot_plugin_localstore import get_data_dir
from sqlmodel.ext.asyncio.session import AsyncSession

_engine: AsyncEngine | None = None
_db_file: Path | None = None


def db_file():
    """SQLite 库文件路径（localstore 插件数据目录；测试可重定向）。"""
    return (
        _db_file
        if _db_file is not None
        else get_data_dir("nonebot_plugin_awmc_arcade") / "awmc_arcade.db"
    )


async def set_db_file(path: Path | None) -> None:
    """重定向库文件并重置引擎（测试隔离用，生产勿调）。

    旧引擎先 dispose 归还连接池，否则池内连接被 GC 回收时触发
    ResourceWarning（aiosqlite 连接未显式关闭）。
    """
    global _db_file, _engine
    if _engine is not None:
        await _engine.dispose()
    _db_file = path
    _engine = None


def get_engine() -> AsyncEngine:
    """懒创建的异步引擎（与 awmc 生态其余插件同款模式）。"""
    global _engine
    if _engine is None:
        _engine = create_async_engine(f"sqlite+aiosqlite:///{db_file()}")
    return _engine


async def init_store() -> None:
    """建表（create_all 起步）。"""
    async with get_engine().begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


class GroupCfg(SQLModel, table=True):
    """已开通排卡的群；行存在 = 开通。"""

    __tablename__ = "group_cfg"  # type: ignore[reportGeneralTypeIssues]

    group_id: int = Field(primary_key=True)
    silent: bool = False  # 静默监听模式：人数上报不再刷确认回复
    created_at: NaiveDatetime = Field(default_factory=datetime.now)


class ArcadeEntry(SQLModel, table=True):
    """群内机厅条目（名字在群内唯一）。"""

    __tablename__ = "arcade_entry"  # type: ignore[reportGeneralTypeIssues]
    __table_args__ = (UniqueConstraint("group_id", "name"),)

    id: int | None = Field(default=None, primary_key=True)
    group_id: int = Field(index=True)
    name: str
    # Nearcade 店铺信息（经搜索选择添加时写入；人数云同步依赖 shop_id）
    nearcade_source: str | None = None
    nearcade_shop_id: str | None = None
    nearcade_url: str | None = None
    # maimai DX 机台数（云端同步时刷新；估算等待时间的基数）
    coutnum: int = 1
    created_by: str | None = None
    created_at: NaiveDatetime = Field(default_factory=datetime.now)

    @property
    def key(self) -> int:
        """主键窄化：从库中读出或落库刷新后的条目 id 恒非空。"""
        assert self.id is not None
        return self.id


class AliasEntry(SQLModel, table=True):
    """机厅别名（群内唯一，指向唯一机厅，保证解析无歧义）。"""

    __tablename__ = "alias_entry"  # type: ignore[reportGeneralTypeIssues]
    __table_args__ = (UniqueConstraint("group_id", "alias"),)

    id: int | None = Field(default=None, primary_key=True)
    group_id: int = Field(index=True)
    arcade_id: int = Field(index=True)
    alias: str


class MapEntry(SQLModel, table=True):
    """机厅音游地图网址（按添加顺序，支持序号操作）。"""

    __tablename__ = "map_entry"  # type: ignore[reportGeneralTypeIssues]

    id: int | None = Field(default=None, primary_key=True)
    group_id: int = Field(index=True)
    arcade_id: int = Field(index=True)
    url: str


class QueueItem(SQLModel, table=True):
    """排卡队列成员；joined_at 即队列位次（群内一人只能排一个机厅）。"""

    __tablename__ = "queue_item"  # type: ignore[reportGeneralTypeIssues]
    __table_args__ = (UniqueConstraint("group_id", "user_id"),)

    id: int | None = Field(default=None, primary_key=True)
    group_id: int = Field(index=True)
    arcade_id: int = Field(index=True)
    user_id: str
    nickname: str  # 入队时的昵称（展示用，静态快照）
    joined_at: NaiveDatetime = Field(default_factory=datetime.now)


class CountLog(SQLModel, table=True):
    """当日人数变更流水；当前人数 = 当日 delta 之和，行时间即最后更新时间。"""

    __tablename__ = "count_log"  # type: ignore[reportGeneralTypeIssues]

    id: int | None = Field(default=None, primary_key=True)
    group_id: int = Field(index=True)
    arcade_id: int = Field(index=True)
    delta: int  # 相对变更量；绝对重置时为设置后的总值
    updated_by: str | None = None
    updated_at: NaiveDatetime = Field(default_factory=datetime.now)


class MetaRow(SQLModel, table=True):
    """KV 杂项（日清标记等）。"""

    __tablename__ = "meta_row"  # type: ignore[reportGeneralTypeIssues]

    key: str = Field(primary_key=True)
    value: str


class ArcadeStore:
    """机厅插件全部数据读写（查询与写入同会话完成，全异步）。"""

    # ---- 群 ----

    @staticmethod
    async def get_group(group_id: int) -> GroupCfg | None:
        async with AsyncSession(get_engine()) as session:
            return (
                await session.exec(
                    select(GroupCfg).where(GroupCfg.group_id == group_id)
                )
            ).first()

    @staticmethod
    async def add_group(group_id: int) -> bool:
        """开通群，返回是否新增（已开通返回 False）。"""
        async with AsyncSession(get_engine()) as session:
            if (
                await session.exec(
                    select(GroupCfg).where(GroupCfg.group_id == group_id)
                )
            ).first():
                return False
            session.add(GroupCfg(group_id=group_id))
            await session.commit()
            return True

    @staticmethod
    async def remove_group(group_id: int) -> bool:
        """关闭群并级联清掉该群全部机厅数据。"""
        async with AsyncSession(get_engine()) as session:
            if not (
                await session.exec(
                    select(GroupCfg).where(GroupCfg.group_id == group_id)
                )
            ).first():
                return False
            # 类型并集循环变量取不到列表达式（col 需要具体模型），逐表展开
            await session.exec(
                delete(AliasEntry).where(col(AliasEntry.group_id) == group_id)
            )
            await session.exec(
                delete(MapEntry).where(col(MapEntry.group_id) == group_id)
            )
            await session.exec(
                delete(QueueItem).where(col(QueueItem.group_id) == group_id)
            )
            await session.exec(
                delete(CountLog).where(col(CountLog.group_id) == group_id)
            )
            await session.exec(
                delete(ArcadeEntry).where(col(ArcadeEntry.group_id) == group_id)
            )
            await session.exec(
                delete(GroupCfg).where(col(GroupCfg.group_id) == group_id)
            )
            await session.commit()
            return True

    @staticmethod
    async def set_silent(group_id: int, silent: bool) -> bool:
        """设置静默模式，返回群是否存在。"""
        async with AsyncSession(get_engine()) as session:
            cfg = (
                await session.exec(
                    select(GroupCfg).where(GroupCfg.group_id == group_id)
                )
            ).first()
            if cfg is None:
                return False
            cfg.silent = silent
            await session.commit()
            return True

    # ---- 机厅 ----

    @staticmethod
    async def list_arcades(group_id: int) -> list[ArcadeEntry]:
        """群内机厅列表（按添加顺序，序号操作以此序为准）。"""
        async with AsyncSession(get_engine()) as session:
            return list(
                await session.exec(
                    select(ArcadeEntry)
                    .where(ArcadeEntry.group_id == group_id)
                    .order_by(col(ArcadeEntry.id))
                )
            )

    @staticmethod
    async def get_arcade(group_id: int, arcade_id: int) -> ArcadeEntry | None:
        async with AsyncSession(get_engine()) as session:
            return (
                await session.exec(
                    select(ArcadeEntry).where(
                        ArcadeEntry.group_id == group_id,
                        ArcadeEntry.id == arcade_id,
                    )
                )
            ).first()

    @staticmethod
    async def get_arcade_by_name(group_id: int, name: str) -> ArcadeEntry | None:
        async with AsyncSession(get_engine()) as session:
            return (
                await session.exec(
                    select(ArcadeEntry).where(
                        ArcadeEntry.group_id == group_id, ArcadeEntry.name == name
                    )
                )
            ).first()

    @staticmethod
    async def find_arcade_by_alias(group_id: int, alias: str) -> ArcadeEntry | None:
        async with AsyncSession(get_engine()) as session:
            row = (
                await session.exec(
                    select(AliasEntry).where(
                        AliasEntry.group_id == group_id,
                        AliasEntry.alias == alias,
                    )
                )
            ).first()
            if row is None:
                return None
            return (
                await session.exec(
                    select(ArcadeEntry).where(ArcadeEntry.id == row.arcade_id)
                )
            ).first()

    @staticmethod
    async def add_arcade(
        group_id: int,
        name: str,
        *,
        created_by: str | None = None,
        source: str | None = None,
        shop_id: str | None = None,
        shop_url: str | None = None,
    ) -> ArcadeEntry | None:
        """新增机厅；群内重名返回 None。"""
        async with AsyncSession(get_engine()) as session:
            if (
                await session.exec(
                    select(ArcadeEntry).where(
                        ArcadeEntry.group_id == group_id, ArcadeEntry.name == name
                    )
                )
            ).first():
                return None
            entry = ArcadeEntry(
                group_id=group_id,
                name=name,
                created_by=created_by,
                nearcade_source=source,
                nearcade_shop_id=shop_id,
                nearcade_url=shop_url,
            )
            session.add(entry)
            await session.commit()
            await session.refresh(entry)
            return entry

    @staticmethod
    async def delete_arcade(group_id: int, arcade_id: int) -> None:
        """删除机厅并级联清理别名/地图/队列/当日流水。"""
        async with AsyncSession(get_engine()) as session:
            # 类型并集循环变量取不到列表达式（col 需要具体模型），逐表展开
            await session.exec(
                delete(AliasEntry).where(
                    col(AliasEntry.group_id) == group_id,
                    col(AliasEntry.arcade_id) == arcade_id,
                )
            )
            await session.exec(
                delete(MapEntry).where(
                    col(MapEntry.group_id) == group_id,
                    col(MapEntry.arcade_id) == arcade_id,
                )
            )
            await session.exec(
                delete(QueueItem).where(
                    col(QueueItem.group_id) == group_id,
                    col(QueueItem.arcade_id) == arcade_id,
                )
            )
            await session.exec(
                delete(CountLog).where(
                    col(CountLog.group_id) == group_id,
                    col(CountLog.arcade_id) == arcade_id,
                )
            )
            await session.exec(
                delete(ArcadeEntry).where(
                    col(ArcadeEntry.group_id) == group_id,
                    col(ArcadeEntry.id) == arcade_id,
                )
            )
            await session.commit()

    @staticmethod
    async def update_nearcade_info(
        arcade_id: int,
        *,
        coutnum: int | None = None,
        shop_id: str | None = None,
        source: str | None = None,
        url: str | None = None,
    ) -> None:
        """回填 Nearcade 店铺信息 / 机台数（云同步过程中刷新）。"""
        async with AsyncSession(get_engine()) as session:
            entry = await session.get(ArcadeEntry, arcade_id)
            if entry is None:
                return
            if coutnum is not None:
                entry.coutnum = coutnum
            if shop_id is not None:
                entry.nearcade_shop_id = shop_id
            if source is not None:
                entry.nearcade_source = source
            if url is not None:
                entry.nearcade_url = url
            await session.commit()

    # ---- 别名 ----

    @staticmethod
    async def list_aliases(group_id: int, arcade_id: int) -> list[str]:
        async with AsyncSession(get_engine()) as session:
            rows = await session.exec(
                select(AliasEntry)
                .where(
                    AliasEntry.group_id == group_id,
                    AliasEntry.arcade_id == arcade_id,
                )
                .order_by(col(AliasEntry.id))
            )
            return [r.alias for r in rows]

    @staticmethod
    async def add_alias(group_id: int, arcade_id: int, alias: str) -> bool:
        """添加别名；群内重复返回 False。"""
        async with AsyncSession(get_engine()) as session:
            if (
                await session.exec(
                    select(AliasEntry).where(
                        AliasEntry.group_id == group_id,
                        AliasEntry.alias == alias,
                    )
                )
            ).first():
                return False
            session.add(AliasEntry(group_id=group_id, arcade_id=arcade_id, alias=alias))
            await session.commit()
            return True

    @staticmethod
    async def remove_alias(group_id: int, arcade_id: int, alias: str) -> bool:
        async with AsyncSession(get_engine()) as session:
            row = (
                await session.exec(
                    select(AliasEntry).where(
                        AliasEntry.group_id == group_id,
                        AliasEntry.arcade_id == arcade_id,
                        AliasEntry.alias == alias,
                    )
                )
            ).first()
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    # ---- 地图 ----

    @staticmethod
    async def list_maps(group_id: int, arcade_id: int) -> list[str]:
        async with AsyncSession(get_engine()) as session:
            rows = await session.exec(
                select(MapEntry)
                .where(
                    MapEntry.group_id == group_id,
                    MapEntry.arcade_id == arcade_id,
                )
                .order_by(col(MapEntry.id))
            )
            return [r.url for r in rows]

    @staticmethod
    async def add_map(group_id: int, arcade_id: int, url: str) -> bool:
        """添加地图网址；重复返回 False。"""
        async with AsyncSession(get_engine()) as session:
            if (
                await session.exec(
                    select(MapEntry).where(
                        MapEntry.group_id == group_id,
                        MapEntry.arcade_id == arcade_id,
                        MapEntry.url == url,
                    )
                )
            ).first():
                return False
            session.add(MapEntry(group_id=group_id, arcade_id=arcade_id, url=url))
            await session.commit()
            return True

    @staticmethod
    async def remove_map(group_id: int, arcade_id: int, url: str) -> bool:
        async with AsyncSession(get_engine()) as session:
            row = (
                await session.exec(
                    select(MapEntry).where(
                        MapEntry.group_id == group_id,
                        MapEntry.arcade_id == arcade_id,
                        MapEntry.url == url,
                    )
                )
            ).first()
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    # ---- 排卡 ----

    @staticmethod
    async def list_queue(group_id: int, arcade_id: int) -> list[QueueItem]:
        async with AsyncSession(get_engine()) as session:
            return list(
                await session.exec(
                    select(QueueItem)
                    .where(
                        QueueItem.group_id == group_id,
                        QueueItem.arcade_id == arcade_id,
                    )
                    .order_by(col(QueueItem.joined_at), col(QueueItem.id))
                )
            )

    @staticmethod
    async def get_queue_position(
        group_id: int, user_id: str
    ) -> tuple[ArcadeEntry, QueueItem] | None:
        """查某用户在群内的排队位置（含所在机厅）。"""
        async with AsyncSession(get_engine()) as session:
            item = (
                await session.exec(
                    select(QueueItem).where(
                        QueueItem.group_id == group_id,
                        QueueItem.user_id == user_id,
                    )
                )
            ).first()
            if item is None:
                return None
            entry = (
                await session.exec(
                    select(ArcadeEntry).where(ArcadeEntry.id == item.arcade_id)
                )
            ).first()
            if entry is None:  # 机厅被删时队列行随级联清理，理论不可达
                return None
            return entry, item

    @staticmethod
    async def join_queue(
        group_id: int, arcade_id: int, user_id: str, nickname: str
    ) -> QueueItem:
        """入队（调用方保证该用户当前未在群内排卡）。"""
        async with AsyncSession(get_engine()) as session:
            item = QueueItem(
                group_id=group_id,
                arcade_id=arcade_id,
                user_id=user_id,
                nickname=nickname,
            )
            session.add(item)
            await session.commit()
            await session.refresh(item)
            return item

    @staticmethod
    async def leave_queue(group_id: int, user_id: str) -> QueueItem | None:
        async with AsyncSession(get_engine()) as session:
            item = (
                await session.exec(
                    select(QueueItem).where(
                        QueueItem.group_id == group_id,
                        QueueItem.user_id == user_id,
                    )
                )
            ).first()
            if item is None:
                return None
            await session.delete(item)
            await session.commit()
            return item

    @staticmethod
    async def rotate_queue(group_id: int, arcade_id: int) -> None:
        """上机：队首移到队尾（重排 joined_at 保证次序稳定）。"""
        async with AsyncSession(get_engine()) as session:
            items = (
                await session.exec(
                    select(QueueItem)
                    .where(
                        QueueItem.group_id == group_id,
                        QueueItem.arcade_id == arcade_id,
                    )
                    .order_by(col(QueueItem.joined_at), col(QueueItem.id))
                )
            ).all()
            now = datetime.now()
            for i, item in enumerate([*items[1:], *items[:1]]):
                item.joined_at = now.replace(microsecond=i)
            await session.commit()

    @staticmethod
    async def delay_in_queue(group_id: int, user_id: str) -> QueueItem | None:
        """延后：与下一位交换位次。已在队尾返回 None。"""
        async with AsyncSession(get_engine()) as session:
            item = (
                await session.exec(
                    select(QueueItem).where(
                        QueueItem.group_id == group_id,
                        QueueItem.user_id == user_id,
                    )
                )
            ).first()
            if item is None:
                return None
            nxt = (
                await session.exec(
                    select(QueueItem)
                    .where(
                        QueueItem.group_id == group_id,
                        QueueItem.arcade_id == item.arcade_id,
                        col(QueueItem.joined_at) > item.joined_at,
                    )
                    .order_by(col(QueueItem.joined_at), col(QueueItem.id))
                    .limit(1)
                )
            ).first()
            if nxt is None:
                return None
            item.joined_at, nxt.joined_at = nxt.joined_at, item.joined_at
            await session.commit()
            return item

    @staticmethod
    async def clear_queue(group_id: int, arcade_id: int) -> int:
        """清空某机厅队列（闭店），返回清掉的人数。"""
        async with AsyncSession(get_engine()) as session:
            items = (
                await session.exec(
                    select(QueueItem).where(
                        QueueItem.group_id == group_id,
                        QueueItem.arcade_id == arcade_id,
                    )
                )
            ).all()
            for item in items:
                await session.delete(item)
            await session.commit()
            return len(items)

    # ---- 人数 ----

    @staticmethod
    async def current_count(group_id: int, arcade_id: int) -> int:
        async with AsyncSession(get_engine()) as session:
            rows = await session.exec(
                select(CountLog).where(
                    CountLog.group_id == group_id,
                    CountLog.arcade_id == arcade_id,
                )
            )
            return sum(r.delta for r in rows)

    @staticmethod
    async def last_count_update(group_id: int, arcade_id: int) -> CountLog | None:
        async with AsyncSession(get_engine()) as session:
            return (
                await session.exec(
                    select(CountLog)
                    .where(
                        CountLog.group_id == group_id,
                        CountLog.arcade_id == arcade_id,
                    )
                    .order_by(col(CountLog.id).desc())
                    .limit(1)
                )
            ).first()

    @staticmethod
    async def add_count_log(
        group_id: int, arcade_id: int, delta: int, updated_by: str | None
    ) -> None:
        async with AsyncSession(get_engine()) as session:
            session.add(
                CountLog(
                    group_id=group_id,
                    arcade_id=arcade_id,
                    delta=delta,
                    updated_by=updated_by,
                )
            )
            await session.commit()

    @staticmethod
    async def reset_count(
        group_id: int, arcade_id: int, value: int, updated_by: str | None
    ) -> None:
        """清空当日流水后记一行绝对值（云端覆盖/显式重置用）。"""
        async with AsyncSession(get_engine()) as session:
            await session.exec(
                delete(CountLog).where(
                    col(CountLog.group_id) == group_id,
                    col(CountLog.arcade_id) == arcade_id,
                )
            )
            session.add(
                CountLog(
                    group_id=group_id,
                    arcade_id=arcade_id,
                    delta=value,
                    updated_by=updated_by,
                )
            )
            await session.commit()

    @staticmethod
    async def clear_day_logs() -> int:
        """清零全部当日人数流水（每日 0 点 / 补偿执行），返回清掉的行数。"""
        async with AsyncSession(get_engine()) as session:
            rows = (await session.exec(select(CountLog))).all()
            await session.exec(delete(CountLog))
            await session.commit()
            return len(rows)

    # ---- KV ----

    @staticmethod
    async def get_meta(key: str) -> str | None:
        async with AsyncSession(get_engine()) as session:
            if row := await session.get(MetaRow, key):
                return row.value
            return None

    @staticmethod
    async def set_meta(key: str, value: str) -> None:
        async with AsyncSession(get_engine()) as session:
            if row := await session.get(MetaRow, key):
                row.value = value
            else:
                session.add(MetaRow(key=key, value=value))
            await session.commit()


store = ArcadeStore()
