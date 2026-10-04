import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Every test gets its own empty database and a known admin in the environment."""
    from drugscope.community import db

    monkeypatch.setenv("DRUGSCOPE_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DRUGSCOPE_ADMIN_USERNAME", "chief")
    monkeypatch.setenv("DRUGSCOPE_ADMIN_PASSWORD", "admin-password-123")
    monkeypatch.setenv("DRUGSCOPE_FREE_CREDITS", "5")
    monkeypatch.setenv("DRUGSCOPE_PRO_CREDITS", "60")
    monkeypatch.setenv("DRUGSCOPE_QUOTA_WINDOW_HOURS", "24")
    db.reset_for_tests()
    yield
    db.reset_for_tests()


@pytest.fixture
def admin():
    from drugscope.community import auth

    assert auth.bootstrap_admin() is None
    return auth.authenticate("chief", "admin-password-123")


@pytest.fixture
def make_user():
    from drugscope.community import auth

    def factory(name="alice"):
        return auth.register(name, f"{name}-password-1", confirmed_adult=True)

    return factory
