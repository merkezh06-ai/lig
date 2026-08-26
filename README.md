# MACANALİZ PRO STABLE
## Render
Build: `pip install -r requirements.txt`
Start: `uvicorn backend:app --host 0.0.0.0 --port $PORT`
Environment Variable: `API_FOOTBALL_KEY`

Frontend `index.html` GitHub Pages'te çalışır. Backend URL alanına Render servis URL'si girilir.

Bet365 tek bookmaker'dır; fallback yoktur.

Önemli: API-Football Free planında sezon kapsamı sınırlıdır. Kod bunu aşamaz. Güncel sezon erişimi hesabınızda yoksa Pro veya başka veri sağlayıcısı gerekir. API-Football pre-match odds geçmişi sınırlıdır; gerçek T-15 timeline için snapshot toplamak gerekir.
