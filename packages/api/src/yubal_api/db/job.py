"""Database model for persisted jobs."""

from sqlmodel import Field, SQLModel


class JobRecord(SQLModel, table=True):
    """A job saved so the queue survives restarts.

    The job itself is stored as JSON, it is only ever loaded back as a whole.
    """

    __tablename__ = "jobs"

    id: str = Field(primary_key=True)
    position: int = Field(index=True)
    data: str
