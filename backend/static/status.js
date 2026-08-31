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

  function buildTrend(container, trend, directiveDeadlineIso) {
    var pace = (trend && trend.pace) || [];
    var lag = (trend && trend.baseline_lag) || [];

    if (!directiveDeadlineIso) {
      container.innerHTML = '<div class="empty-note">Директивный срок не задан — задайте его выше' +
        ', чтобы увидеть отклонение по неделям.</div>';
      return;
    }
    var deadline = toDate(directiveDeadlineIso);
    // Пункт 3, 30.08.2026: единое соглашение знаков с плиткой "Отклонение
    // прогноза от директивного срока" наверху страницы — там просрочка уже
    // считалась ПОЛОЖИТЕЛЬНЫМ числом (forecast - deadline). Геометрия
    // графика (что выше/ниже нуля) не меняется — позже по времени всё
    // так же ниже, это ось дат. Меняются только ЦИФРЫ подписей: geomValue
    // двигает точку по Y (как раньше), overdueDays — то, что печатается.
    function geomValue(p) { return dayDiff(toDate(p.forecast_date), deadline); }
    function overdueDays(p) { return -geomValue(p); }

    if (Math.max(pace.length, lag.length) < 2) {
      var lines = [];
      if (pace.length) lines.push('по темпу: ' + window.TM35_RU_DATE.fmtDMY(pace[pace.length - 1].forecast_date));
      if (lag.length) lines.push('по плану+просрочке: ' + window.TM35_RU_DATE.fmtDMY(lag[lag.length - 1].forecast_date));
      container.innerHTML = '<div class="empty-note">Копится по одной точке в неделю — пока есть только' +
        ' первая неделя (' + (lines.join(', ') || 'нет данных') + '), для линии тренда нужно минимум две. Зайдите через неделю.</div>';
      return;
    }

    // PAD_R с запасом под самую длинную подпись конца линии
    // ("План+просрочка −32") — 130 обрезалось SVG-вьюбоксом, живой скриншот
    // это поймал (30.08.2026).
    var W = 900, H = 280, PAD_L = 90, PAD_R = 175, PAD_T = 20, PAD_B = 40;
    var allWeeks = [];
    pace.forEach(function (p) { allWeeks.push(p.week); });
    lag.forEach(function (p) { if (allWeeks.indexOf(p.week) === -1) allWeeks.push(p.week); });
    allWeeks.sort();

    var values = pace.concat(lag).map(geomValue);
    var rawMin = Math.min.apply(null, values.concat([0]));
    var rawMax = Math.max.apply(null, values.concat([0]));
    var span0 = Math.max(1, rawMax - rawMin);
    var pad = Math.max(3, span0 * 0.18);
    var yMin = rawMin - pad, yMax = rawMax + pad;

    function xs(week) { return PAD_L + allWeeks.indexOf(week) / Math.max(1, allWeeks.length - 1) * (W - PAD_L - PAD_R); }
    function ys(v) { return PAD_T + (yMax - v) / (yMax - yMin) * (H - PAD_T - PAD_B); }

    var COLOR_PACE = cssVar("--trend-pace", "#eb6834");
    var COLOR_PLAN2 = cssVar("--trend-plan", "#2a78d6");
    var COLOR_DEADLINE = cssVar("--trend-deadline", "#d03b3b");

    var svg = el("svg", {
      viewBox: "0 0 " + W + " " + H, width: "100%", style: "max-width:" + W + "px",
      role: "img", "aria-label": "График отклонения прогноза от директивного срока по неделям",
    });

    // Зона просрочки (ниже нуля) — слабая заливка, не спорит с линиями.
    var zeroY = ys(0);
    var zoneH = (H - PAD_B) - zeroY;
    if (zoneH > 0) {
      svg.appendChild(el("rect", {
        x: PAD_L, y: zeroY, width: W - PAD_L - PAD_R, height: zoneH,
        fill: COLOR_DEADLINE, "fill-opacity": 0.075,
      }));
    }

    // Сетка/подписи Y — "круглый" шаг в днях, ноль подписан "срок".
    var rangeSpan = yMax - yMin;
    var niceSteps = [1, 2, 5, 10, 15, 20, 25, 50, 100];
    var step = niceSteps[niceSteps.length - 1];
    for (var si = 0; si < niceSteps.length; si++) {
      if (niceSteps[si] >= rangeSpan / 5) { step = niceSteps[si]; break; }
    }
    var firstTick = Math.ceil(yMin / step) * step;
    for (var v = firstTick; v <= yMax + 0.001; v += step) {
      var rv = Math.round(v);          // геометрия (положение по Y) — не меняем
      var dispRv = -rv;                // подпись — просрочка положительная
      var ty = ys(v);
      svg.appendChild(el("line", { x1: PAD_L, x2: W - PAD_R, y1: ty, y2: ty, stroke: "#eef1f3", "stroke-width": 1 }));
      var tl = el("text", { x: PAD_L - 10, y: ty + 4, "text-anchor": "end", "font-size": 13, fill: COLOR_MUTED });
      tl.textContent = (rv === 0) ? "срок" : (dispRv > 0 ? "+" + dispRv : String(dispRv));
      if (rv === 0) tl.setAttribute("font-weight", "700");
      svg.appendChild(tl);
    }

    // Ось X — недели (дата понедельника, не номер недели — правило проекта).
    allWeeks.forEach(function (w) {
      var x = xs(w);
      var lbl = el("text", { x: x, y: H - PAD_B + 18, "text-anchor": "middle", "font-size": 13, fill: COLOR_MUTED });
      lbl.textContent = fmtDM(toDate(w));
      svg.appendChild(lbl);
    });

    // Линия директивного срока — поверх зоны/сетки, подписана датой справа.
    svg.appendChild(el("line", {
      x1: PAD_L, x2: W - PAD_R, y1: zeroY, y2: zeroY, stroke: COLOR_DEADLINE,
      "stroke-width": 2, "stroke-dasharray": "7,4",
    }));
    var deadlineLbl = el("text", {
      x: W - PAD_R + 8, y: zeroY + 4, "font-size": 13, "font-weight": 700, fill: COLOR_DEADLINE,
    });
    deadlineLbl.textContent = window.TM35_RU_DATE.fmtDMY(directiveDeadlineIso);
    svg.appendChild(deadlineLbl);

    var tooltip = ensureTooltip();

    function drawSeries(points, color, seriesName) {
      if (!points.length) return null;
      var pts = points.map(function (p) { return { x: xs(p.week), y: ys(geomValue(p)), p: p }; });
      if (pts.length > 1) {
        svg.appendChild(el("polyline", {
          points: pts.map(function (pt) { return pt.x + "," + pt.y; }).join(" "),
          fill: "none", stroke: color, "stroke-width": 2.5,
        }));
      }
      pts.forEach(function (pt) {
        var dv = Math.round(overdueDays(pt.p));
        var dvTxt = (dv >= 0 ? "+" + dv : String(dv)) + " дн.";
        var circle = el("circle", {
          cx: pt.x, cy: pt.y, r: 5, fill: color, stroke: "#fff", "stroke-width": 1.5,
          tabindex: "0",
        });
        circle.setAttribute("role", "img");
        circle.setAttribute("aria-label",
          seriesName + ", неделя " + fmtDM(toDate(pt.p.week)) + ", прогноз " +
          window.TM35_RU_DATE.fmtDMY(pt.p.forecast_date) + ", отклонение " + dvTxt);
        circle.classList.add("trend-point");
        var showTip = function () {
          tooltip.innerHTML = "<b>" + seriesName + "</b><br>неделя замера: " + fmtDM(toDate(pt.p.week)) +
            "<br>прогноз: " + window.TM35_RU_DATE.fmtDMY(pt.p.forecast_date) +
            "<br>отклонение: " + dvTxt;
          var rect = circle.getBoundingClientRect();
          tooltip.style.left = (rect.left + rect.width / 2) + "px";
          tooltip.style.top = (rect.top - 10) + "px";
          tooltip.style.transform = "translate(-50%,-100%)";
          tooltip.classList.add("visible");
        };
        var hideTip = function () { tooltip.classList.remove("visible"); };
        circle.addEventListener("mouseenter", showTip);
        circle.addEventListener("mouseleave", hideTip);
        circle.addEventListener("focus", showTip);
        circle.addEventListener("blur", hideTip);
        svg.appendChild(circle);
      });
      return pts[pts.length - 1];
    }

    // Пункт 3, 30.08.2026: "По темпу" здесь — НЕ то же число, что в плитке
    // "Отклонение..." наверху страницы, хотя формула та же (прогноз минус
    // срок). Плитка считает на СЕЙЧАС, эта точка — снимок на начало ISO-
    // недели (пишется не чаще раза в неделю, см. record_forecast_snapshot).
    // Формулы совпадают, момент времени — нет: живьём разошлись 32 (снимок
    // с понедельника 24.08) и 17 (сегодняшний живой расчёт) — не баг,
    // координатор попросил разницу сделать видимой из подписи, не считать
    // равными.
    var lastPace = drawSeries(pace, COLOR_PACE, "По темпу (на начало недели)");
    var lastLag = drawSeries(lag, COLOR_PLAN2, "План + просрочка (на начало недели)");

    // Подписи у концов линий, с текущим значением — разводим по вертикали,
    // если серии близки по значению (иначе текст налезает друг на друга).
    var endLabels = [];
    if (lastPace) endLabels.push({ pt: lastPace, color: COLOR_PACE, name: "По темпу", dv: Math.round(overdueDays(lastPace.p)) });
    if (lastLag) endLabels.push({ pt: lastLag, color: COLOR_PLAN2, name: "План+просрочка", dv: Math.round(overdueDays(lastLag.p)) });
    if (endLabels.length === 2 && Math.abs(endLabels[0].pt.y - endLabels[1].pt.y) < 16) {
      if (endLabels[0].pt.y <= endLabels[1].pt.y) { endLabels[0].dy = -6; endLabels[1].dy = 12; }
      else { endLabels[0].dy = 12; endLabels[1].dy = -6; }
    } else {
      endLabels.forEach(function (l) { l.dy = 4; });
    }
    endLabels.forEach(function (l) {
      var t = el("text", { x: l.pt.x + 9, y: l.pt.y + l.dy, "font-size": 13, "font-weight": 700, fill: l.color });
      t.textContent = l.name + " " + (l.dv >= 0 ? "+" + l.dv : l.dv);
      svg.appendChild(t);
    });

    container.innerHTML = "";
    container.appendChild(svg);
    var legend = document.createElement("div");
    legend.className = "chart-legend";
    legend.innerHTML =
      '<span><i style="background:' + COLOR_PACE + '"></i>по темпу (на начало недели)</span>' +
      '<span><i style="background:' + COLOR_PLAN2 + '"></i>план + просрочка (на начало недели)</span>' +
      '<span><i style="background:' + COLOR_DEADLINE + '"></i>директивный срок ' +
      window.TM35_RU_DATE.fmtDMY(directiveDeadlineIso) + '</span>';
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
    if (tEl) buildTrend(tEl, data.trend, data.directive_deadline);
  });
})();
