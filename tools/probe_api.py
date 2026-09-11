"""API-Football canli teshis araci.

Amac: VARSAYIMLARI OLCUMLE DEGISTIRMEK. Bir sorun cikarsa "bizim kodumuz mu,
API mi" sorusunu kesin cevaplar. Her basarisiz madde icin HAM HTTP kodu,
gonderilen parametreler ve API'nin kendi hata govdesi raporlanir.

Kullanim:
    set API_FOOTBALL_KEY=...            (Windows)
    export API_FOOTBALL_KEY=...         (macOS / Linux)

    python tools/probe_api.py                 # 7 gunluk pencere
    python tools/probe_api.py --days 14       # pencereyi genislet
    python tools/probe_api.py --raw           # ham fixture + odds dokumu
    python tools/probe_api.py --league 39     # tek lig
    python tools/probe_api.py --cache         # cache'e izin ver (varsayilan: KAPALI)

Cache varsayilan olarak KAPALIDIR: teshis araci canli API'yi olcmelidir.
(Cache'ten donen cevapta rate-limit header'i olmadigi icin kota "bilinmiyor"
gorunuyordu - bu, olcum hatasiydi, API sorunu degil.)

Cikti API ANAHTARINI ICERMEZ; guvenle paylasabilirsiniz.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api_client import ApiFootballClient, CallBudget  # noqa: E402
from bookmakers import find_bookmaker  # noqa: E402
from config import configure_logging, get_settings  # noqa: E402
from database import Database  # noqa: E402
from errors import AppError, SeasonNotAccessibleError  # noqa: E402
from odds_parser import (  # noqa: E402
    extract_markets,
    match_market_name,
    parse_odds_rows,
    select_bookmaker,
)

LINE = "-" * 74
HATA, UYARI, BILGI = "HATA", "UYARI", "BILGI"

problems: list[dict[str, Any]] = []


def head(title: str) -> None:
    print("\n" + LINE)
    print(title)
    print(LINE)


def item(label: str, value: object, ok: bool | None = None) -> None:
    mark = "  " if ok is None else ("OK" if ok else "!!")
    print(f"[{mark}] {label:<40} {value}")


def problem(section: str, severity: str, message: str, evidence: str = "") -> None:
    problems.append(
        {"no": len(problems) + 1, "section": section, "severity": severity,
         "message": message, "evidence": evidence}
    )
    print(f"     -> {severity}: {message}")
    if evidence:
        print(f"        kanit: {evidence[:300]}")


def describe_error(exc: AppError) -> str:
    """Hatanin HAM bilgisini tek satira indirir."""
    status = getattr(exc, "upstream_status", 0) or "-"
    detail = (exc.detail or "").strip().replace("\n", " ")
    return f"HTTP {status} / {exc.code} :: {detail[:300] or exc.user_message}"


def normalise(text: Any) -> str:
    """Aksan ve buyuk/kucuk harf farkini yok sayar ('Super' == 'Süper')."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", stripped.lower())


def short_json(payload: Any, limit: int = 900) -> str:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return text if len(text) <= limit else text[:limit] + "\n  ... (kisaltildi)"


