import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


def _build_database_url() -> str:
    # Explicit override wins (used by tests and exotic deployments); the
    # docker-compose default points at the Postgres service below.
    override = os.environ.get("DATABASE_URL")
    if override:
        return override
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = os.environ.get("POSTGRES_PORT", "5432")
    user = os.environ.get("POSTGRES_USER", "postgres")
    password = os.environ.get("POSTGRES_PASSWORD", "postgres")
    db = os.environ.get("APP_DB_NAME", "mem0_app")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db}"


_DATABASE_URL = _build_database_url()

# Pool sizing knobs are Postgres-specific; sqlite ignores/errs on them.
if _DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        _DATABASE_URL,
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )
else:
    engine = create_engine(
        _DATABASE_URL,
        pool_pre_ping=True,
        pool_size=int(os.environ.get("MEM0_DB_POOL_SIZE", "10")),
        max_overflow=int(os.environ.get("MEM0_DB_MAX_OVERFLOW", "20")),
        pool_recycle=int(os.environ.get("MEM0_DB_POOL_RECYCLE", "3600")),
        pool_timeout=int(os.environ.get("MEM0_DB_POOL_TIMEOUT", "30")),
    )

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency that yields a SQLAlchemy session."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
