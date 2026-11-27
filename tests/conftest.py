import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import dtek_http  # noqa: E402
import groups  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Кожен тест — своя порожня БД (тест може підмінити DB_PATH сам), типові черги, без паузи сайту ДТЕК."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "default.db"))
    # без справжнього браузера; тести браузерної сесії вмикають його самі (з фейковим nodriver)
    monkeypatch.setattr(dtek_http, "BROWSER_ENABLED", False)
    monkeypatch.setattr(dtek_http, "DTEK_COOKIE_CACHE", str(tmp_path / "dtek_cookies.json"))
    monkeypatch.setattr(dtek_http, "_browser", None)
    monkeypatch.setattr(dtek_http, "_browser_loaded", False)
    monkeypatch.setattr(dtek_http, "_browser_last_try", 0.0)
    monkeypatch.setattr(dtek_http, "_page", None)
    asyncio.run(db.init())
    groups.reset()
    dtek_http.clear_backoff()
    dtek_http._session = None
    yield
    groups.reset()
    dtek_http.clear_backoff()
    dtek_http._session = None