# ==========================================================================
async def main(args: argparse.Namespace) -> int:
    settings = get_settings(refresh=True)
    configure_logging(settings)
    use_cache = bool(args.cache)

    head("MACANALIZ PRO - API-Football canli teshis")
    print(f"Base URL   : {settings.api_base_url}")
    print(f"Zaman      : {datetime.now(tz=settings.tzinfo).strftime('%d.%m.%Y %H:%M')} "
          f"({settings.timezone_name})")
    print(f"Pencere    : bugun + {args.days} gun")
    print(f"Cache      : {'ACIK' if use_cache else 'KAPALI (canli olcum)'}")

    if not settings.api_key_configured:
        item("API_FOOTBALL_KEY", "TANIMLI DEGIL", False)
        problem("0 Ortam", HATA, "API_FOOTBALL_KEY tanimli degil.")
        return 2
    item("API_FOOTBALL_KEY", f"tanimli ({len(settings.api_key)} karakter)", True)

    database = Database(settings)
    database.init_schema()
    client = ApiFootballClient(settings, database)
    budget = CallBudget(limit=args.budget)

    league_ids = [args.league] if args.league else settings.league_ids

    try:
        # ================================================== 1) canli baglanti
        head("1) Anahtar gecerli mi?  (canli /odds/bookmakers cagrisi)")
        try:
            bookmaker_rows = await client.bookmakers(budget=budget, use_cache=use_cache)
            item("HTTP durumu", client.history[-1].describe()[:90], True)
            item("Donen bookmaker sayisi", len(bookmaker_rows), len(bookmaker_rows) > 0)
            print("     Anahtar GECERLI: API 200 dondurdu ve veri geldi.")
        except AppError as exc:
            item("Baglanti", "BASARISIZ", False)
            problem("1 Baglanti", HATA, exc.user_message, describe_error(exc))
            print_summary(0, 0, budget, client)
            return 1

        # ================================================== 2) kota
        head("2) Kota  (cevap header'larindan)")
        quota = client.quota
        item("Canli cevaptan okundu mu", "evet" if quota.from_live_call else "HAYIR",
             quota.from_live_call)
        item("Gunluk limit", quota.daily_limit if quota.daily_limit is not None else "bilinmiyor")
        item("Gunluk kalan", quota.daily_remaining if quota.daily_remaining is not None else "bilinmiyor")
        item("Dakikalik kalan",
             quota.per_minute_remaining if quota.per_minute_remaining is not None else "bilinmiyor")
        if not quota.from_live_call:
            problem(
                "2 Kota", UYARI,
                "Kota header'i gorulmedi. Cevap cache'ten gelmis olabilir "
                "(--cache acikken) ya da API header gondermiyor.",
            )

        # ================================================== 3) Bet365
        head("3) Bet365 bookmaker kesfi")
        found = find_bookmaker(bookmaker_rows, settings.bookmaker_name)
        bookmaker_id = bookmaker_name = None
        if found:
            bookmaker_id, bookmaker_name = found.id, found.name
            item(f"{settings.bookmaker_name}", f"BULUNDU  id={found.id}  ad='{found.name}'", True)
        else:
            item(f"{settings.bookmaker_name}", "BULUNAMADI", False)
            sample = ", ".join(str(r.get("name")) for r in bookmaker_rows[:12])
            problem("3 Bookmaker", HATA,
                    f"{settings.bookmaker_name} listede yok. Bu plan onu saglamiyor. "
                    "Uygulama baska bookmaker'i onun yerine KULLANMAZ.",
                    f"donen ilk isimler: {sample}")

        # ================================================== 4) lig + sezon
        head("4) Lig ID'leri ve sezon tespiti")
        league_seasons: dict[int, int] = {}
        league_names: dict[int, str] = {}
        for league_id in league_ids:
            config_entry = settings.league(league_id)
            try:
                season, info = await client.current_season(
                    league_id, budget=budget, use_cache=use_cache
                )
            except AppError as exc:
                item(f"Lig {league_id}", "cagri basarisiz", False)
                problem("4 Lig", HATA, f"Lig {league_id}: {exc.user_message}",
                        describe_error(exc))
                continue

            if not info.get("found"):
                item(f"Lig {league_id}", "API'de bulunamadi", False)
                problem("4 Lig", HATA, f"Lig ID {league_id} API'de yok. ID yanlis olabilir.",
                        info.get("reason", ""))
                continue

            real_name = info.get("name")
            league_names[league_id] = str(real_name)
            item(f"Lig {league_id}", f"{real_name} | sezon {season} ({info.get('season_reason')})",
                 season is not None)

            if season is None:
                problem("4 Lig", HATA, f"Lig {league_id} icin sezon belirlenemedi.",
                        f"seasons kuyrugu: {info.get('seasons_tail')}")
            else:
                league_seasons[league_id] = season

            if config_entry and config_entry.expected_name:
                if normalise(real_name) != normalise(config_entry.expected_name):
                    problem("4 Lig", UYARI,
                            f"Lig {league_id} adi config'deki ile ayni degil "
                            f"(API: '{real_name}', config: '{config_entry.expected_name}'). "
                            "Sadece isim farki; ID dogruysa sorun degil.")
            if args.raw:
                print(f"     seasons (son 4): {info.get('seasons_tail')}")

        # ============================================ 4b) plan / sezon erisimi
        head("4b) Plan / sezon erisimi  (ucretsiz plan kisiti var mi?)")
        plan_blocked: dict[int, str] = {}
        if not league_seasons:
            item("Kontrol", "ATLANDI (sezon tespit edilemedi)", None)
        for league_id, season in list(league_seasons.items()):
            label = league_names.get(league_id, str(league_id))
            try:
                ok, message = await client.season_is_accessible(
                    league_id, season, budget=budget
                )
            except AppError as exc:
                item(f"{label} - sezon {season}", "kontrol edilemedi", False)
                problem("4b Plan", HATA, f"{label}: sezon erisimi olculemedi.",
                        describe_error(exc))
                continue

            if ok:
                item(f"{label} - sezon {season}", "ERISILEBILIR", True)
                continue

            item(f"{label} - sezon {season}", "ERISIM YOK (plan kisiti)", False)
            plan_blocked[league_id] = message or ""
            problem("4b Plan", HATA,
                    f"{label}: API planiniz {season} sezonuna erisim vermiyor. "
                    "Bu bir kod hatasi degil, abonelik kisitidir.",
                    message or "")

            access = await client.newest_accessible_season(
                league_id, [season, season - 1, season - 2, season - 3], budget=budget
            )
            newest = access.get("newest_accessible_season")
            if newest:
                item(f"{label} - erisilebilen en guncel sezon", newest, True)
                print(f"     NOT: {newest} sezonu eski veridir. Uygulama bunu "
                      f"'bugunun maclari' olarak GOSTERMEZ.")
            else:
                item(f"{label} - erisilebilen sezon", "BULUNAMADI", False)
            if args.raw:
                print(f"     denenen sezonlar: {access.get('tried')}")

        if plan_blocked and len(plan_blocked) == len(league_seasons):
            print()
            print("     SONUC: Hicbir ligin guncel sezonuna erisim yok.")
            print("     Uygulama bu durumda BOS LISTE degil, acik bir hata dondurur")
            print("     (/api/matches -> 502 season_not_accessible).")
            print("     Plan yukseltilirse KOD DEGISIKLIGI GEREKMEZ.")

        # ================================================== 5) fikstur
        head("5) Yaklasan maclar")
        print("     KONTROL: once 'next' ile (sezon/tarih filtresi YOK), sonra")
        print("     uygulamanin gercekte kullandigi league+season+from/to sorgusu.\n")

        today = datetime.now(tz=settings.tzinfo).date()
        horizon = today + timedelta(days=args.days)
        total_window = 0
        total_next = 0
        window_fixtures: dict[int, list[dict]] = {}

        for league_id, season in league_seasons.items():
            label = league_names.get(league_id, str(league_id))

            # --- 5a: sezon/tarih filtresiz kontrol ---
            next_rows: list[dict] = []
            try:
                next_rows = await client.fixtures(
                    league=league_id, next_count=3, budget=budget, use_cache=use_cache
                )
            except AppError as exc:
                problem("5 Fikstur", HATA, f"{label}: 'next' kontrolu basarisiz.",
                        describe_error(exc))

            total_next += len(next_rows)
            if next_rows:
                first = next_rows[0]
                api_season = (first.get("league") or {}).get("season")
                kickoff = (first.get("fixture") or {}).get("date", "")[:16]
                teams = first.get("teams") or {}
                item(f"{label} - siradaki mac",
                     f"{(teams.get('home') or {}).get('name')} - "
                     f"{(teams.get('away') or {}).get('name')}  {kickoff}  (sezon {api_season})",
                     True)
                if api_season is not None and api_season != season:
                    problem("5 Fikstur", HATA,
                            f"{label}: SEZON UYUSMAZLIGI. Bizim tespitimiz {season}, "
                            f"API'nin mac kaydinda sezon {api_season}. "
                            "league+season sorgusu bu yuzden bos donuyor.",
                            f"fixture_id={(first.get('fixture') or {}).get('id')}")
            else:
                item(f"{label} - siradaki mac", "YOK", False)

            # --- 5b: uygulamanin gercek sorgusu ---
            if league_id in plan_blocked:
                item(f"{label} - {args.days} gunluk pencere",
                     "ATLANDI (plan bu sezona erisim vermiyor)", False)
                continue
            try:
                rows = await client.fixtures(
                    league=league_id, season=season,
                    date_from=today.isoformat(), date_to=horizon.isoformat(),
                    budget=budget, use_cache=use_cache,
                )
            except AppError as exc:
                problem("5 Fikstur", HATA, f"{label}: pencere sorgusu basarisiz.",
                        describe_error(exc))
                continue

            window_fixtures[league_id] = rows
            total_window += len(rows)
            item(f"{label} - {args.days} gunluk pencere", f"{len(rows)} mac", len(rows) > 0)

            if not rows and next_rows:
                kickoff = (next_rows[0].get("fixture") or {}).get("date", "")[:10]
                problem("5 Fikstur", BILGI,
                        f"{label}: pencerede mac yok ama siradaki mac {kickoff}. "
                        "Lig arada (milli takim arasi / sezon arasi) olabilir.")

        if total_window == 0 and total_next == 0:
            problem("5 Fikstur", HATA,
                    "Hicbir ligde yaklasan mac yok - ne pencerede ne de 'next' ile. "
                    "Bu, lig ID'leri veya plan kapsami sorununa isaret eder.")
        elif total_window == 0 and total_next > 0:
            problem("5 Fikstur", UYARI,
                    f"Pencerede 0 mac ama 'next' {total_next} mac buluyor. "
                    "Ya pencere kisa ya da sezon/tarih sorgusu hatali.")

        # ================================================== 6) oranlar
        head("6) Bet365 oran kapsami")
        market_counter: Counter[str] = Counter()
        unknown_markets: Counter[str] = Counter()
        total_with_odds = 0
        proof_odds: dict[str, Any] | None = None

        if bookmaker_id is None:
            item("Oran kontrolu", "ATLANDI (Bet365 bulunamadi)", False)
        elif total_window == 0:
            item("Oran kontrolu", "ATLANDI (pencerede mac yok)", None)
        else:
            for league_id, rows in window_fixtures.items():
                if not rows:
                    continue
                label = league_names.get(league_id, str(league_id))
                season = league_seasons[league_id]
                days = sorted({str((r.get("fixture") or {}).get("date") or "")[:10] for r in rows})
                parsed_all: dict[int, dict] = {}
                raw_books_seen: Counter[str] = Counter()

                for day in days[: args.max_days_odds]:
                    if not day:
                        continue
                    try:
                        odds_rows = await client.odds(
                            league=league_id, season=season, date=day,
                            bookmaker=bookmaker_id, budget=budget, use_cache=use_cache,
                        )
                    except AppError as exc:
                        problem("6 Oran", HATA, f"{label} {day}: oran cagrisi basarisiz.",
                                describe_error(exc))
                        continue

                    for row in odds_rows:
                        for book in row.get("bookmakers") or []:
                            raw_books_seen[str(book.get("name"))] += 1
                        block = select_bookmaker(row, bookmaker_id)
                        if not block:
                            continue
                        bets = block.get("bets") or []
                        recognised = extract_markets(bets)
                        for bet in bets:
                            name = str(bet.get("name"))
                            market_counter[name] += 1
                            if match_market_name(name) is None:
                                unknown_markets[name] += 1
                        if proof_odds is None and recognised:
                            proof_odds = {"row": row, "parsed": recognised}

                    parsed_all.update(parse_odds_rows(odds_rows, bookmaker_id, bookmaker_name or ""))

                total_with_odds += len(parsed_all)
                item(f"{label}", f"{len(parsed_all)}/{len(rows)} macta Bet365 orani",
                     len(parsed_all) > 0)
                if not parsed_all and rows:
                    problem("6 Oran", UYARI,
                            f"{label}: {len(rows)} macin hicbirinde Bet365 orani yok. "
                            "Oranlar genelde maca ~7 gun kala aciliyor.",
                            f"o gunlerde gorulen bookmaker'lar: {dict(raw_books_seen)}"
                            if raw_books_seen else "hic bookmaker blogu donmedi")

        # ================================================== 7) market analizi
        head("7) Market analizi  (API mi veriyor, parser mi eliyor?)")
        if not market_counter:
            item("Bet365 bet blogu", "hic gorulmedi", False)
            if total_with_odds == 0 and total_window > 0 and bookmaker_id is not None:
                problem("7 Market", BILGI,
                        "Market gorulmedi cunku bu maclar icin Bet365 orani hic donmedi. "
                        "Bu bir PARSER sorunu DEGIL - API veri gondermemis.")
        else:
            item("API'nin gonderdigi bet turu", len(market_counter), True)
            recognised_names = 0
            for name, count in market_counter.most_common(30):
                key = match_market_name(name)
                flag = f"-> {key}" if key else "TANINMIYOR"
                if key:
                    recognised_names += 1
                print(f"     {count:>4} x  [{flag:<12}]  {name}")
            item("Parser'in tanidigi bet turu", f"{recognised_names}/{len(market_counter)}",
                 recognised_names > 0)
            if recognised_names == 0:
                problem("7 Market", HATA,
                        "API market gonderiyor ama parser HICBIRINI tanimiyor. "
                        "Bu BIZIM KODUMUZUN sorunu - odds_parser._BET_NAMES guncellenmeli.",
                        f"gelen adlar: {list(market_counter)[:8]}")
            elif unknown_markets:
                problem("7 Market", BILGI,
                        f"{len(unknown_markets)} bet turu taninmiyor ve kullanilmiyor "
                        "(zararsiz - sadece o marketler gosterilmez).",
                        ", ".join(list(unknown_markets)[:10]))

        # ================================================== 8) kanit
        head("8) Ham veri kaniti")
        proof_fixture = None
        for rows in window_fixtures.values():
            if rows:
                proof_fixture = rows[0]
                break
        if proof_fixture is None and total_next > 0:
            print("     (pencerede mac yok; 'next' sonucundan kanit gosteriliyor)")

        if proof_fixture:
            fixture = proof_fixture.get("fixture") or {}
            teams = proof_fixture.get("teams") or {}
            league = proof_fixture.get("league") or {}
            item("Gercek fixture", f"id={fixture.get('id')}  "
                 f"{(teams.get('home') or {}).get('name')} - "
                 f"{(teams.get('away') or {}).get('name')}", True)
            item("  lig / sezon / tarih",
                 f"{league.get('name')} / {league.get('season')} / {str(fixture.get('date'))[:16]}")
            if args.raw:
                print(short_json(proof_fixture))
        else:
            item("Gercek fixture", "ALINAMADI", False)

        if proof_odds:
            row = proof_odds["row"]
            parsed = proof_odds["parsed"]
            item("Gercek Bet365 orani",
                 f"fixture={((row.get('fixture') or {}).get('id'))}  "
                 f"marketler={list(parsed)}", True)
            for market, values in parsed.items():
                print(f"       {market}: {values}")
            if args.raw:
                print(short_json(select_bookmaker(row, bookmaker_id)))
        else:
            item("Gercek Bet365 orani", "ALINAMADI", False)

        # ================================================== ozet
        print_summary(total_window, total_with_odds, budget, client)

        if args.raw:
            head("EK) Tum API cagrilarinin ham dokumu")
            for index, record in enumerate(client.history, 1):
                print(f"  {index:>2}. {record.describe()}")

        return 0 if not any(p["severity"] == HATA for p in problems) else 1
    finally:
        await client.aclose()


