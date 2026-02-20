"""Database connection management for SentinalAI."""

import os


def get_database_url() -> str:
    """Get database URL from environment."""
    return os.getenv(
        "DATABASE_URL",
        "postgresql://sentinalai:password@localhost:5432/sentinalai",
    )
