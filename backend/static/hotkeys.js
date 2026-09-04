/* Ctrl+S (Cmd+S на Mac) — дублирует кнопку сохранения на любой странице
   с формой (координатор, 04.09.2026). Правило: если фокус внутри формы —
   срабатывает её кнопка; если фокус не в форме и на странице она одна —
   срабатывает эта единственная форма. Формы фильтров (method=get, без
   кнопки сохранения) не считаются — так фильтр-селекты с onchange не
   перехватывают хоткей случайно.

   Не все "Сохранить"-кнопки — нативный <button type=submit> формы: часть
   страниц (например, «Ввод по разделам ИД») шлёт данные через fetch по
   клику на <button type=button>. Для них кнопка помечается атрибутом
   data-hotkey-save в разметке страницы — скрипт ищет его в первую
   очередь, до type=submit.

   Видимость (offsetParent !== null) — защита от срабатывания на кнопке
   внутри скрытого/ещё не открытого модального окна. */
(function () {
  function isVisible(el) {
    return !!(el && el.offsetParent !== null && !el.disabled);
  }

  function isSaveForm(form) {
    return form && form.tagName === "FORM" && (form.method || "get").toLowerCase() === "post";
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
    if (key !== "s" || !(e.ctrlKey || e.metaKey) || e.shiftKey || e.altKey) return;

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
})();
