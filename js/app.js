/**
 * Uygulama akisi: ayarlar -> backend -> render.
 *
 * Cok onemli: API basarisiz olursa SESSIZCE DEMO VERIYE GECILMEZ. Hata
 * ekranda acikca gosterilir.
 */
(function (global) {
  "use strict";

  var config = global.MACANALIZ_CONFIG;
  var Api = global.Api;
  var UI = global.UI;
  var F = global.Fmt;

  var state = {
    apiState: "unknown",
    bet365: null,
    bookmakerName: config.bookmakerName,
    database: null,
    databasePersistent: null,
    quota: null,
    plan: null,
    lastUpdate: null,
    version: null,
    loading: false,
    autoTimer: null
  };

  // ---------------------------------------------------------------- ayarlar
  function elById(id) { return document.getElementById(id); }

  function readFilters() {
    var dayRange = elById("day-range").value;
    var params = { leagues: elById("league-ids").value.trim() };
    if (dayRange === "custom") {
      var from = elById("date-from").value.trim();
      var to = elById("date-to").value.trim();
      if (from) params.from = from;
      if (to) params.to = to;
      if (!from && !to) params.days = 1;
    } else {
      params.days = parseInt(dayRange, 10) || 1;
    }
    return params;
  }

  function persistSettings() {
    global.Storage2.write(config.storageKeys.leagueIds, elById("league-ids").value.trim());
    global.Storage2.write(config.storageKeys.dayRange, elById("day-range").value);
    global.Storage2.write(config.storageKeys.autoRefresh, elById("auto-refresh").checked ? "1" : "0");
  }

  function restoreSettings() {
    elById("backend-url").value = Api.baseUrl || "";
    elById("league-ids").value = global.Storage2.read(config.storageKeys.leagueIds, "");
    var day = global.Storage2.read(config.storageKeys.dayRange, "7");
    if (elById("day-range").querySelector('option[value="' + day + '"]')) {
      elById("day-range").value = day;
    }
    elById("auto-refresh").checked = global.Storage2.read(config.storageKeys.autoRefresh, "0") === "1";
    toggleCustomRange();
  }

  function toggleCustomRange() {
    elById("custom-range").hidden = elById("day-range").value !== "custom";
  }

  function setBusy(busy, label) {
    state.loading = busy;
    ["btn-load", "btn-test", "btn-refresh"].forEach(function (id) {
      elById(id).disabled = busy;
    });
    elById("btn-load").textContent = busy ? (label || "Yükleniyor…") : "Maçları Getir";
  }

  function describeError(error) {
    return error && error.message ? error.message : "Bilinmeyen hata.";
  }

  /**
   * Hata basligi. KURAL: backend anlamli bir HTTP cevabi dondurduyse
   * "Backend'e ulasilamadi" DENMEZ - bu, gercek sebebi gizliyordu.
   */
  function errorTitle(error) {
    if (!error) return "Bilinmeyen hata";
    if (error.transport) return "Backend'e ulaşılamadı";
    if (error.code === "season_not_accessible") {
      return "API-Football Free plan kısıtı";
    }
    if (error.code === "contract_mismatch") return "Bu adreste farklı bir uygulama çalışıyor";
    if (error.status === 404) return "Backend bu adresi tanımıyor (HTTP 404)";
    if (error.status === 401 || error.status === 403) {
      return "Backend yetki hatası (HTTP " + error.status + ")";
    }
    if (error.status) return "Backend HTTP " + error.status + " döndürdü";
    return "İstek başarısız";
  }

  /** Teshis satirlari: ne istendi, ne dondu. Hicbir sey gizlenmez. */
  function errorExtras(error) {
    if (!error) return [];
    var lines = [];
    var details = error.details || null;

    if (details && details.provider_message) {
      lines.push("API-Football mesajı: " + details.provider_message);
    }
    if (details && details.hint) lines.push(details.hint);
    if (details && details.problems && details.problems.length) {
      lines.push("Geçersiz parametreler: " + details.problems.join(" | "));
    }
    if (error.url) lines.push("İstek: " + error.method + " " + error.url);
    if (error.status) lines.push("Yanıt: HTTP " + error.status);
    if (error.requestId) {
      lines.push("Request ID: " + error.requestId + "  (Render loglarında bu id ile arayın)");
    }
    if (!details && error.bodyExcerpt) {
      lines.push("Sunucu gövdesi: " + error.bodyExcerpt);
    }
    return lines;
  }

  // ---------------------------------------------------------------- config
  function loadBackendConfig() {
    return Api.config().then(function (payload) {
      // El sikisma: bu adreste GERCEKTEN bizim backend'imiz mi var?
      // (Onceki hata tam olarak buydu: Render'da baska bir uygulama
      // calisiyordu ve frontend bunu "Sunucu 400 dondurdu" diye gosteriyordu.)
      if (payload.api_contract !== config.apiContract) {
        var found = payload.app
          ? payload.app + " " + (payload.version || "")
          : "tanımsız bir servis";
        var mismatch = new global.ApiError(
          "Bu adres MACANALİZ PRO backend'i değil (veya uyumsuz bir sürüm). " +
          "Beklenen sözleşme: " + config.apiContract + ", bulunan: " +
          (payload.api_contract || "yok") + " — " + found + ".",
          "contract_mismatch", 200, null,
          { transport: false, url: Api.baseUrl + "/api/config", method: "GET" }
        );
        throw mismatch;
      }
      state.version = payload.version;
      state.bookmakerName = payload.bookmaker_name || config.bookmakerName;
      elById("bookmaker-field").value = state.bookmakerName;

      var preset = elById("league-preset");
      F.clear(preset);
      var allOption = document.createElement("option");
      allOption.value = "";
      allOption.textContent = "Tüm ligler";
      preset.appendChild(allOption);
      (payload.leagues || []).forEach(function (league) {
        var option = document.createElement("option");
        option.value = String(league.id);
        option.textContent = league.name || ("Lig " + league.id);
        preset.appendChild(option);
      });
      preset.value = elById("league-ids").value.indexOf(",") === -1
        ? elById("league-ids").value : "";
      UI.renderTopbar(state);
      return payload;
    });
  }

  /** /api/config basarisiz olursa sebebi ACIKCA goster. */
  function showConfigProblem(error) {
    state.apiState = "error";
    UI.renderTopbar(state);
    UI.renderStatus([{ label: "Backend", ok: false, detail: describeError(error) }]);
    UI.showError("matches", errorTitle(error), describeError(error), errorExtras(error));
    UI.showEmpty("top5", "Backend doğrulanamadığı için analiz üretilmedi.");
    UI.showEmpty("highlights", "Backend doğrulanamadı.");
  }

  // ---------------------------------------------------------------- test
  function runApiTest() {
    setBusy(true, "Test ediliyor…");
    state.apiState = "loading";
    UI.renderTopbar(state);
    UI.renderStatus([{ label: "Backend", ok: true, detail: "İstek gönderildi, yanıt bekleniyor…" }]);

    var slowShown = false;
    return Api.status({
      onSlow: function () {
        slowShown = true;
        UI.renderStatus([{
          label: "Backend",
          ok: true,
          detail: "Yanıt bekleniyor… Render ücretsiz servisi uykudaysa uyanması ~1 dakika sürebilir."
        }]);
      }
    }).then(function (status) {
      state.apiState = status.api_connected ? "ok" : "error";
      state.bet365 = status.bet365_available;
      state.database = status.database;
      state.databasePersistent = status.database_persistent;
      state.quota = status.quota;
      state.plan = status.plan;
      state.version = status.version;
      UI.renderTopbar(state);
      UI.renderStatus(status.checks || []);
      UI.renderWarnings(status.warnings || []);
      if (status.leagues && status.leagues.length) {
        var seasons = status.leagues
          .filter(function (row) { return row.season; })
          .map(function (row) { return row.name + " " + row.season; });
        elById("season-field").value = seasons.length
          ? seasons.join(", ")
          : "API'den sezon alınamadı";
      }
      return status;
    }).catch(function (error) {
      state.apiState = "error";
      UI.renderTopbar(state);
      UI.renderStatus([{ label: "Backend", ok: false, detail: describeError(error) }]);
      UI.showError("matches", errorTitle(error), describeError(error), errorExtras(error));
      throw error;
    }).finally(function () {
      setBusy(false);
    });
  }

  // ---------------------------------------------------------------- yukleme
  function loadAll() {
    var params = readFilters();
    persistSettings();
    setBusy(true, "Maçlar getiriliyor…");
    state.apiState = "loading";
    UI.renderTopbar(state);

    UI.showLoading("matches", "Maçlar getiriliyor…");
    UI.showLoading("top5", "Analizler hazırlanıyor…");
    UI.showLoading("highlights", "Hesaplanıyor…");

    var onSlow = function () {
      UI.showLoading("matches",
        "Hâlâ bekleniyor… Render ücretsiz servisi uykudaysa ilk istek ~1 dakika sürebilir.");
    };

    return Api.matches(params, { onSlow: onSlow }).then(function (payload) {
      state.apiState = "ok";
      state.quota = payload.quota;
      state.lastUpdate = F.clockNow();
      UI.renderTopbar(state);
      UI.renderWarnings(payload.warnings || []);
      UI.renderMatches(payload.matches || []);

      var anyOdds = (payload.matches || []).some(function (match) {
        return match.bet365 && match.bet365.available;
      });
      state.bet365 = anyOdds ? true : state.bet365;
      UI.renderTopbar(state);

      return Promise.all([
        Api.top5(params).catch(function () { return null; }),
        Api.highlights(params).catch(function () { return null; })
      ]).then(function (results) {
        var top5 = results[0];
        var highlights = results[1];
        UI.renderTop5(top5);
        UI.renderHighlights(highlights ? highlights.cards : null);
        UI.renderKpis(payload.counts, top5 && top5.matches ? top5.matches.length : 0);
      });
    }).catch(function (error) {
      state.apiState = "error";
      UI.renderTopbar(state);
      UI.showError("matches", errorTitle(error), describeError(error), errorExtras(error));
      UI.showEmpty("top5", "Veri alınamadığı için sıralama üretilmedi.");
      UI.showEmpty("highlights", "Veri alınamadığı için kart üretilmedi.");
      UI.renderKpis(null, null);
    }).finally(function () {
      setBusy(false);
    });
  }

  // ---------------------------------------------------------------- detay
  function openMatch(fixtureId) {
    if (!fixtureId) return;
    UI.openModal(function (body) {
      body.appendChild(F.el("div", "loading-state", "Maç analizi hazırlanıyor…"));
    }, "Maç analizi", "Veriler getiriliyor…");

    Api.match(fixtureId).then(function (detail) {
      UI.openModal(function (body) {
        UI.renderMatchDetail(detail, body);
      },
      detail.home.name + " – " + detail.away.name,
      (detail.league ? detail.league.name + " · " : "") + detail.kickoff.display +
        " · " + (detail.bet365.available ? state.bookmakerName + " oranı var" : state.bookmakerName + " oranı yok"));
    }).catch(function (error) {
      UI.openModal(function (body) {
        var box = F.el("div", "error-state");
        box.appendChild(F.el("h3", null, "Maç analizi alınamadı"));
        box.appendChild(F.el("div", null, describeError(error)));
        body.appendChild(box);
      }, "Hata", "");
    });
  }

  // ---------------------------------------------------------------- otomatik
  function updateAutoRefresh() {
    if (state.autoTimer) { clearInterval(state.autoTimer); state.autoTimer = null; }
    if (elById("auto-refresh").checked) {
      state.autoTimer = setInterval(function () {
        if (!state.loading) loadAll();
      }, config.autoRefreshMs);
    }
    persistSettings();
  }

  // ---------------------------------------------------------------- baslangic
  function init() {
    UI.setMatchHandler(openMatch);
    restoreSettings();
    UI.renderTopbar(state);
    UI.renderKpis(null, null);
    UI.showEmpty("top5", "Henüz veri getirilmedi. \"Maçları Getir\" ile başlayın.");
    UI.showEmpty("highlights", "Henüz veri getirilmedi.");
    UI.showEmpty("matches", "Henüz veri getirilmedi.");

    elById("backend-url").addEventListener("change", function (event) {
      Api.setBaseUrl(event.target.value);
      loadBackendConfig().catch(showConfigProblem);
    });

    elById("league-preset").addEventListener("change", function (event) {
      elById("league-ids").value = event.target.value;
      persistSettings();
    });

    elById("day-range").addEventListener("change", function () {
      toggleCustomRange();
      persistSettings();
    });

    elById("auto-refresh").addEventListener("change", updateAutoRefresh);
    elById("btn-test").addEventListener("click", function () {
      runApiTest().catch(function () { /* durum listesinde gosterildi */ });
    });
    elById("btn-load").addEventListener("click", loadAll);
    elById("btn-refresh").addEventListener("click", loadAll);

    loadBackendConfig().then(function () {
      return runApiTest();
    }).then(function (status) {
      if (status && status.api_connected) return loadAll();
    }).catch(function (error) {
      // Sozlesme uyusmazligini burada goster; digerleri zaten gosterildi.
      if (error && error.code === "contract_mismatch") showConfigProblem(error);
    });

    updateAutoRefresh();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})(window);
