/**
 * Bicimlendirme ve GUVENLI DOM yardimcilari.
 *
 * Kritik kural: API'den gelen hicbir metin innerHTML ile basilmaz. Tum
 * metinler textContent ile yazilir (HTML injection'a kapali).
 *
 * Ikinci kritik kural: eksik deger icin 0 veya "-" UYDURULMAZ; ayrik bir
 * "veri yok" gosterimi kullanilir.
 */
(function (global) {
  "use strict";

  var NO_DATA = "veri yok";

  /** Bir HTML elemani olusturur. text her zaman textContent olarak yazilir. */
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  function append(parent) {
    for (var i = 1; i < arguments.length; i++) {
      var child = arguments[i];
      if (child) parent.appendChild(child);
    }
    return parent;
  }

  /** Sayi mi? null/undefined/NaN degilse true. */
  function has(value) {
    return value !== null && value !== undefined && !(typeof value === "number" && isNaN(value));
  }

  /** Oran: 2.1 -> "2.10". Yoksa "veri yok". */
  function odd(value) {
    return has(value) ? Number(value).toFixed(2) : NO_DATA;
  }

  /** Olasilik: 0.613 -> "%61.3". Yoksa "veri yok". */
  function percent(value, digits) {
    if (!has(value)) return NO_DATA;
    return "%" + (Number(value) * 100).toFixed(digits === undefined ? 1 : digits);
  }

  /** Yuzde PUANI: 8.2 -> "+8.2 puan". */
  function points(value) {
    if (!has(value)) return NO_DATA;
    var num = Number(value);
    return (num > 0 ? "+" : "") + num.toFixed(1) + " puan";
  }

  /** Yuzde degisim: -10.42 -> "%-10.42". */
  function changePct(value) {
    if (!has(value)) return NO_DATA;
    var num = Number(value);
    return (num > 0 ? "+" : "") + num.toFixed(2) + "%";
  }

  function score(value, max) {
    if (!has(value)) return NO_DATA;
    return Math.round(Number(value)) + "/" + Math.round(Number(max || 100));
  }

  function integer(value) {
    return has(value) ? String(Math.round(Number(value))) : NO_DATA;
  }

  /** ISO -> "07.09.2026 20:00" (tarayici yerel saatinden bagimsiz, TR ofsetiyle). */
  function dateTime(iso) {
    if (!iso) return NO_DATA;
    var parsed = new Date(iso);
    if (isNaN(parsed.getTime())) return NO_DATA;
    try {
      var parts = new Intl.DateTimeFormat("tr-TR", {
        timeZone: "Europe/Istanbul",
        day: "2-digit", month: "2-digit", year: "numeric",
        hour: "2-digit", minute: "2-digit", hour12: false
      }).formatToParts(parsed);
      var map = {};
      parts.forEach(function (part) { map[part.type] = part.value; });
      return map.day + "." + map.month + "." + map.year + " " + map.hour + ":" + map.minute;
    } catch (error) {
      return parsed.toISOString().slice(0, 16).replace("T", " ");
    }
  }

  function clockNow() {
    var now = new Date();
    return String(now.getHours()).padStart(2, "0") + ":" +
           String(now.getMinutes()).padStart(2, "0") + ":" +
           String(now.getSeconds()).padStart(2, "0");
  }

  /** Sonuc etiketi: "1" -> ev sahibi adi. */
  function outcomeLabel(key, homeName, awayName) {
    if (key === "1") return homeName;
    if (key === "2") return awayName;
    if (key === "X") return "Beraberlik";
    return key;
  }

  function outcomeShort(key) {
    if (key === "1") return "MS 1";
    if (key === "X") return "MS X";
    if (key === "2") return "MS 2";
    return key;
  }

  /** Bir olasilik sozlugunden en yuksek olani dondurur. */
  function best(map) {
    if (!map) return null;
    var bestKey = null;
    var bestValue = -Infinity;
    Object.keys(map).forEach(function (key) {
      if (map[key] > bestValue) { bestValue = map[key]; bestKey = key; }
    });
    return bestKey === null ? null : { key: bestKey, value: bestValue };
  }

  /** Veri kalitesi rengi. */
  function qualityClass(value) {
    var config = global.MACANALIZ_CONFIG;
    if (value >= config.goodDataQuality) return "";
    if (value >= config.weakDataQuality) return "amber";
    return "red";
  }

  /** Kucuk bir ilerleme cubugu. */
  function meter(value, max, extraClass) {
    var wrap = el("div", "meter" + (extraClass ? " " + extraClass : ""));
    var bar = el("span");
    var ratio = has(value) && max ? Math.max(0, Math.min(1, value / max)) : 0;
    bar.style.width = (ratio * 100).toFixed(1) + "%";
    wrap.appendChild(bar);
    return wrap;
  }

  /** "etiket: deger" satiri. */
  function kv(label, value, valueClass) {
    var row = el("div", "kv");
    row.appendChild(el("span", "k", label));
    row.appendChild(el("span", "v" + (valueClass ? " " + valueClass : ""), value));
    return row;
  }

  function miniStat(label, value) {
    var row = el("div", "mini");
    row.appendChild(el("span", "k", label));
    row.appendChild(el("span", "v", value));
    return row;
  }

  function badge(text, kind) {
    return el("span", "badge" + (kind ? " " + kind : ""), text);
  }

  global.Fmt = {
    NO_DATA: NO_DATA,
    el: el,
    clear: clear,
    append: append,
    has: has,
    odd: odd,
    percent: percent,
    points: points,
    changePct: changePct,
    score: score,
    integer: integer,
    dateTime: dateTime,
    clockNow: clockNow,
    outcomeLabel: outcomeLabel,
    outcomeShort: outcomeShort,
    best: best,
    qualityClass: qualityClass,
    meter: meter,
    kv: kv,
    miniStat: miniStat,
    badge: badge
  };
})(window);
