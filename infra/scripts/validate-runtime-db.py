#!/usr/bin/env python3
"""Validate the production runtime role without leaving probe data behind."""

from __future__ import annotations

import os
from uuid import uuid4

from sqlalchemy import text

from ai_research_radar.db import create_db_engine, validate_production_schema


def main() -> None:
    database_url = os.environ.get("RADAR_DATABASE_URL", "")
    if not database_url:
        raise SystemExit("RADAR_DATABASE_URL is required")

    engine = create_db_engine(database_url)
    probe_key = f"__radar_runtime_probe_{uuid4().hex}"
    try:
        validate_production_schema(engine)
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                current_user = connection.scalar(text("select current_user"))
                if current_user != "radar_runtime":
                    raise RuntimeError(
                        f"Expected radar_runtime, connected as {current_user!r}"
                    )

                connection.execute(
                    text(
                        "insert into public.usage_ledger "
                        "(usage_date, usage_key, used, hard_limit) "
                        "values (current_date, :usage_key, 0, 1)"
                    ),
                    {"usage_key": probe_key},
                )
                connection.execute(
                    text(
                        "update public.usage_ledger set used = 1 "
                        "where usage_date = current_date and usage_key = :usage_key"
                    ),
                    {"usage_key": probe_key},
                )
                used = connection.scalar(
                    text(
                        "select used from public.usage_ledger "
                        "where usage_date = current_date and usage_key = :usage_key"
                    ),
                    {"usage_key": probe_key},
                )
                if used != 1:
                    raise RuntimeError("Runtime role CRUD probe returned an invalid value")
                connection.execute(
                    text(
                        "delete from public.usage_ledger "
                        "where usage_date = current_date and usage_key = :usage_key"
                    ),
                    {"usage_key": probe_key},
                )
            finally:
                transaction.rollback()
    finally:
        engine.dispose()

    print("radar_runtime schema and rollback CRUD probe passed")


if __name__ == "__main__":
    main()
