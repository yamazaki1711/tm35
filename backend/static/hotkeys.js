/* Ctrl+Enter (Cmd+Enter на Mac) — дублирует кнопку сохранения на любой
   странице с формой (координатор, 04.09.2026; заменено с Ctrl+S на
   Ctrl+Enter 04.09.2026 — Ctrl+S зарезервирован самим браузером под
   «Сохранить страницу как…», и когда подходящая форма не находилась,
   хоткей молча откатывался на это системное действие вместо сохранения
   записи). Правило: если фокус внутри формы — срабатывает её кнопка;
   если фокус не в форме и на странице она одна — срабатывает эта
   единственная форма. Формы фильтров (method=get, без кнопки
   сохранения) не считаются — так фильтр-селекты с onchange не
   перехватывают хоткей случайно.

   Не все "Сохранить"-кнопки — нативный <button type=submit> формы: часть
   страниц (например, «Ввод по разделам ИД») шлёт данные через fetch по
   клику на <button type=button>. Для них кнопка помечается атрибутом
   data-hotkey-save в разметке страницы — скрипт ищет его в первую
   очередь, до type=submit.

   Видимость (offsetParent !== null) — защита от срабатывания на кнопке
   внутри скрытого/ещё не открытого модального окна.

   Формы внутри <nav> (сейчас единственная — «Выйти») исключены явно:
   без этого исключения на любой странице без своей POST-формы (/rsk,
   /dashboard, /id-packages…) форма выхода становится «единственной на
   странице» и Ctrl+Enter вне поля ввода тихо разлогинивает; подсказка
   «Ctrl+Enter» при этом ещё и висела в шапке рядом с «Выйти» на каждой
   странице сайта — обе беды нашёл координатор 06.09.2026. */
(function () {
  function isVisible(el) {
    return !!(el && el.offsetParent !== null && !el.disabled);
  }

  function isSaveForm(form) {
    return form && form.tagName === "FORM" && (form.method || "get").toLowerCase() === "post" && !form.closest("nav");
  }

  function findButton(form) {
    if (!form) return null;
    var marked = form.querySelector("[data-hotkey-save]");
    if (isVisible(marked)) return marked;
    var native = form.querySelector('button[type="submit"], input[type="submit"]');
    if (isVisible(native)) return native;
    return null;
  }

  function allSaveForms() {
    return Array.prototype.filter.call(document.forms, function (f) {
      return isSaveForm(f) && findButton(f);
    });
  }

  document.addEventListener("keydown", function (e) {
    var key = (e.key || "").toLowerCase();
    if (key !== "enter" || !(e.ctrlKey || e.metaKey) || e.shiftKey || e.altKey) return;

    var active = document.activeElement;
    var form = active && active.form ? active.form : (active && active.closest ? active.closest("form") : null);
    var btn = isSaveForm(form) ? findButton(form) : null;

    if (!btn) {
      var candidates = allSaveForms();
      if (candidates.length === 1) btn = findButton(candidates[0]);
    }

    if (!btn) return;
    e.preventDefault();
    btn.click();
  });

  // Подсказка "Ctrl+Enter"/"⌘Enter" рядом с кнопкой — не на каждой мелкой
  // кнопке формы (например "Убрать" в строке таблицы — тоже технически
  // submit своей маленькой формы, хоткей на неё сработает при фокусе, но
  // подпись рядом с каждой такой кнопкой в каждой строке была бы визуальным
  // шумом): подсказываем только у явно помеченных data-hotkey-save и у
  // обычных (не .btn-secondary) кнопок сохранения. Не показываем на touch —
  // сочетание клавиш там недоступно физически.
  document.addEventListener("DOMContentLoaded", function () {
    if (window.matchMedia && window.matchMedia("(hover: none), (pointer: coarse)").matches) return;
    var isMac = /Mac|iPod|iPhone|iPad/.test(navigator.platform || "");
    var label = isMac ? "⌘Enter" : "Ctrl+Enter";

    var buttons = [];
    allSaveForms().forEach(function (form) {
      var btn = findButton(form);
      if (!btn) return;
      if (btn.hasAttribute("data-hotkey-save") || !btn.classList.contains("btn-secondary")) {
        buttons.push(btn);
      }
    });

    buttons.forEach(function (btn) {
      if (btn.dataset.hotkeyHinted) return;
      btn.dataset.hotkeyHinted = "1";
      var hint = document.createElement("span");
      hint.className = "hotkey-hint";
      hint.textContent = label;
      btn.insertAdjacentElement("afterend", hint);
    });
  });
})();
