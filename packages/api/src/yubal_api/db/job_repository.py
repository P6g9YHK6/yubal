"""Database repository for jobs."""

from sqlalchemy import Engine
from sqlmodel import Session, col, delete, select

from yubal_api.db.job import JobRecord


class JobRepository:
    """Repository for job database operations."""

    def __init__(self, engine: Engine) -> None:
        """Initialize repository with database engine."""
        self._engine = engine

    def list(self) -> list[JobRecord]:
        """List saved jobs in queue order."""
        with Session(self._engine) as session:
            stmt = select(JobRecord).order_by(col(JobRecord.position))
            return list(session.exec(stmt).all())

    def save(self, id: str, position: int, data: str) -> None:
        """Insert or update a job."""
        with Session(self._engine) as session:
            session.merge(JobRecord(id=id, position=position, data=data))
            session.commit()

    def delete(self, id: str) -> None:
        """Delete a job. Does nothing if it doesn't exist."""
        with Session(self._engine) as session:
            session.exec(delete(JobRecord).where(col(JobRecord.id) == id))
            session.commit()
