import asyncio
import contextlib
from typing import Set, Dict, List, Tuple, ClassVar, Optional

from sqlmodel import Field, col
from sqlalchemy import Table, UniqueConstraint, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.logger import logger
from gsuid_core.models import MessageReceive
from gsuid_core.webconsole.mount_app import PageSchema, GsAdminModel, site
from gsuid_core.utils.database.base_models import BaseBotIDModel, with_session, with_read_session

FLUSH_SECONDS = 5.0
EXIT_META = "meta-user_exit_group"
JOIN_META = "meta-user_join_group"

MemberKey = Tuple[str, str, str]


async def ensure_table() -> None:
    import gsuid_core.utils.database.base_models as bm

    await bm.init_database()
    async with bm.engine.begin() as conn:
        await conn.run_sync(QQBotUIDUser.__table__.create, checkfirst=True)


class QQBotUIDUser(BaseBotIDModel, table=True):
    """一个群里的一个 openid 一行；私聊的 group_openid 为空。"""

    __tablename__: ClassVar[str] = "QQBotUID_User"
    __table__: ClassVar[Table]
    __table_args__ = (
        UniqueConstraint("bot_id", "group_openid", "user_openid", name="record_qqbotuid_user"),
        {"extend_existing": True},
    )

    group_openid: str = Field(default="", title="群 OpenID，私聊为空")
    user_openid: str = Field(title="用户 OpenID")

    @classmethod
    @with_session
    async def add_many(cls, session: AsyncSession, keys: Set[MemberKey]) -> None:
        """批量补人"""
        for bot_id, group_openid, users in _bucket(keys):
            existing = await cls._existing(session, bot_id, group_openid, users)
            for user_openid in users - existing:
                session.add(cls(bot_id=bot_id, group_openid=group_openid, user_openid=user_openid))

    @classmethod
    @with_session
    async def remove_many(cls, session: AsyncSession, keys: Set[MemberKey]) -> None:
        """批量删人"""
        for bot_id, group_openid, users in _bucket(keys):
            await session.execute(
                delete(cls).where(
                    col(cls.bot_id) == bot_id,
                    col(cls.group_openid) == group_openid,
                    col(cls.user_openid).in_(users),
                )
            )

    @classmethod
    async def _existing(
        cls,
        session: AsyncSession,
        bot_id: str,
        group_openid: str,
        users: Set[str],
    ) -> Set[str]:
        stmt = select(col(cls.user_openid)).where(
            col(cls.bot_id) == bot_id,
            col(cls.group_openid) == group_openid,
            col(cls.user_openid).in_(users),
        )
        return {str(row) for row in (await session.execute(stmt)).scalars().all()}

    @classmethod
    @with_read_session
    async def list_members(
        cls,
        session: AsyncSession,
        bot_id: str,
        group_openid: str,
    ) -> List[str]:
        """某个群的成员 openid。"""
        stmt = (
            select(col(cls.user_openid))
            .where(
                col(cls.bot_id) == bot_id,
                col(cls.group_openid) == group_openid,
            )
            .order_by(col(cls.id))
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [str(row) for row in rows]


def _bucket(keys: Set[MemberKey]) -> List[Tuple[str, str, Set[str]]]:
    """按 (bot_id, group_openid) 分组，一组一次查询 / 一次删除。"""
    buckets: Dict[Tuple[str, str], Set[str]] = {}
    for bot_id, group_openid, user_openid in keys:
        buckets.setdefault((bot_id, group_openid), set()).add(user_openid)
    return [(bot_id, group_openid, users) for (bot_id, group_openid), users in buckets.items()]


class UserRecorder:
    """内存里攒增删，定时落库。"""

    def __init__(self, flush_seconds: float = FLUSH_SECONDS) -> None:
        self.flush_seconds = flush_seconds
        self._pending: Dict[MemberKey, bool] = {}
        self._task: Optional["asyncio.Task[None]"] = None

    def observe(self, receive: MessageReceive) -> None:
        head = receive.content[0].type if receive.content else None
        if head == JOIN_META:
            present = True
        elif head == EXIT_META:
            present = False
        elif head and head.startswith("meta-"):
            return
        else:
            present = True
        user_id = str(receive.user_id or "")
        if not user_id:
            return
        key = (receive.bot_id, str(receive.group_id or ""), user_id)
        self._pending[key] = present
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="QQBotUID.user_recorder")

    async def _loop(self) -> None:
        while self._pending:
            await asyncio.sleep(self.flush_seconds)
            await self.flush()

    async def flush(self) -> None:
        batch, self._pending = self._pending, {}
        if not batch:
            return
        adds = {key for key, present in batch.items() if present}
        removals = {key for key, present in batch.items() if not present}
        try:
            if removals:
                await QQBotUIDUser.remove_many(removals)
            if adds:
                await QQBotUIDUser.add_many(adds)
        except Exception as e:
            logger.warning(f"[QQBotUID] 群成员落库失败（{len(batch)} 条）: {e}")

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.flush()


@site.register_admin
class QQBotUIDUserAdmin(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(
        label="QQBotUID 用户记录",
        icon="fa fa-user",
    )  # type: ignore
    model = QQBotUIDUser

