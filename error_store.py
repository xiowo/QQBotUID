import time
from typing import Literal, ClassVar, TypedDict
from datetime import datetime

from pydantic import BaseModel
from sqlmodel import Field, col
from sqlalchemy import Text, Table, Double, func, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from gsuid_core.utils.database.base_models import BaseIDModel, with_session, with_read_session

RETAIN_DAYS = 30


class ErrorBot(BaseModel):
    appid: str
    bot_name: str = ""
    avatar: str = ""


class ErrorType(BaseModel):
    code: str
    err_code: str
    message: str
    count: int
    last_seen: float


class QQBotUIDAPIError(BaseIDModel, table=True):
    __tablename__: ClassVar[str] = "QQBotUID_API_Error"
    __table__: ClassVar[Table]
    __table_args__ = {"extend_existing": True}

    appid: str = Field(index=True)
    bot_name: str = ""
    bot_self_id: str = ""
    method: str
    path: str = Field(max_length=512)
    status: int
    code: str = Field(default="", max_length=64)
    err_code: str = Field(default="", max_length=64)
    message: str = Field(max_length=1000)
    trace_id: str = Field(default="", max_length=256)
    content: str = Field(default="", sa_type=Text)
    created_at: float = Field(index=True, sa_type=Double)

    @classmethod
    @with_session
    async def record_batch(cls, session: AsyncSession, rows: list["QQBotUIDAPIError"], *, prune: bool) -> None:
        if prune:
            await session.execute(delete(cls).where(col(cls.created_at) < time.time() - RETAIN_DAYS * 86400))
        session.add_all(rows)

    @classmethod
    @with_read_session
    async def query_errors(
        cls,
        session: AsyncSession,
        days: int,
        appid: str,
        page: int,
        page_size: int,
        view: Literal["recent", "types"],
    ) -> "ErrorPage":
        now = time.time()
        since = (
            datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            if days == 1
            else now - days * 86400
        )
        conditions = [col(cls.created_at) >= since, col(cls.created_at) <= now]
        if appid:
            conditions.append(col(cls.appid) == appid)
        total_errors = (await session.execute(select(func.count()).select_from(cls).where(*conditions))).scalar_one()
        latest_ids = (
            select(func.max(col(cls.id)))
            .where(col(cls.created_at) >= now - RETAIN_DAYS * 86400)
            .group_by(col(cls.appid))
        )
        bot_rows = (
            await session.execute(select(col(cls.appid), col(cls.bot_name)).where(col(cls.id).in_(latest_ids)))
        ).all()
        bots = [ErrorBot(appid=bot_id, bot_name=name) for bot_id, name in bot_rows]
        if view == "recent":
            total = total_errors
            page = min(page, max(1, (total + page_size - 1) // page_size))
            records = (
                (
                    await session.execute(
                        select(cls)
                        .where(*conditions)
                        .order_by(col(cls.created_at).desc(), col(cls.id).desc())
                        .offset((page - 1) * page_size)
                        .limit(page_size)
                    )
                )
                .scalars()
                .all()
            )
            return ErrorPage(
                days=days,
                appid=appid,
                page=page,
                page_size=page_size,
                total=total,
                total_errors=total_errors,
                bots=bots,
                items=list(records),
            )
        count: ColumnElement[int] = func.count()
        last_seen: ColumnElement[float] = func.max(col(cls.created_at))
        grouped = (
            select(col(cls.code), col(cls.err_code), col(cls.message), count, last_seen)
            .where(*conditions)
            .group_by(col(cls.code), col(cls.err_code), col(cls.message))
        )
        total = (await session.execute(select(func.count()).select_from(grouped.subquery()))).scalar_one()
        page = min(page, max(1, (total + page_size - 1) // page_size))
        groups = (
            await session.execute(
                grouped.order_by(count.desc(), last_seen.desc(), col(cls.code), col(cls.err_code), col(cls.message))
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        types = [
            ErrorType(code=code, err_code=err_code, message=message, count=amount, last_seen=stamp)
            for code, err_code, message, amount, stamp in groups
        ]
        return ErrorPage(
            days=days,
            appid=appid,
            page=page,
            page_size=page_size,
            total=total,
            total_errors=total_errors,
            bots=bots,
            items=types,
        )


class ErrorPage(BaseModel):
    days: int
    appid: str
    page: int
    page_size: int
    total: int
    total_errors: int
    bots: list[ErrorBot]
    items: list[QQBotUIDAPIError] | list[ErrorType]


class ErrorResponse(TypedDict):
    status: int
    msg: str
    data: ErrorPage


async def ensure_table() -> None:
    import gsuid_core.utils.database.base_models as bm

    await bm.init_database()
    async with bm.engine.begin() as conn:
        await conn.run_sync(QQBotUIDAPIError.__table__.create, checkfirst=True)
