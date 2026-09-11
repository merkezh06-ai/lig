/**
 * Render katmani.
 *
 * Tek kural: BURADA HICBIR DEGER URETILMEZ. Backend ne gonderdiyse o
 * gosterilir; alan yoksa "veri yok" yazilir. `x || 0` gibi varsayilan
 * doldurma yapilmaz - onceki surumdeki "ekranda var ama API'de yok"
 * sorununun kaynagi tam olarak buydu.
 */
(function (global) {
  "use strict";

  var F = global.Fmt;
  var el = F.el, clear = F.clear, append = F.append;

  var UI = {};
  var onMatchClick = function () {};

  UI.setMatchHandler = function (handler) { onMatchClick = handler; };

  // ------------------------------------------------------------------ genel
  function emptyState(text) {
    return el("div", "empty-state", text);
  }

  function loadingState(text) {
    var box = el("div", "loading-state");
    append(box, el("span", "spinner"), el("span", null, " " + text));
    return box;
  }

  function errorState(title, message, extraLines) {
    var box = el("div", "error-state");
    append(box, el("h3", null, title), el("div", null, message));
    (extraLines || []).forEach(function (line) {
      if (line) box.appendChild(el("div", "cn", line));
    });
    return box;
  }

  UI.showLoading = function (containerId, text) {
    var node = document.getElementById(containerId);
    if (node) { clear(node).appendChild(loadingState(text)); }
  };

  UI.showError = function (containerId, title, message, extraLines) {
    var node = document.getElementById(containerId);
    if (node) { clear(node).appendChild(errorState(title, message, extraLines)); }
  };

  UI.showEmpty = function (containerId, text) {
    var node = document.getElementById(containerId);
    if (node) { clear(node).appendChild(emptyState(text)); }
  };

  // ------------------------------------------------------------------ ust bar
  UI.renderTopbar = function (state) {
    var apiBadge = document.getElementById("api-badge");
    clear(apiBadge);
    apiBadge.className = "badge";
    if (state.apiState === "ok") {
      apiBadge.classList.add("ok"); apiBadge.textContent = "CANLI";
    } else if (state.apiState === "error") {
      apiBadge.classList.add("bad"); apiBadge.textContent = "HATA";
    } else if (state.apiState === "loading") {
      apiBadge.classList.add("info"); apiBadge.textContent = "BAĞLANIYOR";
    } else {
      apiBadge.textContent = "BİLİNMİYOR";
    }

    var bookmakerBadge = document.getElementById("bookmaker-badge");
    bookmakerBadge.className = "badge" + (state.bet365 === true ? " ok" : (state.bet365 === false ? " bad" : ""));
    bookmakerBadge.textContent = state.bet365 === false
      ? (state.bookmakerName || "Bet365") + " YOK"
      : (state.bookmakerName || "Bet365");

    var dbBadge = document.getElementById("db-badge");
    if (state.database === null || state.database === undefined) {
      dbBadge.className = "badge"; dbBadge.textContent = "—";
    } else {
      dbBadge.className = "badge " + (state.databasePersistent ? "ok" : "warn");
      dbBadge.textContent = state.databasePersistent ? "KALICI" : "GEÇİCİ";
    }

    var planBadge = document.getElementById("plan-badge");
    if (planBadge) {
      var plan = state.plan;
      if (!plan || !plan.checked) {
        planBadge.className = "badge";
        planBadge.textContent = "—";
      } else if (plan.season_access_ok) {
        planBadge.className = "badge ok";
        planBadge.textContent = plan.requested_season + " ERİŞİLEBİLİR";
      } else {
        planBadge.className = "badge bad";
        planBadge.textContent = plan.requested_season + " ERİŞİM YOK";
      }
    }

    var quota = document.getElementById("quota-value");
    quota.textContent = (state.quota && state.quota.known && F.has(state.quota.daily_remaining))
      ? String(state.quota.daily_remaining)
      : F.NO_DATA;

    document.getElementById("last-update").textContent = state.lastUpdate || "—";
    if (state.version) document.getElementById("app-version").textContent = "v" + state.version;
  };

  // ------------------------------------------------------------------ durum
  UI.renderStatus = function (checks) {
    var list = document.getElementById("status-list");
    clear(list);
    if (!checks || !checks.length) {
      var placeholder = el("div", "statusrow");
      append(placeholder, el("span", "dot"), el("span", "detail muted", "Henüz test edilmedi."));
      list.appendChild(placeholder);
      return;
    }
    checks.forEach(function (check) {
      var row = el("div", "statusrow " + (check.ok ? "ok" : "bad"));
      append(row,
        el("span", "dot"),
        el("span", "label", check.label),
        el("span", "detail", check.detail || (check.ok ? "Tamam" : "Başarısız"))
      );
      list.appendChild(row);
    });
  };

  // ------------------------------------------------------------------ uyarilar
  UI.renderWarnings = function (warnings) {
    var node = document.getElementById("warnings");
    clear(node);
    if (!warnings || !warnings.length) return;
    var box = el("div", "warnbox");
    box.appendChild(el("strong", null, "Dikkat edilmesi gerekenler"));
    var list = el("ul");
    warnings.forEach(function (text) { list.appendChild(el("li", null, text)); });
    box.appendChild(list);
    node.appendChild(box);
  };

  // ------------------------------------------------------------------ KPI
  UI.renderKpis = function (counts, top5Count) {
    function set(id, value, sub) {
      document.getElementById(id).textContent = F.has(value) ? String(value) : "—";
      document.getElementById(id + "-sub").textContent = sub;
    }
    if (!counts) {
      set("kpi-total", null, "veri yok");
      set("kpi-odds", null, "veri yok");
      set("kpi-analyzed", null, "veri yok");
      set("kpi-top5", null, "veri yok");
      return;
    }
    set("kpi-total", counts.total, "seçilen filtrede");
    set("kpi-odds", counts.with_bet365_odds,
        counts.total ? (counts.total - counts.with_bet365_odds) + " maçta Bet365 oranı yok" : "veri yok");
    set("kpi-analyzed", counts.analyzed, "model çalıştırılabildi");
    set("kpi-top5", top5Count, "sıralamaya giren maç");
  };

  // ------------------------------------------------------------------ Top 5
  function probabilityBar(probabilities) {
    var bar = el("div", "probbar");
    ["1", "X", "2"].forEach(function (key, index) {
      var value = probabilities[key];
      if (!F.has(value)) return;
      var part = el("div", ["p1", "px", "p2"][index], Math.round(value * 100) + "%");
      part.style.width = (value * 100).toFixed(2) + "%";
      bar.appendChild(part);
    });
    return bar;
  }

  function top5Card(match, rank) {
    var card = el("div", "top5card");
    card.setAttribute("role", "button");
    card.setAttribute("tabindex", "0");

    var head = el("div");
    append(head,
      el("div", "rank", "#" + rank + "  " + (match.league ? match.league.name : "")),
      el("div", "teams", match.home.name + " – " + match.away.name)
    );
    card.appendChild(head);

    var meta = el("div", "meta");
    append(meta,
      el("span", null, match.kickoff.display),
      el("span", null, match.status_short)
    );
    card.appendChild(meta);

    var pick = F.best(match.model && match.model.match_result ? match.model.match_result.probabilities : null);
    var pickRow = el("div", "pickrow");
    if (pick) {
      append(pickRow,
        el("span", "pick", F.outcomeShort(pick.key)),
        el("span", "prob", F.percent(pick.value))
      );
    } else {
      pickRow.appendChild(el("span", "muted", "Model çalıştırılamadı"));
    }
    card.appendChild(pickRow);

    if (match.model && match.model.match_result && match.model.match_result.available) {
      card.appendChild(probabilityBar(match.model.match_result.probabilities));
    }

    var grid = el("div", "minigrid");
    var oddValue = F.NO_DATA;
    if (match.bet365 && match.bet365.available && pick) {
      var map = { "1": match.bet365.home, "X": match.bet365.draw, "2": match.bet365.away };
      oddValue = F.odd(map[pick.key]);
    }
    append(grid,
      F.miniStat("Bet365", oddValue),
      F.miniStat("Model/Piyasa", match.value && match.value.available ? F.points(match.value.best_edge) : F.NO_DATA),
      F.miniStat("Güven", F.score(match.confidence.score, match.confidence.max_score)),
      F.miniStat("Veri kalitesi", F.score(match.data_quality.score, match.data_quality.max_score))
    );
    card.appendChild(grid);
    card.appendChild(F.meter(match.data_quality.score, match.data_quality.max_score,
                             F.qualityClass(match.data_quality.score)));

    function open() { onMatchClick(match.fixture_id); }
    card.addEventListener("click", open);
    card.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); }
    });
    return card;
  }

  UI.renderTop5 = function (payload) {
    var node = document.getElementById("top5");
    clear(node);
    if (!payload || !payload.matches || !payload.matches.length) {
      var reasons = [];
      if (payload && payload.excluded_counts) {
        var map = {
          bet365_yok: "Bet365 oranı yok",
          model_yok: "model çalıştırılamadı",
          dusuk_veri_kalitesi: "veri kalitesi eşiğin altında"
        };
        Object.keys(payload.excluded_counts).forEach(function (key) {
          var count = payload.excluded_counts[key];
          if (count) reasons.push(count + " maç: " + (map[key] || key));
        });
      }
      node.appendChild(emptyState(
        "Sıralamaya girebilecek maç bulunamadı." +
        (reasons.length ? " (" + reasons.join(", ") + ")" : "")
      ));
      return;
    }
    var grid = el("div", "top5grid");
    payload.matches.forEach(function (match, index) {
      grid.appendChild(top5Card(match, index + 1));
    });
    node.appendChild(grid);
  };

  // ------------------------------------------------------------------ one cikanlar
  UI.renderHighlights = function (cards) {
    var node = document.getElementById("highlights");
    clear(node);
    if (!cards || !cards.length) {
      node.appendChild(emptyState("Öne çıkan analiz üretilemedi."));
      return;
    }
    var grid = el("div", "highlights");
    cards.forEach(function (card) {
      var box = el("div", "hcard" + (card.available ? " clickable" : ""));
      box.appendChild(el("div", "k", card.label));
      if (card.available) {
        append(box, el("div", "h", card.headline), el("div", "d", card.detail));
        box.addEventListener("click", function () { onMatchClick(card.fixture_id); });
      } else {
        box.appendChild(el("div", "empty", card.note || "veri yok"));
      }
      grid.appendChild(box);
    });
    node.appendChild(grid);
  };

  // ------------------------------------------------------------------ mac tablosu
  var COLUMNS = ["Tarih", "Lig", "Maç", "Bet365 1", "X", "2", "Model 1", "X", "2",
                 "Fark", "Hareket", "Güven", "Veri"];

  UI.renderMatches = function (matches) {
    var node = document.getElementById("matches");
    clear(node);
    document.getElementById("match-count-hint").textContent =
      matches && matches.length ? matches.length + " maç" : "";

    if (!matches || !matches.length) {
      node.appendChild(emptyState("Bu filtrelerde maç bulunamadı."));
      return;
    }

    var wrap = el("div", "tablewrap");
    var table = el("table", "matches");
    var thead = el("thead");
    var headRow = el("tr");
    COLUMNS.forEach(function (title) { headRow.appendChild(el("th", null, title)); });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = el("tbody");
    matches.forEach(function (match) {
      tbody.appendChild(matchRow(match));
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    node.appendChild(wrap);
  };

  function matchRow(match) {
    var row = el("tr");
    row.appendChild(el("td", "muted", match.kickoff.display));
    row.appendChild(el("td", "muted", match.league ? match.league.name : F.NO_DATA));
    row.appendChild(el("td", "teams", match.home.name + " – " + match.away.name));

    var odds = match.bet365 && match.bet365.available
      ? [match.bet365.home, match.bet365.draw, match.bet365.away]
      : [null, null, null];
    odds.forEach(function (value) {
      var cell = el("td", "num" + (F.has(value) ? "" : " muted"), F.odd(value));
      row.appendChild(cell);
    });

    var model = match.model && match.model.match_result && match.model.match_result.available
      ? match.model.match_result.probabilities : null;
    ["1", "X", "2"].forEach(function (key) {
      var value = model ? model[key] : null;
      row.appendChild(el("td", "num" + (F.has(value) ? "" : " muted"), F.percent(value, 0)));
    });

    var edgeCell = el("td", "num");
    if (match.value && match.value.available && F.has(match.value.best_edge)) {
      edgeCell.textContent = F.outcomeShort(match.value.best_outcome) + " " + F.points(match.value.best_edge);
      edgeCell.classList.add(match.value.best_edge > 0 ? "pos" : "neg");
    } else {
      edgeCell.textContent = F.NO_DATA;
      edgeCell.classList.add("muted");
    }
    row.appendChild(edgeCell);

    var moveCell = el("td", "num");
    var movement = match.odds_movement;
    if (movement && movement.available && movement.change_pct && F.has(movement.change_pct["1"])) {
      moveCell.textContent = "1: " + F.changePct(movement.change_pct["1"]);
      moveCell.classList.add(movement.change_pct["1"] < 0 ? "pos" : "neg");
    } else {
      moveCell.textContent = F.NO_DATA;
      moveCell.classList.add("muted");
    }
    row.appendChild(moveCell);

    row.appendChild(el("td", "num", F.score(match.confidence.score, match.confidence.max_score)));
    row.appendChild(el("td", "num", F.score(match.data_quality.score, match.data_quality.max_score)));

    row.addEventListener("click", function () { onMatchClick(match.fixture_id); });
    return row;
  }

  // ------------------------------------------------------------------ modal
  function closeModal() {
    clear(document.getElementById("modal-root"));
    document.body.style.overflow = "";
  }

  UI.closeModal = closeModal;

  UI.openModal = function (buildBody, title, subtitle) {
    var root = document.getElementById("modal-root");
    clear(root);

    var backdrop = el("div", "modal-backdrop");
    var modal = el("div", "modal");

    var head = el("div", "modal-head");
    var titleBox = el("div");
    append(titleBox, el("h2", null, title), el("div", "sub", subtitle || ""));
    var close = el("button", "modal-close", "Kapat");
    close.type = "button";
    close.addEventListener("click", closeModal);
    append(head, titleBox, close);
    modal.appendChild(head);

    var body = el("div");
    modal.appendChild(body);
    buildBody(body);

    backdrop.appendChild(modal);
    backdrop.addEventListener("click", function (event) {
      if (event.target === backdrop) closeModal();
    });
    document.addEventListener("keydown", function escHandler(event) {
      if (event.key === "Escape") { closeModal(); document.removeEventListener("keydown", escHandler); }
    });

    root.appendChild(backdrop);
    document.body.style.overflow = "hidden";
  };

  // ------------------------------------------------------------------ detay
  function block(title) {
    var box = el("div", "block");
    box.appendChild(el("h3", null, title));
    return box;
  }

  function marketBlock(title, market, labelMap) {
    var box = block(title);
    if (!market || !market.available) {
      box.appendChild(el("div", "muted", market && market.note ? market.note : "Yeterli veri yok"));
      return box;
    }
    Object.keys(market.probabilities).forEach(function (key) {
      var label = labelMap && labelMap[key] ? labelMap[key] : key;
      box.appendChild(F.kv(label, F.percent(market.probabilities[key])));
    });
    return box;
  }

  function componentsBlock(title, scoreBlock, intro) {
    var box = block(title);
    box.appendChild(el("div", null, F.score(scoreBlock.score, scoreBlock.max_score)));
    var details = el("details", "reveal");
    details.appendChild(el("summary", null, intro));
    (scoreBlock.components || []).forEach(function (component) {
      var row = el("div", "component-row" + (component.available ? "" : " off"));
      var left = el("div");
      append(left,
        el("div", "cl", component.label),
        el("div", "cn", component.note || (component.available ? "" : "veri yok"))
      );
      append(row, left, el("div", "cp", component.points.toFixed(1) + " / " + component.max_points.toFixed(1)));
      details.appendChild(row);
    });
    box.appendChild(details);
    return box;
  }

  function movementBlock(movement) {
    var box = block("Oran hareketi (kendi kayıtlarımız)");
    if (!movement || !movement.available) {
      box.appendChild(el("div", "muted", movement && movement.note ? movement.note : "Snapshot yok"));
      return box;
    }
    [["first_recorded", movement.first_recorded], ["latest", movement.latest],
     ["closest", movement.closest_to_kickoff]].forEach(function (pair) {
      var point = pair[1];
      if (!point) return;
      var values = ["1", "X", "2"].map(function (key) {
        return key + " " + F.odd(point.values[key]);
      }).join("  ");
      box.appendChild(F.kv(point.label, values));
      box.appendChild(el("div", "cn muted", F.dateTime(point.captured_at)));
    });
    Object.keys(movement.change_pct || {}).forEach(function (key) {
      var change = movement.change_pct[key];
      box.appendChild(F.kv("Değişim " + key,
        F.changePct(change) + " (" + (movement.direction[key] || "") + ")",
        change < 0 ? "pos" : "neg"));
    });
    box.appendChild(el("div", "cn muted",
      "Kayıt sayısı: " + movement.snapshot_count + ". Kaydedilmemiş bir an için oran gösterilmez."));
    return box;
  }

  function formBlock(title, team, form, venue) {
    var box = block(title + " — " + team.name);
    if (form.available) {
      var pills = el("div", "formpills");
      form.results.forEach(function (result) { pills.appendChild(el("span", "pill " + result, result)); });
      box.appendChild(pills);
      box.appendChild(F.kv("G / B / M", form.wins + " / " + form.draws + " / " + form.losses));
      box.appendChild(F.kv("Attığı / yediği", form.goals_for + " / " + form.goals_against));
      box.appendChild(F.kv("Maç başı puan", F.has(form.points_per_game) ? form.points_per_game.toFixed(2) : F.NO_DATA));
    } else {
      box.appendChild(el("div", "muted", form.note || "Form verisi yok"));
    }
    if (venue.available) {
      box.appendChild(F.kv(title === "Ev sahibi" ? "Evinde maç" : "Deplasmanda maç", venue.matches));
      box.appendChild(F.kv("Gol ort. (attığı)", venue.goals_for_avg));
      box.appendChild(F.kv("Gol ort. (yediği)", venue.goals_against_avg));
    } else {
      box.appendChild(el("div", "muted", venue.note || "Ev/deplasman verisi yok"));
    }
    return box;
  }

  UI.renderMatchDetail = function (detail, container) {
    var grid = el("div", "modal-grid");

    // --- Bet365 ---
    var oddsBox = block("Bet365 oranları");
    if (detail.bet365.available) {
      oddsBox.appendChild(F.kv("1", F.odd(detail.bet365.home)));
      oddsBox.appendChild(F.kv("X", F.odd(detail.bet365.draw)));
      oddsBox.appendChild(F.kv("2", F.odd(detail.bet365.away)));
      if (detail.implied.available) {
        oddsBox.appendChild(el("div", "cn muted", "Marj: " + F.changePct(detail.implied.margin_pct)));
        ["1", "X", "2"].forEach(function (key) {
          oddsBox.appendChild(F.kv("Piyasa " + key + " (marj arındırılmış)",
                                   F.percent(detail.implied.normalized[key])));
        });
      }
      var extras = Object.keys(detail.bet365.markets || {});
      if (extras.length) {
        oddsBox.appendChild(el("div", "cn muted", "Gelen marketler: " + extras.join(", ")));
      }
    } else {
      oddsBox.appendChild(el("div", "muted", detail.bet365.reason || "Bet365 oranı yok"));
      oddsBox.appendChild(el("div", "cn muted",
        "Başka bookmaker'ın oranı Bet365 yerine kullanılmaz."));
    }
    grid.appendChild(oddsBox);

    // --- Model ---
    var modelBox = block("Model — maç sonucu");
    if (detail.model.available) {
      modelBox.appendChild(F.kv("Beklenen gol",
        detail.model.expected_goals_home.toFixed(2) + " – " + detail.model.expected_goals_away.toFixed(2)));
      ["1", "X", "2"].forEach(function (key) {
        modelBox.appendChild(F.kv(F.outcomeLabel(key, detail.home.name, detail.away.name),
                                  F.percent(detail.model.match_result.probabilities[key])));
      });
      if (detail.model.top_scoreline) {
        modelBox.appendChild(F.kv("En olası skor",
          detail.model.top_scoreline.home + "-" + detail.model.top_scoreline.away +
          " (" + F.percent(detail.model.top_scoreline.probability) + ")"));
      }
      modelBox.appendChild(el("div", "cn muted",
        "Model Bet365 oranını girdi olarak KULLANMAZ; fark bu yüzden anlamlıdır."));
    } else {
      modelBox.appendChild(el("div", "muted", detail.model.note || "Model çalıştırılamadı"));
    }
    grid.appendChild(modelBox);

    grid.appendChild(marketBlock("İlk yarı", detail.model.first_half,
      { "1": detail.home.name, "X": "Beraberlik", "2": detail.away.name }));
    grid.appendChild(marketBlock("Karşılıklı gol", detail.model.both_teams_to_score,
      { var: "KG Var", yok: "KG Yok" }));
    grid.appendChild(marketBlock("2.5 Üst / Alt", detail.model.over_under_25,
      { ust: "2.5 Üst", alt: "2.5 Alt" }));
    grid.appendChild(marketBlock("Çifte şans", detail.model.double_chance,
      { "1X": "1X", "12": "12", X2: "X2" }));

    // --- Fark ---
    var valueBox = block("Model / piyasa farkı");
    if (detail.value.available) {
      Object.keys(detail.value.edges).forEach(function (key) {
        var edge = detail.value.edges[key];
        valueBox.appendChild(F.kv(F.outcomeShort(key), F.points(edge), edge > 0 ? "pos" : "neg"));
      });
      valueBox.appendChild(el("div", "cn muted",
        "Pozitif fark model avantajı anlamına gelir; kesinlik veya garanti anlamına gelmez."));
    } else {
      valueBox.appendChild(el("div", "muted", detail.value.note || "Hesaplanamadı"));
    }
    grid.appendChild(valueBox);

    grid.appendChild(movementBlock(detail.odds_movement));
    grid.appendChild(formBlock("Ev sahibi", detail.home, detail.home_form, detail.home_venue));
    grid.appendChild(formBlock("Deplasman", detail.away, detail.away_form, detail.away_venue));

    // --- H2H ---
    var h2hBox = block("Karşılıklı maçlar");
    if (detail.h2h.available) {
      h2hBox.appendChild(F.kv("Galibiyet dağılımı",
        detail.h2h.home_wins + " / " + detail.h2h.draws + " / " + detail.h2h.away_wins));
      detail.h2h.recent.slice(0, 5).forEach(function (row) {
        h2hBox.appendChild(F.kv(row.date, row.home + " " + row.score + " " + row.away));
      });
    } else {
      h2hBox.appendChild(el("div", "muted", detail.h2h.note || "H2H verisi yok"));
    }
    grid.appendChild(h2hBox);

    // --- Sakatlik ---
    var injuryBox = block("Sakatlıklar / eksikler");
    if (detail.injuries.available) {
      injuryBox.appendChild(F.kv(detail.home.name, detail.injuries.home_count + " oyuncu"));
      injuryBox.appendChild(F.kv(detail.away.name, detail.injuries.away_count + " oyuncu"));
      detail.injuries.home_players.concat(detail.injuries.away_players).slice(0, 8).forEach(function (player) {
        injuryBox.appendChild(el("div", "cn muted", (player.name || "?") + " — " + (player.reason || "")));
      });
      injuryBox.appendChild(el("div", "cn muted", detail.injuries.note || ""));
    } else {
      injuryBox.appendChild(el("div", "muted", detail.injuries.note || "Sakatlık verisi yok"));
    }
    grid.appendChild(injuryBox);

    // --- API prediction ---
    var apiBox = block("API-Football tahmini (ayrı kaynak)");
    if (detail.api_prediction.available) {
      if (detail.api_prediction.winner_name) {
        apiBox.appendChild(F.kv("Öne çıkan", detail.api_prediction.winner_name));
      }
      ["1", "X", "2"].forEach(function (key) {
        if (F.has(detail.api_prediction.percent[key])) {
          apiBox.appendChild(F.kv(key, F.percent(detail.api_prediction.percent[key])));
        }
      });
      if (detail.api_prediction.advice) {
        apiBox.appendChild(el("div", "cn muted", detail.api_prediction.advice));
      }
      apiBox.appendChild(el("div", "cn muted", detail.api_prediction.note || ""));
    } else {
      apiBox.appendChild(el("div", "muted", detail.api_prediction.note || "Veri yok"));
    }
    grid.appendChild(apiBox);

    grid.appendChild(componentsBlock("Güven skoru", detail.confidence,
      "Bu güven skoru nasıl hesaplandı?"));
    grid.appendChild(componentsBlock("Veri kalitesi", detail.data_quality,
      "Hangi veriler var, hangileri eksik?"));

    container.appendChild(grid);

    // --- Aciklama ---
    if (detail.explanation && detail.explanation.length) {
      var expBox = block("Analiz özeti");
      var list = el("ul", "explist");
      detail.explanation.forEach(function (line) { list.appendChild(el("li", null, line)); });
      expBox.appendChild(list);
      container.appendChild(el("div", null, ""));
      container.appendChild(expBox);
    }
  };

  global.UI = UI;
})(window);
