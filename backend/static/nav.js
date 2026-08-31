/* Выпадающие меню верхней навигации (СМР/ИД). Открытие только по клику —
   не по наведению: наведение не работает на планшете, а с планшета
   заходит ПТО. Клавиатура: Tab доходит до кнопки раздела, Enter/Space
   (стандартное поведение <button>) и стрелка вниз открывают меню и
   переводят фокус на первый пункт, стрелки вверх/вниз внутри меню
   двигают фокус по пунктам, Esc закрывает и возвращает фокус на кнопку
   раздела, клик вне любого меню закрывает все. */
(function () {
  var dropdowns = Array.prototype.slice.call(document.querySelectorAll('.nav-dropdown'));

  function setOpen(dd, open) {
    var btn = dd.querySelector('.nav-dropdown-trigger');
    var menu = dd.querySelector('.nav-dropdown-menu');
    dd.classList.toggle('open', open);
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    menu.hidden = !open;
  }

  function closeAll(exceptDd) {
    dropdowns.forEach(function (dd) {
      if (dd !== exceptDd) setOpen(dd, false);
    });
  }

  dropdowns.forEach(function (dd) {
    var btn = dd.querySelector('.nav-dropdown-trigger');
    var menu = dd.querySelector('.nav-dropdown-menu');
    var items = Array.prototype.slice.call(menu.querySelectorAll('a'));

    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var willOpen = !dd.classList.contains('open');
      closeAll(dd);
      setOpen(dd, willOpen);
      if (willOpen && items.length) items[0].focus();
    });

    btn.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        closeAll(dd);
        setOpen(dd, true);
        if (items.length) items[0].focus();
      } else if (e.key === 'Escape') {
        setOpen(dd, false);
      }
    });

    menu.addEventListener('keydown', function (e) {
      var idx = items.indexOf(document.activeElement);
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        items[(idx + 1) % items.length].focus();
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        items[(idx - 1 + items.length) % items.length].focus();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        setOpen(dd, false);
        btn.focus();
      } else if (e.key === 'Tab') {
        setOpen(dd, false);
      }
    });
  });

  document.addEventListener('click', function (e) {
    dropdowns.forEach(function (dd) {
      if (!dd.contains(e.target)) setOpen(dd, false);
    });
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeAll();
  });
})();
