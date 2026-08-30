/* Единое форматирование дат для отображения (ДД.ММ.ГГГГ), используется
   везде, где дата приходит в JS как ISO-строка из /api/* и раньше
   печаталась как есть. Хранение остаётся ISO — конвертация только на
   отображении. */
window.TM35_RU_DATE = (function () {
  function fmtDMY(iso) {
    if (!iso) return "—";
    var m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (!m) return String(iso);
    return m[3] + "." + m[2] + "." + m[1];
  }
  function fmtDMYHM(iso) {
    if (!iso) return "—";
    var datePart = fmtDMY(iso);
    var m = String(iso).match(/T(\d{2}):(\d{2})/);
    return m ? datePart + " " + m[1] + ":" + m[2] : datePart;
  }
  return { fmtDMY: fmtDMY, fmtDMYHM: fmtDMYHM };
})();
