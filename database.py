"""Kalici veri katmani.

Ayni kod iki motorla calisir:

* ``DATABASE_URL`` verilmisse  -> PostgreSQL (psycopg 3)
* verilmemisse                 -> yerel SQLite dosyasi

Render'in ucretsiz web servisinde dosya sistemi geciCidir; SQLite her
uyanista sifirlanir. Bu durumda ``persistent`` False doner ve uygulama bunu
kullaniciya ACIKCA soyler - snapshot gecmisi varmis gibi davranmaz.

ORM kullanilmaz: sema kucuk, sorgular parametrelidir (SQL injection yok).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Sequence

from config import Settings, get_settings
from errors import DatabaseError

logger = logging.getLogger(__name__)

_SQLITE_ID = "INTEGER PRIMARY KEY AUTOINCREMENT"
_PG_ID = "BIGSERIAL PRIMARY KEY"


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def from_iso(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Database:
    """Kucuk, tasinabilir bir veri erisim katmani."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._lock = threading.RLock()
        self._sqlite_conn: sqlite3.Connection | None = None
        self._psycopg = None

        if self.settings.uses_postgres():
            try:
                import psycopg  # type: ignore

                self._psycopg = psycopg
                self.dialect = "postgres"
            except ImportError:  # pragma: no cover - kurulum eksikligi
                logger.error(
                    "DATABASE_URL verildi ama psycopg kurulu degil; SQLite'a dusuluyor."
                )
                self.dialect = "sqlite"
        else:
            self.dialect = "sqlite"

    # ------------------------------------------------------------------
    # Baglanti
    # ------------------------------------------------------------------
    @property
    def persistent(self) -> bool:
        """Veri deploy/restart sonrasi kaliyor mu?"""
        return self.dialect == "postgres"

    def describe(self) -> str:
        if self.dialect == "postgres":
            return "postgres"
        return f"sqlite ({self.settings.sqlite_path})"

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        if self.dialect == "postgres":
            assert self._psycopg is not None
            url = self.settings.database_url
            # psycopg 3 "postgres://" semasini da kabul eder.
            conn = self._psycopg.connect(url, connect_timeout=10)
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        else:
            with self._lock:
                if self._sqlite_conn is None:
                    self._sqlite_conn = sqlite3.connect(
                        self.settings.sqlite_path, check_same_thread=False, timeout=15
                    )
                    self._sqlite_conn.row_factory = sqlite3.Row
                    self._sqlite_conn.execute("PRAGMA journal_mode=WAL")
                    self._sqlite_conn.execute("PRAGMA foreign_keys=ON")
                conn = self._sqlite_conn
                try:
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

    def _sql(self, statement: str) -> str:
        """SQL '?' yer tutucularini motora gore cevirir."""
        if self.dialect == "postgres":
            return statement.replace("?", "%s")
        return statement

    def execute(self, statement: str, params: Sequence[Any] = ()) -> None:
        try:
            with self._connect() as conn:
                conn.execute(self._sql(statement), tuple(params))
        except Exception as exc:  # pragma: no cover - motor hatasi
            logger.exception("Veritabani yazma hatasi")
            raise DatabaseError(detail=str(exc)) from exc

    def execute_many(self, statement: str, rows: Sequence[Sequence[Any]]) -> None:
        if not rows:
            return
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.executemany(self._sql(statement), [tuple(row) for row in rows])
        except Exception as exc:  # pragma: no cover
            logger.exception("Veritabani toplu yazma hatasi")
            raise DatabaseError(detail=str(exc)) from exc

    def query(self, statement: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(self._sql(statement), tuple(params))
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except Exception as exc:  # pragma: no cover
            logger.exception("Veritabani okuma hatasi")
            raise DatabaseError(detail=str(exc)) from exc

    def query_one(self, statement: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(statement, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # Sema
    # ------------------------------------------------------------------
    def init_schema(self) -> None:
        auto_id = _PG_ID if self.dialect == "postgres" else _SQLITE_ID
        statements = [
            """
            CREATE TABLE IF NOT EXISTS api_cache (
                cache_key   TEXT PRIMARY KEY,
                payload     TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                expires_at  TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS bookmakers (
                id          INTEGER PRIMARY KEY,
                name        TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS leagues (
                id              INTEGER PRIMARY KEY,
                name            TEXT NOT NULL,
                country         TEXT,
                current_season  INTEGER,
                updated_at      TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS teams (
                id          INTEGER PRIMARY KEY,
                name        TEXT NOT NULL,
                logo        TEXT,
                updated_at  TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS fixtures (
                id            INTEGER PRIMARY KEY,
                league_id     INTEGER,
                season        INTEGER,
                home_team_id  INTEGER,
                away_team_id  INTEGER,
                home_name     TEXT,
                away_name     TEXT,
                kickoff_utc   TEXT,
                status_short  TEXT,
                updated_at    TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS odds_snapshots (
                id              {auto_id},
                fixture_id      INTEGER NOT NULL,
                bookmaker_id    INTEGER NOT NULL,
                bookmaker_name  TEXT NOT NULL,
                market          TEXT NOT NULL,
                home_odds       REAL,
                draw_odds       REAL,
                away_odds       REAL,
                extra           TEXT,
                captured_at     TEXT NOT NULL,
                kickoff_utc     TEXT,
                source          TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS predictions_log (
                id                {auto_id},
                fixture_id        INTEGER NOT NULL,
                created_at        TEXT NOT NULL,
                kickoff_utc       TEXT,
                model_home        REAL,
                model_draw        REAL,
                model_away        REAL,
                market_home       REAL,
                market_draw       REAL,
                market_away       REAL,
                confidence        REAL,
                data_quality      REAL,
                actual_home_goals INTEGER,
                actual_away_goals INTEGER,
                settled_at        TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS analysis_cache (
                fixture_id  INTEGER PRIMARY KEY,
                payload     TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                expires_at  TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_snapshots_fixture "
            "ON odds_snapshots (fixture_id, market, captured_at)",
            "CREATE INDEX IF NOT EXISTS idx_snapshots_captured "
            "ON odds_snapshots (captured_at)",
            "CREATE INDEX IF NOT EXISTS idx_predlog_fixture "
            "ON predictions_log (fixture_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_cache_expires ON api_cache (expires_at)",
        ]
        for statement in statements:
            self.execute(statement)
        logger.info("Veritabani semasi hazir (%s)", self.describe())

    # ------------------------------------------------------------------
    # API cache
    # ------------------------------------------------------------------
    def cache_get(self, key: str) -> Any | None:
        row = self.query_one("SELECT payload, expires_at FROM api_cache WHERE cache_key = ?", (key,))
        if not row:
            return None
        expires = from_iso(row["expires_at"])
        if expires is None or expires <= utcnow():
            return None
        try:
            return json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            return None

    def cache_set(self, key: str, payload: Any, expires_at: datetime) -> None:
        now = to_iso(utcnow())
        blob = json.dumps(payload, ensure_ascii=False)
        if self.dialect == "postgres":
            statement = (
                "INSERT INTO api_cache (cache_key, payload, created_at, expires_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (cache_key) DO UPDATE SET "
                "payload = EXCLUDED.payload, created_at = EXCLUDED.created_at, "
                "expires_at = EXCLUDED.expires_at"
            )
        else:
            statement = (
                "INSERT OR REPLACE INTO api_cache (cache_key, payload, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)"
            )
        self.execute(statement, (key, blob, now, to_iso(expires_at)))

    def cache_purge_expired(self) -> None:
        self.execute("DELETE FROM api_cache WHERE expires_at <= ?", (to_iso(utcnow()),))

    # ------------------------------------------------------------------
    # Referans kayitlari
    # ------------------------------------------------------------------
    def upsert_bookmaker(self, bookmaker_id: int, name: str) -> None:
        self._upsert("bookmakers", "id", (bookmaker_id, name, to_iso(utcnow())), ("id", "name", "updated_at"))

    def upsert_league(self, league_id: int, name: str, country: str | None, season: int | None) -> None:
        self._upsert(
            "leagues",
            "id",
            (league_id, name, country, season, to_iso(utcnow())),
            ("id", "name", "country", "current_season", "updated_at"),
        )

    def upsert_team(self, team_id: int, name: str, logo: str | None) -> None:
        self._upsert("teams", "id", (team_id, name, logo, to_iso(utcnow())), ("id", "name", "logo", "updated_at"))

    def upsert_fixture(self, values: dict[str, Any]) -> None:
        columns = (
            "id",
            "league_id",
            "season",
            "home_team_id",
            "away_team_id",
            "home_name",
            "away_name",
            "kickoff_utc",
            "status_short",
            "updated_at",
        )
        row = tuple(values.get(column) for column in columns[:-1]) + (to_iso(utcnow()),)
        self._upsert("fixtures", "id", row, columns)

    def _upsert(self, table: str, key: str, row: Sequence[Any], columns: Sequence[str]) -> None:
        placeholders = ", ".join("?" for _ in columns)
        column_list = ", ".join(columns)
        if self.dialect == "postgres":
            updates = ", ".join(f"{col} = EXCLUDED.{col}" for col in columns if col != key)
            statement = (
                f"INSERT INTO {table} ({column_list}) VALUES ({placeholders}) "
                f"ON CONFLICT ({key}) DO UPDATE SET {updates}"
            )
        else:
            statement = f"INSERT OR REPLACE INTO {table} ({column_list}) VALUES ({placeholders})"
        self.execute(statement, row)

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------
    def latest_snapshot(self, fixture_id: int, market: str) -> dict[str, Any] | None:
        return self.query_one(
            "SELECT * FROM odds_snapshots WHERE fixture_id = ? AND market = ? "
            "ORDER BY captured_at DESC LIMIT 1",
            (fixture_id, market),
        )

    def insert_snapshot(self, row: dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO odds_snapshots (fixture_id, bookmaker_id, bookmaker_name, market, "
            "home_odds, draw_odds, away_odds, extra, captured_at, kickoff_utc, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["fixture_id"],
                row["bookmaker_id"],
                row["bookmaker_name"],
                row["market"],
                row.get("home_odds"),
                row.get("draw_odds"),
                row.get("away_odds"),
                json.dumps(row.get("extra") or {}, ensure_ascii=False),
                row["captured_at"],
                row.get("kickoff_utc"),
                row.get("source", "api-football"),
            ),
        )

    def snapshots_for(self, fixture_id: int, market: str = "1x2") -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM odds_snapshots WHERE fixture_id = ? AND market = ? "
            "ORDER BY captured_at ASC",
            (fixture_id, market),
        )

    def snapshot_stats(self) -> dict[str, Any]:
        row = self.query_one(
            "SELECT COUNT(*) AS total, MAX(captured_at) AS last_at FROM odds_snapshots"
        )
        if not row:
            return {"total": 0, "last_at": None}
        return {"total": int(row.get("total") or 0), "last_at": from_iso(row.get("last_at"))}

    # ------------------------------------------------------------------
    # Tahmin kaydi (kalibrasyon icin)
    # ------------------------------------------------------------------
    def log_prediction(self, row: dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO predictions_log (fixture_id, created_at, kickoff_utc, model_home, "
            "model_draw, model_away, market_home, market_draw, market_away, confidence, "
            "data_quality) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["fixture_id"],
                to_iso(utcnow()),
                row.get("kickoff_utc"),
                row.get("model_home"),
                row.get("model_draw"),
                row.get("model_away"),
                row.get("market_home"),
                row.get("market_draw"),
                row.get("market_away"),
                row.get("confidence"),
                row.get("data_quality"),
            ),
        )

    def settle_prediction(self, fixture_id: int, home_goals: int, away_goals: int) -> None:
        self.execute(
            "UPDATE predictions_log SET actual_home_goals = ?, actual_away_goals = ?, "
            "settled_at = ? WHERE fixture_id = ? AND settled_at IS NULL",
            (home_goals, away_goals, to_iso(utcnow()), fixture_id),
        )

    def calibration_rows(self, limit: int = 500) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM predictions_log WHERE settled_at IS NOT NULL "
            "ORDER BY settled_at DESC LIMIT ?",
            (limit,),
        )


_db: Database | None = None


def get_database(settings: Settings | None = None, refresh: bool = False) -> Database:
    global _db
    if _db is None or refresh:
        _db = Database(settings)
    return _db
