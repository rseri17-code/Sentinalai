#!/usr/bin/env python3
"""Initialize SentinalAI database schema.

Usage:
    python scripts/init_database.py
    python scripts/init_database.py --check   # verify schema only
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.connection import get_engine

logger = logging.getLogger("sentinalai.init_db")

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "database" / "schema.sql"

EXPECTED_TABLES = ["investigations", "knowledge_base", "tool_usage"]


def init_database(check_only: bool = False) -> bool:
    """Initialize or verify the database schema. Returns True on success."""
    engine = get_engine()
    if engine is None:
        logger.error("DATABASE_URL not set or database unreachable")
        return False

    try:
        with engine.connect() as conn:
            if check_only:
                return _verify_schema(conn)

            logger.info("Applying schema from %s", SCHEMA_PATH)
            sql = SCHEMA_PATH.read_text()
            for statement in sql.split(";"):
                statement = statement.strip()
                if statement:
                    conn.execute(statement)
            conn.commit()
            logger.info("Schema applied successfully")

            return _verify_schema(conn)

    except Exception as e:
        logger.error("Database initialization failed: %s", e)
        return False


def _verify_schema(conn) -> bool:
    """Verify all expected tables exist."""
    result = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public'"
    )
    existing = {row[0] for row in result}

    missing = [t for t in EXPECTED_TABLES if t not in existing]
    if missing:
        logger.error("Missing tables: %s", missing)
        return False

    logger.info("Schema verified: all %d tables present", len(EXPECTED_TABLES))
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize SentinalAI database")
    parser.add_argument("--check", action="store_true", help="Verify schema only")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    success = init_database(check_only=args.check)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
