import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from app.db import Base
from app.config import DATABASE_URL, TEST_DATABASE_URL


def _safe_test_url() -> str:
    """Return a database URL that is guaranteed not to be the main database.

    The session fixture below runs drop_all() when the tests finish, so it
    must never point at DATABASE_URL. If TEST_DATABASE_URL is unset, equals
    the main URL, or its database name doesn't contain "test", fall back to
    "<main db name>_test" on the same server. Anything still unsafe aborts
    the run before a single table is touched.
    """
    main = make_url(DATABASE_URL) if DATABASE_URL else None
    url = make_url(TEST_DATABASE_URL) if TEST_DATABASE_URL else None

    def is_main(u):
        return main is not None and (u.host, u.port, u.database) == (main.host, main.port, main.database)

    if url is None or is_main(url) or "test" not in (url.database or "").lower():
        if main is None:
            pytest.exit("No DATABASE_URL/TEST_DATABASE_URL configured; refusing to run DB tests.", returncode=2)
        url = main.set(database=f"{main.database}_test")

    if is_main(url) or "test" not in (url.database or "").lower():
        pytest.exit("Refusing to run: test database would be the main database.", returncode=2)
    return url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def test_engine():
    engine = create_engine(_safe_test_url())
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session(test_engine, monkeypatch):
    TestSession = sessionmaker(bind=test_engine)
    session = TestSession()
    yield session
    session.rollback()
    session.close()


@pytest.fixture(autouse=True)
def _isolated_vault(tmp_path, monkeypatch):
    """Every test gets its own empty document vault, so no test can ever
    write into the real storage/ folder."""
    from app import vault
    monkeypatch.setattr(vault, "ROOT", tmp_path / "documents")
    yield vault.ROOT


@pytest.fixture(autouse=True)
def _no_vault_marker(monkeypatch):
    # Tests use temporary vault folders, which never hold the .csp_vault marker.
    monkeypatch.setattr("app.vault.STORAGE_REQUIRE_MARKER", False)
