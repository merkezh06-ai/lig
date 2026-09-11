"""Bet365 bookmaker kesfi.

Bet365 ID'si KODA GOMULMEZ. Uygulama ilk baglantida ``/odds/bookmakers``
cagirir, ``name`` alaninda Bet365'i bulur ve gercek ID'yi cache'ler.

Bet365 listede yoksa uygulama bunu acikca soyler. Baska bir bookmaker
Bet365 yerine KULLANILMAZ.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from api_client import ApiFootballClient, CallBudget
from config import Settings, get_settings
from database import Database
from errors import AppError, BookmakerNotFoundError

logger = logging.getLogger(__name__)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _norm(text: Any) -> str:
    return _NON_ALNUM.sub("", str(text or "").lower())


@dataclass(frozen=True)
class BookmakerRef:
    id: int
    name: str


def find_bookmaker(
    rows: Iterable[Mapping[str, Any]], wanted_name: str
) -> BookmakerRef | None:
    """Bookmaker listesinde ismi eslesen kaydi bulur.

    Once tam eslesme ("bet365"), sonra "ile baslar" denenir. "Bet365" ile
    "Bet365 Bonus" gibi varyantlar ayni saglayicidir; ama "365bet" gibi
    farkli bir isim eslesmez.
    """
    target = _norm(wanted_name)
    if not target:
        return None

    candidates: list[BookmakerRef] = []
    for row in rows:
        try:
            bookmaker_id = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        candidates.append(BookmakerRef(bookmaker_id, name))

    for candidate in candidates:
        if _norm(candidate.name) == target:
            return candidate
    for candidate in candidates:
        if _norm(candidate.name).startswith(target):
            return candidate
    return None


class BookmakerResolver:
    """Bet365 ID'sini kesfeder ve saklar."""

    def __init__(
        self,
        client: ApiFootballClient,
        database: Database,
        settings: Settings | None = None,
    ) -> None:
        self.client = client
        self.database = database
        self.settings = settings or get_settings()
        self._cached: BookmakerRef | None = None
        self._last_error: str | None = None
        self._all: list[BookmakerRef] = []

    @property
    def cached(self) -> BookmakerRef | None:
        return self._cached

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def resolve(
        self, budget: CallBudget | None = None, force: bool = False
    ) -> BookmakerRef:
        """Bet365 referansini dondurur; bulunamazsa acik hata firlatir."""
        if self._cached is not None and not force:
            return self._cached

        rows = await self.client.bookmakers(budget=budget)
        self._all = [
            BookmakerRef(int(row["id"]), str(row.get("name") or ""))
            for row in rows
            if str(row.get("id", "")).isdigit()
        ]

        found = find_bookmaker(rows, self.settings.bookmaker_name)
        if found is None:
            self._last_error = (
                f"{self.settings.bookmaker_name} bookmaker listesinde bulunamadi "
                f"({len(rows)} bookmaker donduruldu)."
            )
            logger.error(self._last_error)
            raise BookmakerNotFoundError(
                f"{self.settings.bookmaker_name} bookmaker listesinde bulunamadi. "
                "Bu API anahtari/plani bu bookmaker'i saglamiyor olabilir. "
                "Baska bookmaker onun yerine kullanilmaz."
            )

        self._cached = found
        self._last_error = None
        logger.info("Bookmaker bulundu: %s (id=%s)", found.name, found.id)
        try:
            self.database.upsert_bookmaker(found.id, found.name)
        except AppError:  # pragma: no cover - cache yazimi kritik degil
            logger.debug("Bookmaker veritabanina yazilamadi")
        return found

    async def try_resolve(
        self, budget: CallBudget | None = None
    ) -> tuple[BookmakerRef | None, str | None]:
        """Bet365'i dener; (referans, hata_mesaji) dondurur.

        DIKKAT: yalnizca FATAL OLMAYAN hatalar yutulur. Anahtar yok/gecersiz
        gibi yapilandirma hatalari yukari firlatilir - aksi halde "API anahtari
        yok" durumu ekranda "Bet365 orani yok" gibi gorunur ve gercek sebep
        kaybolur.
        """
        try:
            return await self.resolve(budget=budget), None
        except AppError as exc:
            if exc.fatal:
                self._last_error = exc.user_message
                raise
            self._last_error = exc.user_message
            return None, exc.user_message

    def known_bookmakers(self) -> list[BookmakerRef]:
        return list(self._all)
