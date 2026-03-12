"""
SQLite cache for downloaded filing data and XML files.
Prevents redundant API calls across runs.
"""

import json
import os
import sqlite3
import time
from typing import Optional


_cache_instance = None


def get_cache() -> "FilingCache":
    """Get the singleton cache instance."""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = FilingCache()
    return _cache_instance


class FilingCache:
    """Local SQLite cache for ProPublica API responses and XML filings."""

    def __init__(self, db_path: str = None):
        if db_path is None:
            cache_dir = os.path.join(os.path.expanduser("~"), ".prospect-explorer")
            os.makedirs(cache_dir, exist_ok=True)
            db_path = os.path.join(cache_dir, "cache.db")
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS org_data (
                    ein TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    fetched_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS xml_filings (
                    object_id TEXT PRIMARY KEY,
                    xml_data BLOB NOT NULL,
                    fetched_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS parsed_grants (
                    funder_ein TEXT NOT NULL,
                    tax_year TEXT NOT NULL,
                    grants_json TEXT NOT NULL,
                    parsed_at REAL NOT NULL,
                    PRIMARY KEY (funder_ein, tax_year)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS officers (
                    ein TEXT PRIMARY KEY,
                    officers_json TEXT NOT NULL,
                    fetched_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS web_pages (
                    cache_key TEXT PRIMARY KEY,
                    page_html TEXT NOT NULL,
                    fetched_at REAL NOT NULL
                )
            """)
            conn.commit()

    def get_org(self, ein: str, max_age_hours: float = 168) -> Optional[dict]:
        """Get cached org data. Default max age: 7 days."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT data, fetched_at FROM org_data WHERE ein = ?", (ein,)
            ).fetchone()
            if row:
                age_hours = (time.time() - row[1]) / 3600
                if age_hours <= max_age_hours:
                    return json.loads(row[0])
        return None

    def store_org(self, ein: str, data: dict):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO org_data (ein, data, fetched_at) VALUES (?, ?, ?)",
                (ein, json.dumps(data), time.time()),
            )
            conn.commit()

    def get_xml(self, object_id: str, max_age_hours: float = 8760) -> Optional[bytes]:
        """Get cached XML. Default max age: 1 year (filings don't change)."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT xml_data, fetched_at FROM xml_filings WHERE object_id = ?",
                (object_id,),
            ).fetchone()
            if row:
                age_hours = (time.time() - row[1]) / 3600
                if age_hours <= max_age_hours:
                    return row[0]
        return None

    def store_xml(self, object_id: str, xml_data: bytes):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO xml_filings (object_id, xml_data, fetched_at) VALUES (?, ?, ?)",
                (object_id, xml_data, time.time()),
            )
            conn.commit()

    def get_grants(self, funder_ein: str, tax_year: str) -> Optional[list]:
        """Get cached parsed grants for a funder+year."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT grants_json FROM parsed_grants WHERE funder_ein = ? AND tax_year = ?",
                (funder_ein, tax_year),
            ).fetchone()
            if row:
                return json.loads(row[0])
        return None

    def store_grants(self, funder_ein: str, tax_year: str, grants: list):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO parsed_grants (funder_ein, tax_year, grants_json, parsed_at) "
                "VALUES (?, ?, ?, ?)",
                (funder_ein, tax_year, json.dumps([g.__dict__ if hasattr(g, '__dict__') else g for g in grants]), time.time()),
            )
            conn.commit()

    def get_officers(self, ein: str, max_age_hours: float = 8760) -> Optional[list]:
        """Get cached officers for an org. Default max age: 1 year."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT officers_json, fetched_at FROM officers WHERE ein = ?", (ein,)
            ).fetchone()
            if row:
                age_hours = (time.time() - row[1]) / 3600
                if age_hours <= max_age_hours:
                    return json.loads(row[0])
        return None

    def store_officers(self, ein: str, officers: list):
        """Cache officers for an org."""
        officers_data = [
            o.__dict__ if hasattr(o, '__dict__') else o
            for o in officers
        ]
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO officers (ein, officers_json, fetched_at) VALUES (?, ?, ?)",
                (ein, json.dumps(officers_data), time.time()),
            )
            conn.commit()

    def get_web_page(self, cache_key: str, max_age_hours: float = 168) -> Optional[str]:
        """Get cached web page. Default max age: 7 days."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT page_html, fetched_at FROM web_pages WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if row:
                age_hours = (time.time() - row[1]) / 3600
                if age_hours <= max_age_hours:
                    return row[0]
        return None

    def set_web_page(self, cache_key: str, page_html: str, ttl: int = None):
        """Cache a web page."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO web_pages (cache_key, page_html, fetched_at) VALUES (?, ?, ?)",
                (cache_key, page_html, time.time()),
            )
            conn.commit()

    def clear(self):
        """Clear all cached data."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM org_data")
            conn.execute("DELETE FROM xml_filings")
            conn.execute("DELETE FROM parsed_grants")
            conn.execute("DELETE FROM officers")
            conn.commit()

    def stats(self) -> dict:
        """Return cache statistics."""
        with sqlite3.connect(self.db_path) as conn:
            orgs = conn.execute("SELECT COUNT(*) FROM org_data").fetchone()[0]
            xmls = conn.execute("SELECT COUNT(*) FROM xml_filings").fetchone()[0]
            grants = conn.execute("SELECT COUNT(*) FROM parsed_grants").fetchone()[0]
            officers = conn.execute("SELECT COUNT(*) FROM officers").fetchone()[0]
        return {"organizations": orgs, "xml_filings": xmls, "parsed_grants": grants, "officers": officers}
