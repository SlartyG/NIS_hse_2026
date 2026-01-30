from __future__ import annotations

import re
import time
from datetime import date
from typing import Dict, List, Optional, Tuple

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError


# ===================== НАСТРОЙКИ =====================
URL = "https://скдф.рф/traffic-accidents/"

START_DATE = date(2024, 11, 1)
END_DATE = date(2024, 11, 30)

MAX_PAGES = 5
PAGE_SIZE = "160"

HEADLESS = False
NAV_TIMEOUT_MS = 60_000
ACTION_TIMEOUT_MS = 30_000
WAIT_TABLE_CHANGE_MS = 30_000

OUT_CSV = "skdf_traffic_accidents_2024-11.csv"
OUT_EXCEL = "skdf_traffic_accidents_2024-11.xlsx"

# ===================== СЕЛЕКТОРЫ =====================
SEL_TABLE = "table.table.skdf"
SEL_FIELDS_BTN = f"{SEL_TABLE} thead tr:first-child th:first-child button"

SEL_LIMIT_BTN = "#limitTable"
SEL_LIMIT_MENU_ITEM = ".dropdown-menu a"

SEL_NEXT_PAGE_BTN = "ul.pagination li.page-item:last-child button.page-link"

RE_FILTERS_BTN = re.compile(r"Фильтры", re.I)
RE_SHOW_BTN = re.compile(r"Показать", re.I)

SEL_PERIOD_ACCORDION_BTN = ".accordion-button:has-text('Период')"
SEL_PERIOD_INPUT = "#ReactDatePicker"

SEL_DATEPICKER_DIALOG = ".react-datepicker[role='dialog'][aria-label='Choose Date']"
SEL_CAL_PREV_BTN = f"{SEL_DATEPICKER_DIALOG} .react-datepicker__header button.btn-icon.btn-skdf-function:first-child"
SEL_CAL_NEXT_BTN = f"{SEL_DATEPICKER_DIALOG} .react-datepicker__header button.btn-icon.btn-skdf-function:last-child"

# Плейсхолдеры месяца/года (их 2) — строгий режим ломался из-за wait_for() на "двух элементах".
# Исправление: ждать ДИАЛОГ, а плейсхолдеры читать без wait_for() на коллекции.
SEL_CAL_PLACEHOLDERS = f"{SEL_DATEPICKER_DIALOG} .downshift__input__field__value_placeholder"


MONTHS_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель", 5: "Май", 6: "Июнь",
    7: "Июль", 8: "Август", 9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}


def _txt(s: Optional[str]) -> str:
    return (s or "").replace("\xa0", " ").strip()


def _month_name_ru(m: int) -> str:
    return MONTHS_RU[m]


# ===================== UI helpers =====================
def open_filters(page) -> None:
    page.get_by_role("button", name=RE_FILTERS_BTN).click()


def click_select_all_fields(page) -> None:
    page.locator(SEL_FIELDS_BTN).click()
    checkbox = page.locator("input[type='checkbox']:visible").first
    checkbox.wait_for(state="visible")
    try:
        if not checkbox.is_checked():
            checkbox.click(force=True)
    except Exception:
        checkbox.click(force=True)


def set_page_size(page, page_size: str) -> None:
    page.locator(SEL_LIMIT_BTN).click()
    page.locator(SEL_LIMIT_MENU_ITEM, has_text=page_size).click()
    page.wait_for_load_state("networkidle")


# ===================== Calendar helpers =====================
def _calendar_get_month_year(page) -> Tuple[str, int]:
    """
    Исправление strict mode violation:
    - НЕ вызываем wait_for() на locator, который матчится на 2 элемента.
    - Ждём сам диалог календаря, затем ждём ПЕРВЫЙ плейсхолдер (month),
      а второй читаем как nth(1).
    """
    page.locator(SEL_DATEPICKER_DIALOG).wait_for(state="visible")

    placeholders = page.locator(SEL_CAL_PLACEHOLDERS)
    placeholders.first.wait_for(state="visible")  # ждём только 1 элемент => strict ok

    # На всякий: если второй ещё не успел дорендериться — подождём коротко
    deadline = time.time() + 5
    while time.time() < deadline and placeholders.count() < 2:
        page.wait_for_timeout(100)

    month = _txt(placeholders.nth(0).inner_text())
    year_txt = _txt(placeholders.nth(1).inner_text()) if placeholders.count() >= 2 else ""

    # year_txt обычно "2026"
    year_digits = re.sub(r"\D+", "", year_txt)
    if not year_digits:
        raise RuntimeError(f"Не смогли прочитать год из календаря: '{year_txt}'")

    return month, int(year_digits)


def _navigate_calendar_to(page, target: date) -> None:
    target_month = _month_name_ru(target.month)
    target_year = target.year

    for _ in range(0, 60):  # запас
        cur_month, cur_year = _calendar_get_month_year(page)

        # текущий месяц -> номер
        cur_month_num = None
        for k, v in MONTHS_RU.items():
            if v == cur_month:
                cur_month_num = k
                break
        if cur_month_num is None:
            # если вдруг локаль/текст другой — попробуем просто идти назад
            page.locator(SEL_CAL_PREV_BTN).click()
            continue

        if (cur_year, cur_month_num) == (target_year, target.month):
            return

        if (cur_year, cur_month_num) < (target_year, target.month):
            page.locator(SEL_CAL_NEXT_BTN).click()
        else:
            page.locator(SEL_CAL_PREV_BTN).click()

    raise RuntimeError("Не удалось перемотать календарь до нужного месяца/года.")


