/* Единый виджет ввода даты — ДД.ММ.ГГГГ с маской + календарь-выбор.
   Не полагается на локаль браузера (нативный <input type="date"> её
   показывает по настройкам ОС/браузера пользователя, не по <html lang>) —
   отображение всегда одинаковое у всех пользователей.

   Разметка (см. любой шаблон с data-dm-picker):
     <span class="dm-picker" data-dm-picker>
       <input type="text" class="dm-text" placeholder="ДД.ММ.ГГГГ">
       <button type="button" class="dm-btn">📅</button>
       <input type="hidden" id="..." name="..." value="{ISO или пусто}">
       <div class="dm-cal" hidden></div>
     </span>

   Хранимое/передаваемое значение (id/name на hidden-инпуте, form submit,
   .value из JS) — всегда ISO YYYY-MM-DD, как у нативного type="date".
   Существующий код, читающий .value по id или слушающий 'change' на этом
   id, продолжает работать без изменений. */
(function () {
  var MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"];
  var WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

  function pad2(n) { return (n < 10 ? "0" : "") + n; }

  function isoToDmy(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
    return m ? m[3] + "." + m[2] + "." + m[1] : "";
  }

  function dmyToIso(dmy) {
    var m = /^(\d{2})\.(\d{2})\.(\d{4})$/.exec(dmy || "");
    if (!m) return null;
    var day = parseInt(m[1], 10), month = parseInt(m[2], 10), year = parseInt(m[3], 10);
    if (month < 1 || month > 12) return null;
    var d = new Date(Date.UTC(year, month - 1, day));
    if (d.getUTCFullYear() !== year || d.getUTCMonth() !== month - 1 || d.getUTCDate() !== day) return null;
    return year + "-" + pad2(month) + "-" + pad2(day);
  }

  function maskDigits(raw) {
    var digits = raw.replace(/\D/g, "").slice(0, 8);
    var out = digits.slice(0, 2);
    if (digits.length > 2) out += "." + digits.slice(2, 4);
    if (digits.length > 4) out += "." + digits.slice(4, 8);
    return out;
  }

  function init(root) {
    var textEl = root.querySelector(".dm-text");
    var btnEl = root.querySelector(".dm-btn");
    var hiddenEl = root.querySelector("input[type=hidden]");
    var calEl = root.querySelector(".dm-cal");
    if (!textEl || !hiddenEl || !calEl) return;

    var viewYear, viewMonth;

    function syncTextFromHidden() {
      textEl.value = isoToDmy(hiddenEl.value);
      textEl.classList.remove("dm-invalid");
    }

    function setValue(iso, opts) {
      var changed = hiddenEl.value !== (iso || "");
      hiddenEl.value = iso || "";
      syncTextFromHidden();
      if (changed) {
        hiddenEl.dispatchEvent(new Event("change", { bubbles: true }));
      } else if (opts && opts.forceEvent) {
        hiddenEl.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }

    function commitTextInput() {
      var raw = textEl.value.trim();
      if (!raw) { setValue(""); return; }
      var iso = dmyToIso(raw);
      if (iso) {
        setValue(iso);
      } else {
        textEl.classList.add("dm-invalid");
      }
    }

    textEl.addEventListener("input", function () {
      var pos = textEl.selectionStart;
      var before = textEl.value;
      textEl.value = maskDigits(textEl.value);
      var delta = textEl.value.length - before.length;
      textEl.selectionStart = textEl.selectionEnd = Math.max(0, pos + delta);
      textEl.classList.remove("dm-invalid");
    });
    textEl.addEventListener("blur", commitTextInput);
    textEl.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { commitTextInput(); closeCal(); }
      if (e.key === "Escape") { closeCal(); }
    });

    function closeCal() {
      calEl.hidden = true;
    }
    function openCal() {
      var iso = hiddenEl.value;
      var base = /^\d{4}-\d{2}-\d{2}$/.test(iso) ? new Date(iso + "T00:00:00Z") : new Date();
      viewYear = base.getUTCFullYear();
      viewMonth = base.getUTCMonth();
      renderCal();
      calEl.hidden = false;
    }
    btnEl.addEventListener("click", function (e) {
      e.preventDefault();
      if (calEl.hidden) openCal(); else closeCal();
    });
    document.addEventListener("click", function (e) {
      // composedPath(), не root.contains(e.target) — клик по ‹/› пересобирает
      // календарь (renderCal→innerHTML=""), и e.target (старая, уже
      // отсоединённая от DOM кнопка) перестаёт быть потомком root к моменту
      // проверки; contains() на неё всегда даёт false, из-за чего календарь
      // закрывался сразу при смене месяца. composedPath() — снимок пути на
      // момент диспатча события, не зависит от последующей мутации DOM.
      var path = typeof e.composedPath === "function" ? e.composedPath() : [];
      if (path.indexOf(root) === -1) closeCal();
    });

    function renderCal() {
      calEl.innerHTML = "";
      var header = document.createElement("div");
      header.className = "dm-cal-header";
      var prev = document.createElement("button");
      prev.type = "button"; prev.className = "dm-cal-nav"; prev.textContent = "‹";
      var next = document.createElement("button");
      next.type = "button"; next.className = "dm-cal-nav"; next.textContent = "›";
      var title = document.createElement("span");
      title.className = "dm-cal-title";
      title.textContent = MONTHS[viewMonth] + " " + viewYear;
      prev.addEventListener("click", function (e) {
        e.preventDefault();
        viewMonth--; if (viewMonth < 0) { viewMonth = 11; viewYear--; }
        renderCal();
      });
      next.addEventListener("click", function (e) {
        e.preventDefault();
        viewMonth++; if (viewMonth > 11) { viewMonth = 0; viewYear++; }
        renderCal();
      });
      header.appendChild(prev); header.appendChild(title); header.appendChild(next);
      calEl.appendChild(header);

      var grid = document.createElement("div");
      grid.className = "dm-cal-grid";
      WEEKDAYS.forEach(function (w) {
        var wd = document.createElement("div");
        wd.className = "dm-cal-wd"; wd.textContent = w;
        grid.appendChild(wd);
      });

      var firstOfMonth = new Date(Date.UTC(viewYear, viewMonth, 1));
      var startOffset = (firstOfMonth.getUTCDay() + 6) % 7; // Monday=0
      var daysInMonth = new Date(Date.UTC(viewYear, viewMonth + 1, 0)).getUTCDate();
      var todayIso = new Date().toISOString().slice(0, 10);
      var selectedIso = hiddenEl.value;

      for (var i = 0; i < startOffset; i++) {
        grid.appendChild(document.createElement("div"));
      }
      for (var day = 1; day <= daysInMonth; day++) {
        var iso = viewYear + "-" + pad2(viewMonth + 1) + "-" + pad2(day);
        var cell = document.createElement("button");
        cell.type = "button";
        cell.className = "dm-cal-day";
        if (iso === todayIso) cell.classList.add("dm-cal-today");
        if (iso === selectedIso) cell.classList.add("dm-cal-selected");
        cell.textContent = String(day);
        cell.addEventListener("click", function (pickedIso) {
          return function (e) { e.preventDefault(); setValue(pickedIso); closeCal(); };
        }(iso));
        grid.appendChild(cell);
      }
      calEl.appendChild(grid);
    }

    syncTextFromHidden();
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-dm-picker]").forEach(init);
  });
})();
