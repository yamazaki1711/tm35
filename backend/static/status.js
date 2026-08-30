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
  var COLOR_MUTED = "#5a6472";

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

  function buildTrend(container, trend) {
    var pace = (trend && trend.pace) || [];
    var lag = (trend && trend.baseline_lag) || [];
    if (Math.max(pace.length, lag.length) < 2) {
      var lines = [];
      if (pace.length) lines.push('по темпу: ' + window.TM35_RU_DATE.fmtDMY(pace[pace.length - 1].forecast_date));
      if (lag.length) lines.push('по плану+просрочке: ' + window.TM35_RU_DATE.fmtDMY(lag[lag.length - 1].forecast_date));
      container.innerHTML = '<div class="empty-note">Копится по одной точке в неделю — пока есть только' +
        ' первая неделя (' + (lines.join(', ') || 'нет данных') + '), для линии тренда нужно минимум две. Зайдите через неделю.</div>';
      return;
    }
    var W = 900, H = 260, PAD_L = 90, PAD_R = 20, PAD_T = 20, PAD_B = 40;
    var allWeeks = [];
    pace.forEach(function (p) { allWeeks.push(p.week); });
    lag.forEach(function (p) { if (allWeeks.indexOf(p.week) === -1) allWeeks.push(p.week); });
    allWeeks.sort();
    var allDates = pace.concat(lag).map(function (p) { return toDate(p.forecast_date); });
    var minD = new Date(Math.min.apply(null, allDates));
    var maxD = new Date(Math.max.apply(null, allDates));
    var span = Math.max(1, dayDiff(minD, maxD));

    function xs(week) { return PAD_L + allWeeks.indexOf(week) / Math.max(1, allWeeks.length - 1) * (W - PAD_L - PAD_R); }
    function ys(d) { return PAD_T + dayDiff(minD, d) / span * (H - PAD_T - PAD_B); }

    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, width: "100%", style: "max-width:" + W + "px" });
    allWeeks.forEach(function (w) {
      var x = xs(w);
      var lbl = el("text", { x: x, y: H - PAD_B + 16, "text-anchor": "middle", "font-size": 14, fill: COLOR_MUTED });
      lbl.textContent = fmtDM(toDate(w));  // w — дата понедельника этой недели (ГГГГ-ММ-ДД из backend), не номер недели
      svg.appendChild(lbl);
    });
    function drawLine(points, color) {
      if (points.length < 1) return;
      var pts = points.map(function (p) { return xs(p.week) + "," + ys(toDate(p.forecast_date)); }).join(" ");
      if (points.length > 1) svg.appendChild(el("polyline", { points: pts, fill: "none", stroke: color, "stroke-width": 2.5 }));
      points.forEach(function (p) {
        svg.appendChild(el("circle", { cx: xs(p.week), cy: ys(toDate(p.forecast_date)), r: 3.5, fill: color }));
      });
    }
    // Y — подписи дат по левому краю. Названия рядов — не текстом в углу
    // SVG (при узком диапазоне дат налезали на первую подпись оси, особенно
    // с крупным шрифтом), а обычной HTML-легендой под графиком, как у
    // остальных диаграмм на этой странице.
    var lastLabelText = null;
    for (var i = 0; i <= 4; i++) {
      var dd = new Date(minD.getTime() + span / 4 * i * 86400000);
      var yy = ys(dd);
      svg.appendChild(el("line", { x1: PAD_L, x2: W - PAD_R, y1: yy, y2: yy, stroke: "#eef1f3", "stroke-width": 1 }));
      var text = fmtDM(dd);
      // При маленьком диапазоне (мало недель накоплено) соседние деления
      // округляются до одной и той же календарной даты — не повторять
      // подпись подряд, гридлинию всё равно рисуем.
      if (text === lastLabelText) continue;
      lastLabelText = text;
      var l2 = el("text", { x: PAD_L - 8, y: yy + 3, "text-anchor": "end", "font-size": 14, fill: COLOR_MUTED });
      l2.textContent = text;
      svg.appendChild(l2);
    }
    drawLine(pace, COLOR_FACT);
    drawLine(lag, COLOR_FORECAST);

    container.innerHTML = "";
    container.appendChild(svg);
    var legend = document.createElement("div");
    legend.className = "chart-legend";
    legend.innerHTML =
      '<span><i style="background:' + COLOR_FACT + '"></i>по темпу</span>' +
      '<span><i style="background:' + COLOR_FORECAST + '"></i>по плану+просрочке</span>';
    container.appendChild(legend);
  }

  document.addEventListener("DOMContentLoaded", function () {
    var data = window.TM35_STATUS;
    if (!data) return;
    var scEl = document.getElementById("scurve-chart");
    var hEl = document.getElementById("hist-chart");
    var tEl = document.getElementById("trend-chart");
    if (scEl) buildSCurve(scEl, data);
    if (hEl) buildHistogram(hEl, data);
    if (tEl) buildTrend(tEl, data.trend);
  });
})();
