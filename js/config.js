/**
 * Frontend yapilandirmasi.
 *
 * Backend URL koda gomulmez: ayar panelinden degistirilir ve localStorage'da
 * saklanir. Buradaki deger yalnizca ILK acilistaki varsayilandir.
 */
window.MACANALIZ_CONFIG = {
  // Kendi Render adresinizi buraya yazabilir veya arayuzden girebilirsiniz.
  defaultBackendUrl: "http://localhost:8000",

  storageKeys: {
    backendUrl: "macanaliz.backendUrl",
    leagueIds: "macanaliz.leagueIds",
    dayRange: "macanaliz.dayRange",
    autoRefresh: "macanaliz.autoRefresh"
  },

  // Render ucretsiz servisi 15 dk trafiksizlikte uyur ve uyanmasi ~1 dk surer.
  // Bu yuzden ilk istek icin uzun timeout kullanilir.
  requestTimeoutMs: 95000,
  coldStartHintAfterMs: 6000,

  autoRefreshMs: 5 * 60 * 1000,

  bookmakerName: "Bet365",

  // Backend ile sozlesme surumu. /api/config bunu dondurmezse veya farkli
  // donerse frontend "bu adreste baska bir uygulama var" der.
  apiContract: "macanaliz-pro/1",

  // Model olasiligi renk esikleri (yalnizca gorsel).
  strongProbability: 0.55,
  goodDataQuality: 70,
  weakDataQuality: 45
};
