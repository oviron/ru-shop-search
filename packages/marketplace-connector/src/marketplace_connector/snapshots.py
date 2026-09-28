"""Свои снимки цен: строки search и compare с ценой в рублях ложатся в SQLite в каталоге данных плагина."""

from __future__ import annotations

import datetime
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from urllib.parse import urlsplit

from compare_connector.models_output import MarketOffer
from mcp_core.plugin_data import plugin_data_dir
from pydantic import BaseModel, Field

_FILE = "price_snapshots.sqlite"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_snapshot (
    shop TEXT NOT NULL, product_id TEXT NOT NULL, url_key TEXT NOT NULL, title TEXT NOT NULL,
    price_rub REAL NOT NULL, observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS price_snapshot_product ON price_snapshot (shop, product_id);
CREATE INDEX IF NOT EXISTS price_snapshot_url ON price_snapshot (shop, url_key);
"""


class Snapshot(BaseModel):
    observed_at: str = Field(description="Когда цена была в выдаче, UTC ISO.")
    price_rub: float = Field(description="Обычная цена в рублях из строки search или compare.")
    title: str = Field(default="", description="Название из той же строки.")


def url_key(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.netloc.lower()}{parts.path.rstrip('/')}" if parts.netloc else ""


class Snapshots:
    def __init__(self) -> None:
        self._path = plugin_data_dir() / _FILE

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self._path)
        db.executescript(_SCHEMA)
        return db

    def record(self, offers: Iterable[MarketOffer], *, now: datetime.datetime) -> None:
        """Одна запись на товар, день и цену: повторный поиск в тот же день не раздувает базу."""
        stamp = now.astimezone(datetime.UTC).isoformat(timespec="seconds")
        rows = [
            (o.source, o.product_id, url_key(o.url), o.title[:200], o.price_rub, stamp)
            for o in offers
            if o.product_id and o.price_rub is not None and o.currency == "rub"
        ]
        if not rows:
            return
        with closing(self._connect()) as db, db:
            db.executemany(
                "INSERT INTO price_snapshot SELECT ?1, ?2, ?3, ?4, ?5, ?6 WHERE NOT EXISTS ("
                "SELECT 1 FROM price_snapshot WHERE shop = ?1 AND product_id = ?2 AND price_rub = ?5 "
                "AND substr(observed_at, 1, 10) = substr(?6, 1, 10))",
                rows,
            )

    def history(self, shop: str, *, product_id: str = "", url: str = "") -> list[Snapshot]:
        key = url_key(url) if url else ""
        if not self._path.exists() or not (product_id or key):
            return []
        with closing(self._connect()) as db:
            found = db.execute(
                "SELECT observed_at, price_rub, title FROM price_snapshot "
                "WHERE shop = ? AND (product_id = ? OR (? != '' AND url_key = ?)) ORDER BY observed_at",
                (shop, product_id, key, key),
            ).fetchall()
        return [Snapshot(observed_at=a, price_rub=p, title=t) for a, p, t in found]
