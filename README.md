# MACANALİZ PRO

Futbol maç ve **Bet365** oran analizi. FastAPI backend + vanilla JS frontend,
API-Football v3 verisiyle çalışır.

> Bu uygulama kazanç garantisi vermez, kesin sonuç iddia etmez ve **veri uydurmaz**.
> API'de olmayan hiçbir oran, istatistik veya geçmiş değer üretilmez — veri yoksa
> ekranda "veri yok" yazar.

---

## İçindekiler

1. [Ne yapar](#ne-yapar)
2. [Tasarım kararları](#tasarım-kararları)
3. [Hızlı başlangıç](#hızlı-başlangıç)
4. [Environment variables](#environment-variables)
5. [API anahtarı ve yetenek doğrulaması](#api-anahtarı-ve-yetenek-doğrulaması)
6. [Render'a deploy](#rendera-deploy)
7. [GitHub Pages'e deploy](#github-pagese-deploy)
8. [Veritabanı ve snapshot sistemi](#veritabanı-ve-snapshot-sistemi)
9. [API endpointleri](#api-endpointleri)
10. [Testler ve deploy öncesi kontrol](#testler-ve-deploy-öncesi-kontrol)
11. [Sorun giderme](#sorun-giderme)
12. [Proje yapısı](#proje-yapısı)

---

## Ne yapar

- Seçilen liglerde yaklaşan maçları listeler (Türkiye saatiyle).
- Her maç için **yalnızca Bet365** oranını çeker. Bet365 yoksa maç açıkça
  "Bet365 oranı yok" olarak işaretlenir; başka bookmaker onun yerine konmaz.
- Piyasadan **bağımsız** bir Dixon-Coles gol modeliyle MS / İY / KG / 2.5 Üst-Alt
  olasılıklarını üretir.
- Bet365 oranından marj arındırılmış piyasa olasılığını hesaplar ve modelle
  karşılaştırır (**model / piyasa farkı**).
- Her veri yenilemesinde gördüğü oranı zaman damgasıyla kaydeder; böylece
  zamanla **kendi oran geçmişini** oluşturur.
- Şeffaf bir **güven skoru** ve ondan ayrı bir **veri kalitesi** skoru üretir.
- "Bugünün Akıllı İlk 5"i `(model/piyasa farkı) × güven` sıralamasıyla seçer.

---

## Tasarım kararları

Bunlar bilinçli tercihlerdir; değiştirmeden önce nedenini okuyun.

**1. Model Bet365 oranını girdi olarak kullanmaz.**
Kullansaydı "model / piyasa farkı" döngüsel bir sayı olurdu: piyasayı modele
verip sonra piyasayla karşılaştırmış olurduk. Model yalnızca gol verisi, form,
ev/deplasman ayrımı ve H2H kullanır.

**2. "Açılış oranı" ve "T-15" etiketleri yoktur.**
API-Football pre-match oranlarını ~7 günlük pencerede sunar ve ~3 saatte bir
günceller. Yani maça 15 dakika kala ayrı bir değer yoktur ve bizim geçmişimiz
ilk kayıt anında başlar. Uygulama yalnızca gerçekten kaydedilmiş anları
gösterir: `İlk kaydedilen`, `Güncel`, `Başlama saatine en yakın kayıt`.

**3. Sakatlık verisi gol beklentisini değiştirmez.**
Oyuncu bazlı ağırlık verimiz yok; "3 sakat = %4 düşür" gibi bir katsayı
uydurmak savunulamaz. Sakatlık yalnızca güven ve veri kalitesi skorlarını
etkiler.

**4. Büyük model/piyasa sapması güveni düşürür.**
20+ puanlık bir sapma genelde gerçek bir avantaj değil, eksik bilgi (kadro
haberi, motivasyon) işaretidir. Fark ayrıca gösterilir ama güveni yükseltmez.

**5. Bet365 ID'si koda gömülmez.**
Uygulama `/odds/bookmakers` çağırıp isimden gerçek ID'yi bulur ve cache'ler.

**6. Frontend hiçbir değer üretmez.**
`data.odds?.home || 0` gibi tek bir satır bile yoktur. Backend `null`
gönderdiyse ekranda "veri yok" yazar. `innerHTML` kullanılmaz (HTML injection'a
kapalı).

**7. API hatasında demo veriye düşülmez.**
Hata açıkça gösterilir. Sessiz demo fallback, "ekranda maç var ama API'de yok"
sorununun kaynağıdır.

**8. Sezon `current` bayrağına körü körüne güvenilmez.**
Sezon seçimi önce `start <= bugün <= end` kuralıyla yapılır; ancak bu bir sonuç
vermezse `current: true` bayrağına, o da yoksa en büyük sezon yılına düşülür.
Sebep: API'nin `current` bayrağı sezon geçişlerinde gecikebiliyor ve yanlış
sezonla yapılan `/fixtures?league&season&from&to` çağrısı **hata vermez,
sessizce 0 maç döndürür** — ekranda "bugün maç yok" gibi görünen sinsi bir hata.

**9. Plan sezon kısıtı açıkça gösterilir, gizlenmez.**
API-Football'ın ücretsiz planı güncel sezon verisini vermiyor; istek HTTP 200
dönüp gövdede `{"plan": "Free plans do not have access to this season..."}`
hatası taşıyor. Uygulama bunu:

- ayrı bir hata tipine (`season_not_accessible`) çevirir,
- **sağlayıcının kendi cümlesini** kullanıcıya aynen gösterir,
- **boş liste döndürmez** — "bugün maç yok" ile "plan izin vermiyor" karışmaz,
- erişilebilen en güncel sezonu **tespit edip raporlar** ama onu "bugünün
  maçları" olarak **göstermez**,
- sahte maç, sahte oran veya tahmin üretmez.

Sezon her zaman API'den tespit edildiği için **plan yükseltilince kod
değişikliği gerekmez**; uygulama en geç `TTL_SEASON_ACCESS` (varsayılan 1 saat)
içinde güncel sezona geçer.

**10. Backend kimliği doğrulanır (sözleşme el sıkışması).**
`/api/config` bir `api_contract` işareti döndürür (`macanaliz-pro/1`); frontend
açılışta bunu doğrular. Uymuyorsa **"Bu adreste farklı bir uygulama çalışıyor"**
der ve bulunan servisin adını/sürümünü yazar. Sebep: bir kez Render'da başka bir
uygulama çalışıyordu ve hata ekranda anlamsız bir `Sunucu 400 döndürdü` olarak
görünüyordu.

**11. "Backend'e ulaşılamadı" yalnızca gerçekten cevap gelmediğinde yazılır.**
Sunucu 400/404/422/502 gibi anlamlı bir cevap verdiyse başlık gerçek durumu
söyler ve hata kutusunda **istenen URL, HTTP kodu, `Request ID` ve gövde
özeti** gösterilir. Backend her cevaba `X-Request-ID` ekler ve aynı id'yi
loglar — tarayıcıdaki hata, Render logundaki satırla birebir eşleşir.

**12. Yapılandırma hataları uyarıya dönüştürülmez.**
Hatalar ikiye ayrılır (`errors.py` içindeki `fatal` alanı):

- **fatal** — API anahtarı yok / geçersiz / plan kapsamıyor. Hiçbir şey
  çalışmaz, bu yüzden istek 503/502 ile başarısız olur ve kullanıcı gerçek
  sebebi görür. (Aksi halde bu durum ekranda "bu filtrelerde maç bulunamadı"
  gibi görünüyordu — tam olarak kaçındığımız şey.)
- **fatal değil** — kota bitti, tek bir lig çekilemedi, Bet365 yok. Elde edilen
  gerçek veri gösterilir, sorun uyarı olarak bildirilir. Ancak *hiçbir* lig
  çekilemediyse sebep yine yukarı fırlatılır; boş liste + uyarı gösterilmez.

---

## Hızlı başlangıç

```bash
git clone <repo-url>
cd macanaliz-pro

python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt

cp .env.example .env        # Windows: copy .env.example .env
# .env içine API_FOOTBALL_KEY yazın

# Backend
uvicorn backend:app --reload --port 8000

# Frontend (ayrı terminal)
python -m http.server 5500
# tarayıcıda http://localhost:5500 açın, sol panele http://localhost:8000 yazın
```

> `.env` dosyası otomatik okunmaz. Windows PowerShell'de:
> `$env:API_FOOTBALL_KEY="..."` , macOS/Linux'ta: `export API_FOOTBALL_KEY="..."`

---

## Environment variables

| Değişken | Zorunlu | Varsayılan | Açıklama |
|---|---|---|---|
| `API_FOOTBALL_KEY` | **evet** | — | API-Football / API-Sports anahtarı. Yalnızca backend'de okunur. |
| `DATABASE_URL` | önerilen | — | PostgreSQL bağlantısı. Yoksa SQLite kullanılır ve "geçici" uyarısı gösterilir. |
| `ALLOWED_ORIGINS` | evet (prod) | `*` | CORS. GitHub Pages adresiniz, virgülle ayrılmış. |
| `PORT` | Render verir | `8000` | Dinlenecek port. |
| `LEAGUE_IDS` | hayır | 39,140,135,78,61,203 | Takip edilecek lig ID'leri. |
| `TIMEZONE` | hayır | `Europe/Istanbul` | Görüntüleme saat dilimi. |
| `LOG_LEVEL` | hayır | `INFO` | Log seviyesi. |
| `SNAPSHOT_ENABLED` | hayır | `true` | Periyodik snapshot toplayıcı. |
| `SNAPSHOT_INTERVAL_MINUTES` | hayır | `180` | Snapshot aralığı (sağlayıcı ~3 saatte bir güncelliyor). |
| `SNAPSHOT_HORIZON_DAYS` | hayır | `7` | Kaç gün ileriye kadar snapshot alınacağı. |
| `MAX_CALLS_PER_REFRESH` | hayır | `400` | Tek yenilemede harcanabilecek azami API çağrısı. |
| `QUOTA_RESERVE` | hayır | `40` | Bu kotanın altına inince derin analiz durur. |
| `TTL_SEASON_ACCESS` | hayır | `3600` | Plan sezon erişimi ne sıklıkla yeniden kontrol edilsin (plan yükseltmesi bu sürede otomatik algılanır). |
| `TOP5_MIN_DATA_QUALITY` | hayır | `50` | Top-5'e girmek için gereken en düşük veri kalitesi. |
| `MODEL_RHO` | hayır | `-0.05` | Dixon-Coles düşük skor düzeltmesi. |
| `MODEL_FORM_WEIGHT` | hayır | `0.35` | Sezonluk güç ile son form harmanı. |
| `CONF_W_*`, `DQ_W_*` | hayır | — | Güven / veri kalitesi bileşen ağırlıkları. |

Tam liste için `config.py`.

---

## API anahtarı ve yetenek doğrulaması

Anahtar **hiçbir zaman** koda yazılmaz, GitHub'a gönderilmez, loglanmaz ve HTTP
cevabına konmaz. `.gitignore` `.env` dosyasını kapsar.

Anahtarınızla gerçekten neyin çalıştığını ölçmek için:

```bash
python tools/probe_api.py              # 7 günlük pencere
python tools/probe_api.py --days 14    # pencereyi genişlet
python tools/probe_api.py --raw        # ham fixture + odds + tüm API çağrıları
python tools/probe_api.py --league 39  # tek lig
```

Rapor sırayla şunları ölçer:

1. **Anahtar geçerli mi** — canlı `/odds/bookmakers` çağrısı, ham HTTP kodu ile.
2. **Kota** — canlı cevabın header'ından okunup okunmadığı ayrıca belirtilir.
3. **Bet365** — listede var mı, gerçek ID'si kaç.
4. **Lig + sezon** — API'nin döndürdüğü gerçek ad, seçilen sezon ve **hangi
   kurala göre seçildiği** (tarih aralığı / `current` bayrağı / son çare).
4b. **Plan / sezon erişimi** — aboneliğiniz güncel sezonun verisine erişim
   veriyor mu; vermiyorsa sağlayıcının mesajı ve erişilebilen en güncel sezon.
5. **Fikstür** — önce `next` ile (sezon ve tarih filtresi **olmadan**), sonra
   uygulamanın gerçekte kullandığı `league+season+from/to` sorgusu. İkisi
   uyuşmuyorsa sezon uyuşmazlığı olarak raporlanır.
6. **Bet365 oran kapsamı** — kaç maçta oran geldiği.
7. **Market analizi** — API'nin gönderdiği her bet türü ve parser'ın onu tanıyıp
   tanımadığı. "0 market" sonucunun API'den mi parser'dan mı geldiğini kesin
   ayırır.
8. **Ham kanıt** — gerçek bir fixture ve gerçek Bet365 oranları.

Sonda numaralı bir **sorun listesi** var: her madde HATA / UYARI / BİLGİ olarak
işaretli ve yanında ham HTTP kodu ile API'nin kendi hata gövdesi yazılı.

Cache varsayılan olarak **kapalıdır** — teşhis aracı canlı API'yi ölçmelidir.
(Cache'ten dönen cevapta rate-limit header'ı olmadığı için kota "bilinmiyor"
görünüyordu; bu bir ölçüm hatasıydı, API sorunu değil.)

**Çıktı anahtarı içermez**, güvenle paylaşabilirsiniz.

Bu adımı atlamayın: yanlış bir lig ID'si veya Bet365'in planınızda olmaması,
uygulamayı sessizce boş bırakır.

---

## Render'a deploy

**Blueprint ile (kolay):** Render'da *New → Blueprint* → bu repoyu seçin.
`render.yaml` her şeyi ayarlar; sadece environment variable'ları girin.

**Elle:**

| Ayar | Değer |
|---|---|
| Language | Python 3 |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn backend:app --host 0.0.0.0 --port $PORT` |
| Health Check Path | `/api/health` |

Environment:

```
API_FOOTBALL_KEY = <anahtarınız>
DATABASE_URL     = <postgres bağlantı adresi>
ALLOWED_ORIGINS  = https://KULLANICI.github.io
PYTHON_VERSION   = 3.11.9
```

**Ücretsiz katman uyarıları:**

- Servis 15 dakika trafiksizlikte uyur; ilk istek ~1 dakika sürer. Frontend bunu
  "backend uyanıyor" olarak gösterir, hata saymaz.
- Dosya sistemi geçicidir ve ücretsiz instance'a persistent disk takılamaz.
  **SQLite kullanırsanız snapshot geçmişi her uyanışta silinir.**
- Render'ın ücretsiz PostgreSQL'i oluşturulduktan 30 gün sonra silinir. Kalıcı
  geçmiş için harici bir Postgres (Neon, Supabase) veya ücretli plan kullanın.
- Uygulama uykudayken snapshot alınamaz. Düzenli geçmiş istiyorsanız
  cron-job.org / UptimeRobot ile `/api/health` adresini ~10 dakikada bir
  pingleyin.

---

## GitHub Pages'e deploy

1. Repo → *Settings → Pages* → Source: `Deploy from a branch`, branch `main`,
   folder `/ (root)`.
2. `https://KULLANICI.github.io/macanaliz-pro/` adresini açın.
3. Sol paneldeki **Backend URL** alanına Render adresinizi yazın
   (`https://...onrender.com`). Değer `localStorage`'da saklanır.
4. Render'da `ALLOWED_ORIGINS` içine `https://KULLANICI.github.io` ekleyin.

> Backend URL **https** olmalı. GitHub Pages HTTPS servis eder; http adres
> tarayıcı tarafından engellenir (mixed content). Uygulama bu durumu tespit edip
> açık bir hata gösterir.

---

## Veritabanı ve snapshot sistemi

`DATABASE_URL` varsa PostgreSQL, yoksa SQLite kullanılır — kod aynıdır, ORM yok.

Tablolar: `api_cache`, `bookmakers`, `leagues`, `teams`, `fixtures`,
`odds_snapshots`, `analysis_cache`, `predictions_log`.

Her snapshot şunları taşır: `fixture_id`, `bookmaker_id`, `bookmaker_name`,
`market`, `home_odds`, `draw_odds`, `away_odds`, `extra`, `captured_at`,
`kickoff_utc`, `source`.

Snapshot yalnızca oran **değiştiyse** veya son kayıttan bu yana yeterli süre
geçtiyse yazılır. En az iki kayıt yoksa oran hareketi **hesaplanmaz** —
"yalnızca tek snapshot var" denir.

`predictions_log` tablosu her modeli ve piyasa olasılığını kaydeder. Maçlar
oynandıkça `/api/calibration` gerçek isabet oranını ve Brier skorunu raporlar;
böylece güven skoru bir iddia değil, ölçülmüş bir şey olur.

---

## API endpointleri

| Endpoint | Açıklama |
|---|---|
| `GET /api/health` | Sağlık kontrolü (Render health check). |
| `GET /api/config` | Frontend yapılandırması + `api_contract` el sıkışması (sır içermez). |
| `GET /api/status` | Backend / anahtar / API / Bet365 / DB / zamanlayıcı durumu. |
| `GET /api/quota` | Kalan API kotası. |
| `GET /api/capabilities` | Bu anahtarla gerçekte ne yapılabildiği. |
| `GET /api/bookmakers` | Bulunan bookmaker'lar ve Bet365 eşleşmesi. |
| `GET /api/leagues` | Ligler + API'den gelen güncel sezon. |
| `GET /api/fixtures` | Hafif fikstür listesi. |
| `GET /api/matches` | Analizli maç listesi (ana ekran). |
| `GET /api/top5` | Bugünün Akıllı İlk 5. |
| `GET /api/highlights` | Öne çıkan analiz kartları. |
| `GET /api/match/{id}` | Tam maç analizi. |
| `GET /api/analysis/{id}` | `match` ile aynı (uyumluluk). |
| `GET /api/odds/{id}` | Bet365 oranları + hareket. |
| `GET /api/predictions/{id}` | Model + API tahmini + fark. |
| `GET /api/form/{id}` | Form ve ev/deplasman blokları. |
| `GET /api/h2h/{id}` | Karşılıklı maçlar. |
| `GET /api/snapshots/{id}` | Kaydedilmiş oran snapshot'ları. |
| `POST /api/snapshots/capture` | Snapshot turunu elle tetikler. |
| `GET /api/calibration` | Model kalibrasyon raporu. |

Ortak sorgu parametreleri: `leagues` (virgüllü ID), `days`, `from`, `to`.

Hata formatı:

```json
{ "error": { "code": "api_key_missing", "message": "API anahtarı bulunamadı. ..." } }
```

---

## Testler ve deploy öncesi kontrol

```bash
# Tüm testler (stdlib unittest — ek kurulum gerekmez)
python -m unittest discover -s tests -t .

# veya
pytest tests/

# Deploy öncesi 20 maddelik kontrol listesi
python tools/preflight.py

# Canlı deploy'u da doğrula (madde 19) - Render adresinizi verin
BACKEND_URL=https://xxxx.onrender.com python tools/preflight.py
# Windows: set BACKEND_URL=https://xxxx.onrender.com && python tools\preflight.py
```

`preflight.py` şunları doğrular: backend import, endpoint listesi, `/api/health`,
`/api/status` (checks alanı dolu mu), anahtar yokken 503 dönüyor mu, canlı API +
Bet365 keşfi, oran ayrıştırma (başka bookmaker sızmıyor mu), analiz matematiği,
predictions ayrıştırma, veritabanı + snapshot, yapılandırma hatalarının
yutulmaması, bookmaker ID'sinin gömülü olmaması, frontend dosyaları + güvenli
DOM, demo fallback olmaması, CORS, sahte veri üreteci olmaması, sır sızıntısı,
TODO kalmaması ve test paketi.

**Madde 19 (canlı deploy doğrulaması)** `BACKEND_URL` verilirse çalışır: canlı
adresteki servisin gerçekten bu kod olduğunu `api_contract` ile doğrular.
`SKIP: 0` hedefi için hem `API_FOOTBALL_KEY` hem `BACKEND_URL` tanımlı olmalı.

Test paketi **temiz bir ortamda** alt süreç olarak çalıştırılır; preflight'ın
kendi ayarları (`SNAPSHOT_ENABLED=false` gibi) testlere sızmaz. Bir test düşerse
preflight hangi testlerin düştüğünü adlarıyla yazar.

**Bir madde bile FAIL ise deploy etmeyin.**

---

## Sorun giderme

| Belirti | Sebep / çözüm |
|---|---|
| "API anahtarı bulunamadı" | Sunucuda `API_FOOTBALL_KEY` tanımlı değil. Render → Environment. |
| "API anahtarı geçersiz" | Anahtar yanlış veya süresi dolmuş. |
| "API-Football kota sınırına ulaşıldı" | Günlük kota bitti. `/api/quota` ile kontrol edin. |
| "Bet365 bookmaker listesinde bulunamadı" | Planınız Bet365 sağlamıyor. Uygulama başka bookmaker'ı **yerine koymaz**. `python tools/probe_api.py` ile doğrulayın. |
| Maç listesi boş | Seçilen lig/tarihte maç yok, ya da lig ID'si yanlış. `/api/leagues` uyarılarına bakın. |
| Tüm maçlarda "Bet365 oranı yok" | Oranlar genelde maçtan ~7 gün önce açılır. Daha yakın bir tarih aralığı seçin. |
| Model "yeterli veri yok" diyor | Sezon başında bitmiş maç azdır. `MODEL_MIN_MATCHES` bu eşiği belirler. |
| İlk yarı "yeterli veri yok" | Takımların ilk yarı skoru bilinen maç sayısı `MODEL_MIN_HT_MATCHES` altında. |
| Oran hareketi boş | Henüz iki snapshot birikmedi. Geçmiş oran uydurulmaz. |
| Frontend boş, konsolda CORS hatası | `ALLOWED_ORIGINS` içine GitHub Pages adresinizi ekleyin. |
| İlk istek çok yavaş | Render ücretsiz servisi uyanıyor (~1 dk). Normaldir. |
| Snapshot geçmişi kayboluyor | SQLite + Render ücretsiz katman. `DATABASE_URL` tanımlayın. |
| probe: "Pencerede 0 maç ama next N maç buluyor" | Sezon veya tarih penceresi sorunu. probe sezon uyuşmazlığını ayrıca raporlar. |
| probe: "SEZON UYUŞMAZLIĞI" | API'nin `current` bayrağı yanlış sezonu işaret ediyor. Kod artık tarih aralığını öncelikler; yine görüyorsanız `--raw` çıktısını paylaşın. |
| probe: kota "bilinmiyor" | Cevap cache'ten gelmiş. probe artık varsayılan olarak cache'siz çalışır. |
| "Bu adreste farklı bir uygulama çalışıyor" | Render'a bu repo deploy edilmemiş; orada başka bir servis çalışıyor. `BACKEND_URL=... python tools/preflight.py` madde 19 bunu doğrular. |
| "Backend HTTP 404 döndürdü" | Backend ayakta ama istenen adresi tanımıyor — büyük ihtimalle eski/farklı bir sürüm. Hata kutusundaki URL satırına bakın. |
| Bir hatayı Render logunda bulmak | Hata kutusundaki `Request ID` değerini Render loglarında aratın; her istek `rid=<id> GET /api/... -> 200 (12 ms)` biçiminde loglanır. |
| `/api/matches` 502 `season_not_accessible` | **Plan kısıtı** — kod hatası değil. Ücretsiz plan güncel sezona erişim vermiyor. Üst çubuktaki "Sezon / plan" rozeti ve `/api/status` içindeki `plan` bloğu durumu gösterir. Plan yükseltilince kod değişmeden çalışır. |
| Üst çubukta "2026 ERİŞİM YOK" | Aynı sebep. `python tools/probe_api.py` madde 4b erişilebilen en güncel sezonu da yazar. |
| `/api/matches` 503 "API anahtarı bulunamadı" | Doğru davranış: yapılandırma hatası boş listeye dönüştürülmez. Sunucuda `API_FOOTBALL_KEY` tanımlayın. |
| "Bağlantı Durumu" paneli boş | `/api/status` `checks` alanı dönmüyor. Güncel sürümde düzeltildi; `python tools/preflight.py` madde 4 bunu kontrol eder. |

---

## Proje yapısı

```
.
├── index.html              Ana sayfa
├── css/styles.css          Dark analytics teması, responsive
├── js/
│   ├── config.js           Varsayılanlar, storage anahtarları
│   ├── format.js           Biçimlendirme + güvenli DOM yardımcıları
│   ├── api.js              Backend istemcisi (cold start, hata haritası)
│   ├── ui.js               Render katmanı
│   └── app.js              Akış / olay bağlama
│
├── backend.py              FastAPI: yönlendirme, doğrulama, hata çevirisi
├── config.py               Merkezi yapılandırma + logging (sır redaksiyonu)
├── errors.py               Hata tipleri + Türkçe mesajlar
├── models.py               Backend → frontend sözleşmesi (her alan source'lu)
├── database.py             SQLite / PostgreSQL veri katmanı
├── api_client.py           API-Football istemcisi (cache, paging, kota)
├── bookmakers.py           Bet365 runtime keşfi
├── odds_parser.py          Market normalizasyonu (bet ADIYLA eşleşme)
├── analysis_engine.py      Dixon-Coles modeli + skorlar (saf stdlib)
├── snapshots.py            Oran snapshot yazma/okuma
├── services.py             Orkestrasyon, iki kademeli kota stratejisi
├── scheduler.py            Periyodik snapshot döngüsü (asyncio)
│
├── tools/
│   ├── probe_api.py        Canlı API teşhis aracı (--raw, --days, --league)
│   └── preflight.py        Deploy öncesi 20 maddelik kontrol
│
└── tests/                  172 test (unittest / pytest)
```

---

## Lisans ve sorumluluk

Bu yazılım analiz amaçlıdır. Bahis kararlarınızın sorumluluğu size aittir.
Model çıktıları olasılıktır, kesinlik değildir; "kesin", "garanti", "banko" gibi
ifadeler uygulamada bilinçli olarak kullanılmaz.
