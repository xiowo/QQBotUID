import asyncio
from typing import Set, Dict, List, Tuple, ClassVar, Iterable, Optional

from sqlmodel import Field, col
from sqlalchemy import Table, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.logger import logger
from gsuid_core.webconsole.mount_app import PageSchema, GsAdminModel, site
from gsuid_core.utils.database.base_models import BaseBotIDModel, with_session, with_read_session


async def ensure_table() -> None:
    import gsuid_core.utils.database.base_models as bm

    await bm.init_database()
    async with bm.engine.begin() as conn:
        await conn.run_sync(QQBotUIDUnionOpenID.__table__.create, checkfirst=True)


def _clean(bot_id: str, user_openid: str, union_openid: str) -> Optional[Tuple[str, str, str]]:
    """union_openid 为空不记录。"""
    values = (
        str(bot_id or "").strip(),
        str(user_openid or "").strip(),
        str(union_openid or "").strip(),
    )
    return values if all(values) else None


class QQBotUIDUnionOpenID(BaseBotIDModel, table=True):
    __tablename__: ClassVar[str] = "QQBotUID_Union_OpenID"
    __table__: ClassVar[Table]
    __table_args__ = (
        UniqueConstraint("bot_id", "user_openid", name="record_qqbotuid_union_openid"),
        {"extend_existing": True},
    )

    user_openid: str = Field(title="用户 OpenID")
    union_openid: str = Field(title="用户 Union OpenID", index=True)

    @classmethod
    @with_session
    async def upsert(cls, session: AsyncSession, bot_id: str, user_openid: str, union_openid: str) -> bool:
        """写入 / 更新一条对应关系，返回是否有变化。"""
        cleaned = _clean(bot_id, user_openid, union_openid)
        if cleaned is None:
            return False
        bot_id, user_openid, union_openid = cleaned
        row = await cls._fetch(session, bot_id, user_openid)
        if row is None:
            session.add(cls(bot_id=bot_id, user_openid=user_openid, union_openid=union_openid))
            return True
        if row.union_openid == union_openid:
            return False
        row.union_openid = union_openid
        session.add(row)
        return True

    @classmethod
    async def _fetch(
        cls,
        session: AsyncSession,
        bot_id: str,
        user_openid: str,
    ) -> Optional["QQBotUIDUnionOpenID"]:
        stmt = select(cls).where(col(cls.bot_id) == bot_id, col(cls.user_openid) == user_openid)
        return (await session.execute(stmt)).scalars().first()

    @classmethod
    @with_read_session
    async def select_union_openid(
        cls,
        session: AsyncSession,
        bot_id: str,
        user_openid: str,
    ) -> str:
        """openid → union_openid，没记过返回空串。"""
        row = await cls._fetch(session, bot_id, str(user_openid or "").strip())
        return row.union_openid if row is not None else ""

    @classmethod
    @with_read_session
    async def select_user_openids(
        cls,
        session: AsyncSession,
        bot_id: str,
        union_openid: str,
    ) -> List[str]:
        """同一个人的全部 openid（群 / 私聊各一条）。"""
        stmt = (
            select(col(cls.user_openid))
            .where(col(cls.bot_id) == bot_id, col(cls.union_openid) == union_openid)
            .order_by(col(cls.id))
        )
        return [str(row) for row in (await session.execute(stmt)).scalars().all()]

    @classmethod
    @with_read_session
    async def select_union_openid_map(
        cls,
        session: AsyncSession,
        bot_id: str,
        user_openids: Iterable[str],
    ) -> Dict[str, str]:
        """批量 openid → union_openid，没记过的键不出现在结果里。"""
        targets = {str(item).strip() for item in user_openids if str(item).strip()}
        if not targets:
            return {}
        stmt = select(col(cls.user_openid), col(cls.union_openid)).where(
            col(cls.bot_id) == bot_id,
            col(cls.user_openid).in_(targets),
        )
        rows = (await session.execute(stmt)).all()
        return {str(user_openid): str(union_openid) for user_openid, union_openid in rows}


async def record(bot_id: str, user_openid: str, union_openid: str) -> bool:
    """记一条对应关系，返回是否有变化。没有 union_openid 直接跳过。"""
    return await QQBotUIDUnionOpenID.upsert(bot_id, user_openid, union_openid)


async def get_union_openid(bot_id: str, user_openid: str) -> str:
    """openid → union_openid，没记过返回空串。"""
    return await QQBotUIDUnionOpenID.select_union_openid(bot_id, user_openid)


async def get_user_openids(bot_id: str, union_openid: str) -> List[str]:
    """union_openid → 该用户出现过的全部 openid。"""
    return await QQBotUIDUnionOpenID.select_user_openids(bot_id, union_openid)


async def get_union_openid_map(bot_id: str, user_openids: Iterable[str]) -> Dict[str, str]:
    """批量转换，没记过的 openid 不出现在结果里。"""
    return await QQBotUIDUnionOpenID.select_union_openid_map(bot_id, user_openids)


class UnionRecorder:
    """入站事件里的对应关系，去重后异步落库。"""

    def __init__(self) -> None:
        self._seen: Dict[Tuple[str, str], str] = {}
        self._tasks: Set["asyncio.Task[None]"] = set()

    def observe(self, bot_id: str, user_openid: str, union_openid: str) -> None:
        cleaned = _clean(bot_id, user_openid, union_openid)
        if cleaned is None:
            return
        bot_id, user_openid, union_openid = cleaned
        key = (bot_id, user_openid)
        if self._seen.get(key) == union_openid:
            return
        self._seen[key] = union_openid
        task = asyncio.create_task(self._write(key, union_openid), name="QQBotUID.union_openid")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _write(self, key: Tuple[str, str], union_openid: str) -> None:
        try:
            await QQBotUIDUnionOpenID.upsert(key[0], key[1], union_openid)
        except Exception as e:
            # 写失败就丢掉缓存，下次这条消息再落一次
            self._seen.pop(key, None)
            logger.warning(f"[QQBotUID] union_openid 落库失败（{key[1]}）: {e}")


@site.register_admin
class QQBotUIDUnionOpenIDAdmin(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(
        label="QQBotUID openid 映射",
        icon="fa fa-id-card",
    )  # type: ignore
    model = QQBotUIDUnionOpenID
