from collections.abc import AsyncGenerator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ap_inbox.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()

async_engine = create_async_engine(settings.database_url, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(async_engine, expire_on_commit=False, class_=AsyncSession)

sync_engine = create_engine(settings.database_url_sync, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=sync_engine, autocommit=False, autoflush=False, class_=Session)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


def get_sync_db() -> Session:
    return SessionLocal()
