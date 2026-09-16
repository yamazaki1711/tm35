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

  // Подсказка "Ctrl+Enter"/"⌘Enter" рядом с кнопкой — раньше отбиралась по
  // имени класса (не .btn-secondary) как замена настоящему условию, и это
  // не то же самое: /id-rsk-link рендерит ~20 одинаковых POST-форм
  // «Прикрепить» по одной на строку — по имени класса каждая проходила
  // фильтр, получалась подсказка под каждой строкой, а хоткей там ничего
  // не делает (фокус после поиска остаётся в форме фильтра, GET, кандидат
  // на срабатывание не один — см. правило вверху файла). Подсказка обязана
  // повторять УСЛОВИЕ хоткея, а не гадать по классу: он однозначно бьёт по
  // цели независимо от фокуса, только когда allSaveForms() возвращает
  // ровно одну форму — это и есть единственный случай, когда подсказку
  // можно показывать. На странице с несколькими формами сохранения
  // подсказка не показывается вообще ни у одной — хоткей там по-прежнему
  // работает при фокусе внутри конкретной формы, просто не разрекламирован
  // без адреса. Кнопка-действие в строке таблицы (например «Убрать») сама
  // по себе технически submit маленькой формы — на странице с несколькими
  // такими строками их несколько, значит allSaveForms().length !== 1, и
  // подсказка так и не появляется, отдельного условия не нужно. Не
  // показываем на touch — сочетание клавиш там недоступно физически.
  document.addEventListener("DOMContentLoaded", function () {
    if (window.matchMedia && window.matchMedia("(hover: none), (pointer: coarse)").matches) return;

    var candidates = allSaveForms();
    if (candidates.length !== 1) return;
    var btn = findButton(candidates[0]);
    if (!btn || btn.dataset.hotkeyHinted) return;

    var isMac = /Mac|iPod|iPhone|iPad/.test(navigator.platform || "");
    var label = isMac ? "⌘Enter" : "Ctrl+Enter";

    btn.dataset.hotkeyHinted = "1";
    var hint = document.createElement("span");
    hint.className = "hotkey-hint";
    hint.textContent = label;
    btn.insertAdjacentElement("afterend", hint);
  });
})();
