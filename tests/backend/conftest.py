from __future__ import annotations

import pytest

from ai_research_radar.db import create_db_engine, init_schema, session_factory


@pytest.fixture
def session():
    engine = create_db_engine("sqlite:///:memory:")
    init_schema(engine)
    factory = session_factory(engine)
    with factory() as value:
        yield value