def print_summary(total_window: int, total_with_odds: int,
                  budget: CallBudget, client: ApiFootballClient) -> None:
    head("OZET")
    item("Pencerede yaklasan mac", total_window, total_window > 0)
    item("Bet365 orani olan mac", total_with_odds, total_with_odds > 0)
    item("Harcanan API cagrisi", budget.used)
    item("Kalan gunluk kota",
         client.quota.daily_remaining if client.quota.daily_remaining is not None else "bilinmiyor")

    plan_errors = [p for p in problems if p["section"].startswith("4b")]
    if plan_errors:
        print()
        print("  ! PLAN KISITI: API-Football aboneliginiz guncel sezona erisim vermiyor.")
        print("    Bu bir kod hatasi degildir. Uygulama sahte veri uretmez; bu durumu")
        print("    ekranda acikca gosterir. Plan yukseltilince kod degismeden calisir.")

    errors = [p for p in problems if p["severity"] == HATA]
    warnings = [p for p in problems if p["severity"] == UYARI]
    infos = [p for p in problems if p["severity"] == BILGI]
    item("HATA / UYARI / BILGI", f"{len(errors)} / {len(warnings)} / {len(infos)}",
         len(errors) == 0)

    if problems:
        head("SORUN LISTESI")
        for entry in problems:
            print(f"  #{entry['no']} [{entry['severity']}] ({entry['section']})")
            print(f"      {entry['message']}")
            if entry["evidence"]:
                print(f"      kanit: {entry['evidence'][:400]}")
    else:
        print("\nHicbir sorun bulunamadi.")

    if errors:
        print("\nHATA isaretli maddeler cozulmeden deploy etmeyin.")
    elif warnings or infos:
        print("\nHata yok. UYARI/BILGI maddeleri genelde takvimden kaynaklanir "
              "(lig arasi, oranlarin henuz acilmamis olmasi).")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="API-Football canli teshis")
    parser.add_argument("--days", type=int, default=7,
                        help="kac gunluk pencere taransin (varsayilan 7)")
    parser.add_argument("--league", type=int, default=None, help="yalnizca bu lig ID'si")
    parser.add_argument("--raw", action="store_true", help="ham JSON dokumu goster")
    parser.add_argument("--cache", action="store_true",
                        help="cache kullan (varsayilan: kapali, canli olcum)")
    parser.add_argument("--budget", type=int, default=80, help="azami API cagrisi")
    parser.add_argument("--max-days-odds", type=int, default=4,
                        help="oran sorgusu icin azami gun sayisi (kota korumasi)")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))
