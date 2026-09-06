/* Экран "Успеваем?" — S-кривая, гистограмма численности, тренд прогноза.
   Чистый SVG, без внешних библиотек — тот же минимальный стек, что и в
   graфике работ (gantt.html). Данные приходят готовыми из backend
   (window.TM35_STATUS), здесь только геометрия. */
(function () {
  var SVGNS = "http://www.w3.org/2000/svg";
  var COLOR_PLAN = "#0b6fb0";
  var COLOR_FACT = "#1a7f37";
  var COLOR_FORECAST = "#9a6700";
  var COLOR_BAD = "#c0392b";
  // Контраст поднят 30.08.2026 вместе с --c-text-3 в style.css (тот же
  // приглушённый текст, здесь — раз рисуется в SVG атрибутами, а не CSS).
  var COLOR_MUTED = "#5b6376";

  function cssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v && v.trim()) || fallback;
  }

  var _tooltipEl = null;
  function ensureTooltip() {
    if (_tooltipEl) return _tooltipEl;
    _tooltipEl = document.createElement("div");
    _tooltipEl.className = "chart-tooltip";
    document.body.appendChild(_tooltipEl);
    return _tooltipEl;
  }

  function el(tag, attrs) {
    var e = document.createElementNS(SVGNS, tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }

  function toDate(s) { return new Date(s + "T00:00:00Z"); }
  function dayDiff(a, b) { return (b - a) / 86400000; }

  function fmtDate(d) {
    return d.getUTCFullYear() + "-" + String(d.getUTCMonth() + 1).padStart(2, "0") + "-" +
      String(d.getUTCDate()).padStart(2, "0");
  }

  // Подпись оси — ДД.ММ (без года, короче, но не ISO-порядок MM-DD).
  function fmtDM(d) {
    return String(d.getUTCDate()).padStart(2, "0") + "." + String(d.getUTCMonth() + 1).padStart(2, "0");
  }

  function buildSCurve(container, data) {
    var series = data.series || [];
    if (series.length === 0) {
      container.innerHTML = '<div class="empty-note">Нет календарных данных для построения кривой.</div>';
      return;
    }
    var W = 900, H = 340, PAD_L = 55, PAD_R = 20, PAD_T = 20, PAD_B = 40;
    var dates = series.map(function (r) { return toDate(r.date); });
    var allDates = dates.slice();
    if (data.last_actual_date) allDates.push(toDate(data.last_actual_date));
    if (data.forecast_pace_date) allDates.push(toDate(data.forecast_pace_date));
    var minD = new Date(Math.min.apply(null, allDates));
    var maxD = new Date(Math.max.apply(null, allDates));
    var totalDays = Math.max(1, dayDiff(minD, maxD));

    var yMax = Math.max(
      series[series.length - 1].bcws_cum || 0,
      data.total_trudoemkost || 0
    ) * 1.08 || 1;

    function xs(d) { return PAD_L + dayDiff(minD, d) / totalDays * (W - PAD_L - PAD_R); }
    function ys(v) { return H - PAD_B - (v / yMax) * (H - PAD_T - PAD_B); }

    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, width: "100%", style: "max-width:" + W + "px" });

    // сетка по Y
    for (var g = 0; g <= 4; g++) {
      var gv = yMax / 4 * g;
      var gy = ys(gv);
      svg.appendChild(el("line", { x1: PAD_L, x2: W - PAD_R, y1: gy, y2: gy, stroke: "#e2e6ea", "stroke-width": 1 }));
      var lbl = el("text", { x: PAD_L - 8, y: gy + 4, "text-anchor": "end", "font-size": 14, fill: COLOR_MUTED });
      lbl.textContent = Math.round(gv);
      svg.appendChild(lbl);
    }

    // ось X — несколько подписей дат
    var ticks = 5;
    for (var t = 0; t <= ticks; t++) {
      var td = new Date(minD.getTime() + totalDays / ticks * t * 86400000);
      var tx = xs(td);
      svg.appendChild(el("line", { x1: tx, x2: tx, y1: PAD_T, y2: H - PAD_B, stroke: "#f0f2f4", "stroke-width": 1 }));
      var tl = el("text", { x: tx, y: H - PAD_B + 16, "text-anchor": "middle", "font-size": 14, fill: COLOR_MUTED });
      tl.textContent = fmtDM(td);
      svg.appendChild(tl);
    }

    // план нарастающим итогом (BCWS)
    var planPts = series.map(function (r) { return xs(toDate(r.date)) + "," + ys(r.bcws_cum); }).join(" ");
    svg.appendChild(el("polyline", { points: planPts, fill: "none", stroke: COLOR_PLAN, "stroke-width": 2.5 }));

    // факт нарастающим итогом (ACWP) — только там, где actual не null
    var factSeries = series.filter(function (r) { return r.actual !== null; });
    if (factSeries.length) {
      var factPts = factSeries.map(function (r) { return xs(toDate(r.date)) + "," + ys(r.acwp_cum); }).join(" ");
      svg.appendChild(el("polyline", { points: factPts, fill: "none", stroke: COLOR_FACT, "stroke-width": 2.5 }));

      var lastFact = factSeries[factSeries.length - 1];
      var lx = xs(toDate(lastFact.date)), ly = ys(lastFact.acwp_cum);

      // точка "фактически выполнено" (взвешенный % × общая трудоёмкость)
      var by = ys(data.bcwp_point || 0);
      svg.appendChild(el("circle", { cx: lx, cy: by, r: 5, fill: "#a855f7" }));
      var blbl = el("text", { x: lx + 8, y: by - 6, "font-size": 14, fill: "#a855f7" });
      blbl.textContent = "фактически выполнено, чел.-дней: " + Math.round(data.bcwp_point || 0);
      svg.appendChild(blbl);

      // прогнозный хвост — пунктир от последнего факта до прогнозной даты на уровне всего объёма
      if (data.forecast_pace_date) {
        var fx = xs(toDate(data.forecast_pace_date));
        var fy = ys(data.total_trudoemkost || 0);
        svg.appendChild(el("line", {
          x1: lx, y1: ly, x2: fx, y2: fy, stroke: COLOR_FORECAST, "stroke-width": 2,
          "stroke-dasharray": "6,4",
        }));
        svg.appendChild(el("circle", { cx: fx, cy: fy, r: 4, fill: COLOR_FORECAST }));
      }

      // вертикальная линия "сегодня (по данным)"
      svg.appendChild(el("line", {
        x1: lx, x2: lx, y1: PAD_T, y2: H - PAD_B, stroke: COLOR_MUTED, "stroke-width": 1, "stroke-dasharray": "3,3",
      }));
    }

    // линия "весь объём"
    var fullY = ys(data.total_trudoemkost || 0);
    svg.appendChild(el("line", {
      x1: PAD_L, x2: W - PAD_R, y1: fullY, y2: fullY, stroke: "#d6dbe1", "stroke-width": 1, "stroke-dasharray": "2,4",
    }));

    container.innerHTML = "";
    container.appendChild(svg);

    var legend = document.createElement("div");
    legend.className = "chart-legend";
    legend.innerHTML =
      '<span><i style="background:' + COLOR_PLAN + '"></i>плановые трудозатраты нарастающим итогом, чел.-дней</span>' +
      '<span><i style="background:' + COLOR_FACT + '"></i>фактические трудозатраты нарастающим итогом, чел.-дней</span>' +
      '<span><i style="background:#a855f7"></i>фактически выполнено, чел.-дней</span>' +
      '<span><i style="background:' + COLOR_FORECAST + '"></i>прогноз при текущем темпе</span>';
    container.appendChild(legend);
  }

  function buildHistogram(container, data) {
    var series = data.series || [];
    if (series.length === 0) {
      container.innerHTML = '<div class="empty-note">Нет данных.</div>';
      return;
    }
    var barW = 6, gap = 2;
    var W = Math.max(900, series.length * (barW * 2 + gap) + 60);
    var H = 220, PAD_L = 50, PAD_R = 10, PAD_T = 10, PAD_B = 30;
    var yMax = Math.max.apply(null, series.map(function (r) { return Math.max(r.planned || 0, r.actual || 0); })) * 1.15 || 1;
    function ys(v) { return H - PAD_B - (v / yMax) * (H - PAD_T - PAD_B); }

    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, width: "100%", style: "max-width:" + W + "px" });
    for (var g = 0; g <= 3; g++) {
      var gv = yMax / 3 * g, gy = ys(gv);
      svg.appendChild(el("line", { x1: PAD_L, x2: W - PAD_R, y1: gy, y2: gy, stroke: "#eef1f3", "stroke-width": 1 }));
      var lbl = el("text", { x: PAD_L - 6, y: gy + 4, "text-anchor": "end", "font-size": 14, fill: COLOR_MUTED });
      lbl.textContent = Math.round(gv);
      svg.appendChild(lbl);
    }
    series.forEach(function (r, i) {
      var x = PAD_L + i * (barW * 2 + gap);
      var ph = (H - PAD_B) - ys(r.planned || 0);
      svg.appendChild(el("rect", { x: x, y: ys(r.planned || 0), width: barW, height: ph, fill: COLOR_PLAN, opacity: 0.55 }));
      if (r.actual !== null) {
        var ah = (H - PAD_B) - ys(r.actual || 0);
        svg.appendChild(el("rect", { x: x + barW, y: ys(r.actual || 0), width: barW, height: ah, fill: COLOR_FACT, opacity: 0.85 }));
      }
    });
    container.innerHTML = "";
    var wrap = document.createElement("div");
    wrap.className = "table-wrap";
    wrap.appendChild(svg);
    container.appendChild(wrap);
    var legend = document.createElement("div");
    legend.className = "chart-legend";
    legend.innerHTML =
      '<span><i style="background:' + COLOR_PLAN + '"></i>план, чел./день</span>' +
      '<span><i style="background:' + COLOR_FACT + '"></i>факт, чел./день</span>';
    container.appendChild(legend);
  }

  // Иконки статуса — те же path'ы, что уже используются в плашках
  // "в графике"/"риск срыва"/"срыв срока" вверху страницы (status.html) —
  // переиспользованы один в один, чтобы цвет светофора не был единственным
  // носителем смысла нигде на странице, а не только здесь.
  var STATUS_ICONS = {
    ok: '<path d="M20 6 9 17l-5-5"/>',
    risk: '<path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>',
    danger: '<circle cx="12" cy="12" r="10"/><path d="M12 8v5M12 16h.01"/>',
  };
  var STATUS_LABEL = { ok: "в графике", risk: "риск срыва", danger: "срыв срока" };

  // Пороги и названия классов ("ok"/"risk"/"danger") — те же самые, что уже
  // определяют плашку "Отклонение прогноза от директивного срока" наверху
  // страницы (status.html, deviation_days, класс .deviation-badge): <=0 в
  // графике, <=7 риск, >7 срыв. Не изобретаются заново — один и тот же
  // порог и один и тот же CSS-класс для одной и той же величины везде на
  // странице.
  function statusOf(overdueDays) {
    if (overdueDays <= 0) return "ok";
    if (overdueDays <= 7) return "risk";
    return "danger";
  }

  function badgeHtml(status, text) {
    return '<span class="deviation-badge ' + status + '">' +
      '<svg class="icon" viewBox="0 0 24 24">' + STATUS_ICONS[status] + '</svg>' + text + '</span>';
  }

  // Таймлайн-«светофор» вместо линейного графика (координатор, 06.09.2026 —
  // "не обязательно график, нужно просто визуализировать процесс"). Две
  // серии остаются раздельными строками, а не сводятся в одну: это те же
  // два независимых прогноза, которые координатор 30.08.2026 попросил не
  // путать (см. комментарий у record_forecast_snapshot в main.py) — здесь
  // тот же принцип, просто в новой форме.
  function buildTrend(container, trend, directiveDeadlineIso) {
    var pace = (trend && trend.pace) || [];
    var lag = (trend && trend.baseline_lag) || [];

    if (!directiveDeadlineIso) {
      container.innerHTML = '<div class="empty-note">Директивный срок не задан — задайте его выше' +
        ', чтобы увидеть отклонение по неделям.</div>';
      return;
    }
    if (!pace.length && !lag.length) {
      container.innerHTML = '<div class="empty-note">Пока нет ни одной недельной отметки — ' +
        'первая появится в начале следующей недели.</div>';
      return;
    }
    var deadline = toDate(directiveDeadlineIso);
    function overdueDays(p) { return dayDiff(deadline, toDate(p.forecast_date)); }

    var tooltip = ensureTooltip();

    function buildRow(points, seriesName) {
      var row = document.createElement("div");
      row.className = "st-row";

      var head = document.createElement("div");
      head.className = "st-row-head";
      var label = document.createElement("span");
      label.className = "st-row-label";
      label.textContent = seriesName;
      head.appendChild(label);
      if (points.length) {
        var last = points[points.length - 1];
        var dv = Math.round(overdueDays(last));
        var st = statusOf(dv);
        var dvTxt = (dv >= 0 ? "+" + dv : String(dv)) + " дн. на " + fmtDM(toDate(last.week));
        var badge = document.createElement("span");
        badge.innerHTML = badgeHtml(st, STATUS_LABEL[st] + " (" + dvTxt + ")");
        head.appendChild(badge.firstChild);
      }
      row.appendChild(head);

      if (!points.length) {
        var empty = document.createElement("div");
        empty.className = "empty-note";
        empty.textContent = "Пока нет отметок по этой серии.";
        row.appendChild(empty);
        return row;
      }

      var track = document.createElement("div");
      track.className = "st-track";

      var pointsWrap = document.createElement("div");
      pointsWrap.className = "st-points";
      points.forEach(function (p) {
        var dv = Math.round(overdueDays(p));
        var st = statusOf(dv);
        var dvTxt = (dv >= 0 ? "+" + dv : String(dv)) + " дн.";
        var pt = document.createElement("div");
        pt.className = "st-point";
        var dot = document.createElement("button");
        dot.type = "button";
        dot.className = "st-dot st-" + st;
        dot.setAttribute("aria-label",
          seriesName + ", неделя " + fmtDM(toDate(p.week)) + ", прогноз " +
          window.TM35_RU_DATE.fmtDMY(p.forecast_date) + ", отклонение " + dvTxt + ", " + STATUS_LABEL[st]);
        var showTip = function () {
          tooltip.innerHTML = "<b>" + seriesName + "</b><br>неделя замера: " + fmtDM(toDate(p.week)) +
            "<br>прогноз: " + window.TM35_RU_DATE.fmtDMY(p.forecast_date) +
            "<br>отклонение: " + dvTxt + " (" + STATUS_LABEL[st] + ")";
          var rect = dot.getBoundingClientRect();
          tooltip.style.left = (rect.left + rect.width / 2) + "px";
          tooltip.style.top = (rect.top - 10) + "px";
          tooltip.style.transform = "translate(-50%,-100%)";
          tooltip.classList.add("visible");
        };
        var hideTip = function () { tooltip.classList.remove("visible"); };
        dot.addEventListener("mouseenter", showTip);
        dot.addEventListener("mouseleave", hideTip);
        dot.addEventListener("focus", showTip);
        dot.addEventListener("blur", hideTip);
        pt.appendChild(dot);
        var dateLbl = document.createElement("div");
        dateLbl.className = "st-point-date";
        dateLbl.textContent = fmtDM(toDate(p.week));
        pt.appendChild(dateLbl);
        pointsWrap.appendChild(pt);
      });
      track.appendChild(pointsWrap);

      var connector = document.createElement("div");
      connector.className = "st-connector";
      track.appendChild(connector);

      var target = document.createElement("div");
      target.className = "st-target";
      target.innerHTML =
        '<svg class="icon st-target-flag" viewBox="0 0 24 24" aria-hidden="true">' +
        '<path d="M5 22V4M5 4h13l-3 4 3 4H5"/></svg>' +
        '<div class="st-target-date">' + window.TM35_RU_DATE.fmtDMY(directiveDeadlineIso) + '</div>';
      track.appendChild(target);

      row.appendChild(track);
      return row;
    }

    var wrap = document.createElement("div");
    wrap.className = "status-timeline";
    wrap.appendChild(buildRow(pace, "По темпу (на начало недели)"));
    wrap.appendChild(buildRow(lag, "План + просрочка (на начало недели)"));

    var legend = document.createElement("div");
    legend.className = "chart-legend st-legend";
    legend.innerHTML =
      badgeHtml("ok", "в графике") + badgeHtml("risk", "риск срыва (до +7 дн.)") + badgeHtml("danger", "срыв срока (>+7 дн.)");
    wrap.appendChild(legend);

    container.innerHTML = "";
    container.appendChild(wrap);
  }

  document.addEventListener("DOMContentLoaded", function () {
    var data = window.TM35_STATUS;
    if (!data) return;
    var scEl = document.getElementById("scurve-chart");
    var hEl = document.getElementById("hist-chart");
    var tEl = document.getElementById("trend-chart");
    if (scEl) buildSCurve(scEl, data);
    if (hEl) buildHistogram(hEl, data);
    if (tEl) buildTrend(tEl, data.trend, data.directive_deadline);
  });
})();
