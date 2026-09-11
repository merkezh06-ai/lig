/**
 * Backend istemcisi.
 *
 * Onemli davranislar:
 *  - Backend URL ayardan gelir, koda gomulu degildir.
 *  - Render ucretsiz servisi uyuyorsa ilk istek ~1 dk surer; bu durum
 *    "backend uyaniyor" olarak gosterilir, sessizce hata sayilmaz.
 *  - Hata olursa DEMO VERIYE DUSULMEZ. Hata acikca bildirilir.
 */
(function (global) {
  "use strict";

  var config = global.MACANALIZ_CONFIG;

  /**
   * @param {boolean} transport  true = sunucudan HIC cevap alinamadi.
   *   Bu ayrim kritik: sunucu 400/404 gibi anlamli bir cevap dondurduyse
   *   kullaniciya "Backend'e ulasilamadi" DENMEZ - gercek cevap gosterilir.
   */
  function ApiError(message, code, status, details, extra) {
    extra = extra || {};
    this.name = "ApiError";
    this.message = message;
    this.code = code || "unknown";
    this.status = status || 0;
    // Sunucunun gonderdigi hata govdesinin TAMAMI (ör. plan kisitinda
    // saglayicinin kendi mesaji ve yol gosterici not).
    this.details = details || null;
    this.transport = Boolean(extra.transport);
    this.url = extra.url || null;
    this.method = extra.method || "GET";
    this.bodyExcerpt = extra.bodyExcerpt || null;
    this.requestId = extra.requestId || null;
  }
  ApiError.prototype = Object.create(Error.prototype);

  function readStored(key, fallback) {
    try {
      var value = global.localStorage.getItem(key);
      return value === null ? fallback : value;
    } catch (error) {
      return fallback;
    }
  }

  function writeStored(key, value) {
    try {
      global.localStorage.setItem(key, value);
    } catch (error) {
      /* private mode - sessizce gec */
    }
  }

  var Api = {
    baseUrl: readStored(config.storageKeys.backendUrl, config.defaultBackendUrl),

    setBaseUrl: function (url) {
      this.baseUrl = String(url || "").trim().replace(/\/+$/, "");
      writeStored(config.storageKeys.backendUrl, this.baseUrl);
      return this.baseUrl;
    },

    /**
     * @param {string} path        /api/... yolu
     * @param {object} [params]    query parametreleri
     * @param {object} [options]   { onSlow: fn, method: "GET"|"POST" }
     */
    request: function (path, params, options) {
      options = options || {};
      var self = this;

      if (!self.baseUrl) {
        return Promise.reject(new ApiError(
          "Backend URL tanimli degil. Sol paneldeki alana Render adresinizi girin.",
          "no_backend_url"
        ));
      }

      var url;
      try {
        url = new URL(self.baseUrl.replace(/\/+$/, "") + path);
      } catch (error) {
        return Promise.reject(new ApiError(
          "Backend URL gecersiz: " + self.baseUrl, "bad_backend_url"
        ));
      }

      Object.keys(params || {}).forEach(function (key) {
        var value = params[key];
        if (value !== null && value !== undefined && value !== "") {
          url.searchParams.set(key, value);
        }
      });

      if (global.location.protocol === "https:" && url.protocol === "http:") {
        return Promise.reject(new ApiError(
          "Sayfa HTTPS uzerinden acildi ama backend HTTP. Tarayici bu istegi engeller " +
          "(mixed content). Backend URL'sini https:// ile girin.",
          "mixed_content"
        ));
      }

      var controller = typeof AbortController !== "undefined" ? new AbortController() : null;
      var timer = setTimeout(function () {
        if (controller) controller.abort();
      }, config.requestTimeoutMs);

      var slowTimer = null;
      if (options.onSlow) {
        slowTimer = setTimeout(options.onSlow, config.coldStartHintAfterMs);
      }

      function done() {
        clearTimeout(timer);
        if (slowTimer) clearTimeout(slowTimer);
      }

      var method = options.method || "GET";
      var finalUrl = url.toString();

      return fetch(finalUrl, {
        method: method,
        headers: { Accept: "application/json" },
        signal: controller ? controller.signal : undefined,
        cache: "no-store",
        mode: "cors"
      }).then(function (response) {
        return response.text().then(function (raw) {
          done();
          var payload = null;
          if (raw) {
            try { payload = JSON.parse(raw); } catch (error) { payload = null; }
          }
          var requestId = response.headers.get("X-Request-ID");
          var excerpt = raw ? String(raw).slice(0, 400) : null;

          if (!response.ok) {
            // Sunucu CEVAP VERDI. Mesaji varsa onu goster; yoksa ham govdeyi
            // teshis icin tasi - "Sunucu 400 dondurdu" gibi bos bir metinle
            // birakma.
            var message = payload && payload.error && payload.error.message
              ? payload.error.message
              : "Backend HTTP " + response.status + " dondurdu ve taninan bir "
                + "hata govdesi gondermedi.";
            var code = payload && payload.error && payload.error.code
              ? payload.error.code
              : "http_" + response.status;
            throw new ApiError(message, code, response.status,
                               payload && payload.error ? payload.error : null,
                               { transport: false, url: finalUrl, method: method,
                                 bodyExcerpt: excerpt, requestId: requestId });
          }
          if (payload === null) {
            throw new ApiError(
              "Backend 200 dondurdu ama govdesi gecerli JSON degil.",
              "bad_json", response.status, null,
              { transport: false, url: finalUrl, method: method,
                bodyExcerpt: excerpt, requestId: requestId });
          }
          return payload;
        });
      }).catch(function (error) {
        done();
        if (error instanceof ApiError) throw error;
        if (error && error.name === "AbortError") {
          throw new ApiError(
            "Backend zaman asimina ugradi. Render ucretsiz servisi uykudaysa ilk istek " +
            "bir dakikayi bulabilir; tekrar deneyin.",
            "timeout", 0, null, { transport: true, url: finalUrl, method: method }
          );
        }
        throw new ApiError(
          "Backend'e ulasilamadi (sunucudan hic cevap gelmedi). URL dogru mu, " +
          "servis ayakta mi ve CORS izinli mi kontrol edin.",
          "network", 0, null, { transport: true, url: finalUrl, method: method }
        );
      });
    },

    health: function (options) { return this.request("/api/health", {}, options); },
    config: function () { return this.request("/api/config"); },
    status: function (options) { return this.request("/api/status", {}, options); },
    quota: function () { return this.request("/api/quota"); },
    leagues: function () { return this.request("/api/leagues"); },
    matches: function (params, options) { return this.request("/api/matches", params, options); },
    top5: function (params) { return this.request("/api/top5", params); },
    highlights: function (params) { return this.request("/api/highlights", params); },
    match: function (fixtureId) { return this.request("/api/match/" + encodeURIComponent(fixtureId)); },
    snapshots: function (fixtureId) {
      return this.request("/api/snapshots/" + encodeURIComponent(fixtureId));
    },
    calibration: function () { return this.request("/api/calibration"); }
  };

  global.Api = Api;
  global.ApiError = ApiError;
  global.Storage2 = { read: readStored, write: writeStored };
})(window);
