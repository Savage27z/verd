import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Every test gets its own empty SQLite file."""
    monkeypatch.setenv("ODIFY_DB", str(tmp_path / "odify.db"))
    from odify_bot import store
    store.init_db()
    yield


def fake_leads(n: int, prefix: str = "p") -> list[dict]:
    return [{
        "place_id": f"{prefix}{i}", "name": f"Biz {i}", "phone": f"0803 000 000{i}",
        "intl_phone": f"+234 803 000 000{i}", "address": f"{i} Allen Ave, Ikeja", "email": "",
        "website": "", "web_presence": "none", "category": "Barber shop", "rating": 4.5,
        "reviews": 10 * i, "maps_url": f"https://maps.google.com/?cid={i}", "score": 50 - i,
    } for i in range(n)]
