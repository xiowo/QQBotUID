import time
from typing import ClassVar, Optional

from sqlmodel import Field, col
from sqlalchemy import Table, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.utils.database.base_models import BaseBotIDModel, with_session, with_read_session

CACHE_TTL = 24 * 3600
RETRY_TTL = 1800


async def ensure_table() -> None:
    import gsuid_core.utils.database.base_models as bm

    await bm.init_database()
    async with bm.engine.begin() as conn:
        await conn.run_sync(QQBotUIDGroupInfo.__table__.create, checkfirst=True)


class QQBotUIDGroupInfo(BaseBotIDModel, table=True):
    """群名 + 成员数 + 更新时间"""

    __tablename__: ClassVar[str] = "QQBotUID_Group_Info"
    __table__: ClassVar[Table]
    __table_args__ = (
        UniqueConstraint("bot_id", "group_openid", name="record_qqbotuid_group_info"),
        {"extend_existing": True},
    )

    group_openid: str = Field(title="群 OpenID")
    group_name: str = Field(default="", title="群名")
    member_num: int = Field(default=0, title="群成员数")
    updated_at: float = Field(default=0.0, title="更新时间")

    @classmethod
    async def get_fresh(cls, bot_id: str, group_openid: str) -> Optional["QQBotUIDGroupInfo"]:
        """取没过期的缓存行，没有或过期返回 None。"""
        row = await cls._select_row(bot_id, group_openid)
        if row is None:
            return None
        ttl = CACHE_TTL if row.group_name else RETRY_TTL
        if time.time() - row.updated_at > ttl:
            return None
        return row

    @classmethod
    @with_session
    async def upsert(
        cls,
        session: AsyncSession,
        bot_id: str,
        group_openid: str,
        group_name: str,
        member_num: int = 0,
    ) -> None:
        row = await cls._select_row(bot_id, group_openid)
        if row is None:
            session.add(
                cls(
                    bot_id=bot_id,
                    group_openid=group_openid,
                    group_name=group_name,
                    member_num=member_num,
                    updated_at=time.time(),
                )
            )
            return
        row.group_name = group_name
        row.member_num = member_num
        row.updated_at = time.time()
        session.add(row)

    @classmethod
    @with_read_session
    async def _select_row(
        cls,
        session: AsyncSession,
        bot_id: str,
        group_openid: str,
    ) -> Optional["QQBotUIDGroupInfo"]:
        stmt = select(cls).where(col(cls.bot_id) == bot_id, col(cls.group_openid) == group_openid)
        return (await session.execute(stmt)).scalars().first()