def _pick_day_in_current_month(page, day: int) -> None:
    dd = f"{day:03d}"
    sel = (
        f"{SEL_DATEPICKER_DIALOG} "
        f".react-datepicker__day.react-datepicker__day--{dd}:not(.react-datepicker__day--outside-month)"
    )
    cell = page.locator(sel).first
    cell.wait_for(state="visible")
    cell.click()


def set_period_range_via_calendar(page, start: date, end: date) -> None:
    open_filters(page)

    # раскрыть аккордеон "Период"
    page.locator(SEL_PERIOD_ACCORDION_BTN).first.wait_for(state="visible")
    page.locator(SEL_PERIOD_ACCORDION_BTN).first.click()

    # открыть календарь кликом по input
    page.locator(SEL_PERIOD_INPUT).wait_for(state="visible")
    page.locator(SEL_PERIOD_INPUT).click()

    # дождаться календаря
    page.locator(SEL_DATEPICKER_DIALOG).wait_for(state="visible")

    # выбрать старт
    _navigate_calendar_to(page, start)
    _pick_day_in_current_month(page, start.day)

    # выбрать конец
    _navigate_calendar_to(page, end)
    _pick_day_in_current_month(page, end.day)

    # нажать "Показать ..."
    try:
        page.get_by_role("button", name=RE_SHOW_BTN).click()
    except PWTimeoutError:
        page.locator("text=/Показать/i").first.click()

    page.wait_for_load_state("networkidle")


# ===================== Table / paging =====================
def parse_table(page) -> List[Dict[str, str]]:
    table = page.locator(SEL_TABLE)
    table.wait_for(state="visible")

    thead_rows = table.locator("thead tr")
    row1 = thead_rows.nth(0).locator("th")
    row2 = thead_rows.nth(1).locator("th") if thead_rows.count() > 1 else None

    headers_row1 = [_txt(row1.nth(i).inner_text()) for i in range(row1.count())]
    if headers_row1:
        headers_row1[0] = ""  # служебный

    sub = []
    if row2 is not None and row2.count() >= 2:
        sub = [_txt(row2.nth(i).inner_text()) for i in range(row2.count())]

    final_headers: List[str] = []
    i = 0
    while i < len(headers_row1):
        h = headers_row1[i]
        if not h:
            final_headers.append("_service")
            i += 1
            continue
        if "Количество пострадавших" in h and sub:
            final_headers.extend(["Ранено", "Погибло"])
            i += 1
            continue
        final_headers.append(h)
        i += 1

    rows = table.locator("tbody tr[data-index]")
    out: List[Dict[str, str]] = []

    for r in range(rows.count()):
        tr = rows.nth(r)
        tds = tr.locator("td")
        values = [_txt(tds.nth(i).inner_text()) for i in range(tds.count())]

        if len(values) < len(final_headers):
            values += [""] * (len(final_headers) - len(values))
        elif len(values) > len(final_headers):
            extras = [f"extra_{k}" for k in range(1, len(values) - len(final_headers) + 1)]
            headers = final_headers + extras
            out.append(dict(zip(headers, values)))
            continue

        out.append(dict(zip(final_headers, values)))

    return out


def first_row_signature(page) -> str:
    table = page.locator(SEL_TABLE)
    first = table.locator("tbody tr[data-index]").first
    if first.count() == 0:
        return ""
    return _txt(first.inner_text())


def wait_table_changed(page, prev_sig: str, timeout_ms: int) -> bool:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        page.wait_for_timeout(200)
        cur = first_row_signature(page)
        if cur and cur != prev_sig:
            return True
    return False


def next_page(page) -> bool:
    btn = page.locator(SEL_NEXT_PAGE_BTN)
    if btn.count() == 0:
        return False
    try:
        if not btn.is_enabled():
            return False
    except Exception:
        pass

    prev = first_row_signature(page)
    btn.click()

    changed = wait_table_changed(page, prev, WAIT_TABLE_CHANGE_MS)
    page.wait_for_load_state("networkidle")
    return changed


# ===================== main =====================
def main() -> None:
    all_rows: List[Dict[str, str]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="ru-RU",
        )
        page = context.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        page.set_default_timeout(ACTION_TIMEOUT_MS)

        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")

        # 1) Выбор всех полей
        click_select_all_fields(page)

        # 2) Период через календарь (исправлено)
        set_period_range_via_calendar(page, START_DATE, END_DATE)

        # 3) Показывать по -> 160
        set_page_size(page, PAGE_SIZE)

        # 4) Сбор страниц
        for pg in range(1, MAX_PAGES + 1):
            rows = parse_table(page)
            for r in rows:
                r["_page"] = str(pg)
            all_rows.extend(rows)

            print(f"[OK] page {pg}: +{len(rows)} rows (total={len(all_rows)})")

            if pg == MAX_PAGES:
                break

            if not next_page(page):
                print("[INFO] Next page not available / table not changed. Stop.")
                break

        browser.close()

    df = pd.DataFrame(all_rows)
    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    df.to_excel(OUT_EXCEL, index=False)
    print(f"[DONE] saved: {OUT_CSV} | rows={len(df)} | cols={len(df.columns)}")


if __name__ == "__main__":
    main()
