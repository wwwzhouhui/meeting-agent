"""Meeting storage: two implementations behind one small interface.

Interface (callers must not change):
    save(meeting) -> Meeting
    get(meeting_id) -> Meeting | None
    list_all() -> list[Meeting]
    delete(meeting_id) -> bool

- ``InMemoryDB``: process-local dict. Default when DATABASE_URL is
  empty — tests and the key-less demo behave exactly as before, and
  everything vanishes on restart.
- ``PostgresDB``: durable store on one table

      meetings(id TEXT PRIMARY KEY, status TEXT NOT NULL,
               data JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL)

  The whole Meeting (Pydantic) is serialized into ``data``; ``status``
  is duplicated into its own column for cheap filtering. Rationale:
  every current query pattern is "get by id", "list all" or "filter by
  status" — a JSONB document store fits that exactly, and splitting
  the meeting into a dozen relational tables would buy nothing but
  migration pain while the schema is still evolving. If query patterns
  grow (per-owner task queries, full-text minutes search), promote
  those fields to columns/tables then.

``make_store(settings)`` picks the implementation from DATABASE_URL:

- empty                     -> InMemoryDB
- postgresql:// / postgres:// -> PostgresDB
- anything else             -> ValueError (loud, on purpose)

Connection failures in PostgresDB raise RuntimeError naming the target
host/db and whether DATABASE_URL was set — never a silent empty result.
"""
from __future__ import annotations

import copy
import threading
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .schema import Meeting

if TYPE_CHECKING:  # avoid an import cycle at module import time
    from .config import Settings


class InMemoryDB:
    def __init__(self) -> None:
        self._data: dict[str, Meeting] = {}
        self._lock = threading.Lock()

    def save(self, meeting: Meeting) -> Meeting:
        with self._lock:
            self._data[meeting.id] = copy.deepcopy(meeting)
        return meeting

    def get(self, meeting_id: str) -> Meeting | None:
        with self._lock:
            m = self._data.get(meeting_id)
            return copy.deepcopy(m) if m else None

    def list_all(self) -> list[Meeting]:
        with self._lock:
            return [copy.deepcopy(m) for m in self._data.values()]

    def delete(self, meeting_id: str) -> bool:
        with self._lock:
            return self._data.pop(meeting_id, None) is not None


_DDL = """
CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    data JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


class PostgresDB:
    """Postgres-backed store (psycopg v3, one connection per operation).

    A fresh Meeting object is materialized on every read, so callers
    can mutate what get()/list_all() return without corrupting the
    stored row — the same isolation InMemoryDB gives via deepcopy.
    """

    def __init__(self, database_url: str) -> None:
        self._url = database_url
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "DATABASE_URL points at Postgres but the psycopg driver "
                "is not installed; run: pip install \"psycopg[binary]>=3.1\""
            ) from exc
        self._psycopg = psycopg
        with self._connect() as conn:
            conn.execute(_DDL)

    # -- internals ---------------------------------------------------------
    def _target(self) -> str:
        """host:port/db for error messages (never credentials)."""
        parts = urlparse(self._url)
        host = parts.hostname or "?"
        port = parts.port or 5432
        db = (parts.path or "").lstrip("/") or "?"
        return f"host={host} port={port} db={db}"

    def _connect(self):
        try:
            return self._psycopg.connect(self._url)
        except Exception as exc:
            raise RuntimeError(
                f"PostgresDB: cannot connect to Postgres ({self._target()}); "
                f"DATABASE_URL is {'set' if (self._url or '').strip() else 'EMPTY'}; "
                f"original error: {exc}"
            ) from exc

    # -- store interface -----------------------------------------------------
    def save(self, meeting: Meeting) -> Meeting:
        from psycopg.types.json import Jsonb

        with self._connect() as conn:
            conn.execute(
                "INSERT INTO meetings (id, status, data, updated_at) "
                "VALUES (%s, %s, %s, now()) "
                "ON CONFLICT (id) DO UPDATE SET "
                "status = EXCLUDED.status, data = EXCLUDED.data, "
                "updated_at = now()",
                (meeting.id, meeting.status, Jsonb(meeting.model_dump(mode="json"))),
            )
        return meeting

    def get(self, meeting_id: str) -> Meeting | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM meetings WHERE id = %s", (meeting_id,)
            ).fetchone()
        if row is None:
            return None
        return Meeting.model_validate(row[0])

    def list_all(self) -> list[Meeting]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM meetings ORDER BY updated_at"
            ).fetchall()
        return [Meeting.model_validate(r[0]) for r in rows]

    def delete(self, meeting_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM meetings WHERE id = %s", (meeting_id,)
            )
        return cur.rowcount > 0


def make_store(settings: "Settings | None" = None):
    """Build the store for this process from ``settings.database_url``.

    Empty URL -> InMemoryDB (default; tests/demo unchanged).
    postgresql:// or postgres:// -> PostgresDB (creates its table).
    Any other scheme -> ValueError, loudly, instead of silently
    falling back to memory and "losing" data on restart.
    """
    if settings is None:
        from .config import get_settings

        settings = get_settings()
    url = (settings.database_url or "").strip()
    if not url:
        return InMemoryDB()
    if url.startswith(("postgresql://", "postgres://")):
        return PostgresDB(url)
    scheme = url.split("://", 1)[0] if "://" in url else url
    raise ValueError(
        f"DATABASE_URL has unsupported scheme {scheme!r}: use "
        "postgresql://... for Postgres persistence, or leave "
        "DATABASE_URL empty for the in-memory store"
    )


# Process-wide store used by the API layer and the flow's brief history.
# Built once at import: Postgres when DATABASE_URL is configured,
# in-memory otherwise. The API holds this single instance for the whole
# process — do not build another store per request.
store = make_store()
