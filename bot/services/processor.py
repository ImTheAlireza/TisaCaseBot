"""
processor.py — هسته‌ی پردازش فایل سفارش برای ربات تلگرام

ورودی: فایل .xlsx / .csv / .pdf  (جدول سفارش‌های تیساکیس / تیسا چاپ)
       ستون‌های جدول: ردیف | بارکد | تاریخ ثبت | نام گیرنده | کد سفارش | مقصد | نام فروشگاه | آدرس | وزن
       (فایل‌هایی با هدر انگلیسی order_id / tracking_code هم پشتیبانی می‌شوند)

خروجی: یک ``Report`` (دیتاکلس) با:
  - csv_text      : فایل tracking.csv با ستون‌های order_id,tracking_code
                    (فقط ردیف‌هایی که بارکدشان معتبر است)
  - summary       : خلاصه‌ی گزارش برای نمایش در چت
  - problems_csv  : گزارش کامل مشکلات، با لینک سطرِ مبدأ (یا None اگر مشکلی نبود)
  - review_csv/review_xlsx : ردیف‌هایی که باید دیده شوند (خطا و هشدار); نسخهٔ xlsx طوری است که ستون بارکد
                    در اکسل «متن» می‌ماند و عدد ۲۴ رقمی خراب نمی‌شود — و چون هدرهایش
                    همان «بارکد / کد سفارش / ردیف» است، خودِ ربات هم می‌تواند فایلِ
                    اصلاح‌شده را بخواند (رفت‌وبرگشت، بدون مسیرِ دوم).
  - questions     : اگر دو ستون محتمل بود، سؤالِ «کدام ستون؟» — حدس زدن ممنوع.

هیچ‌چیز اینجا به تلگرام وابسته نیست؛ تست‌هایش هم بدون ربات اجرا می‌شوند.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field, fields
from typing import Any

import pandas as pd

from bot.config import settings
from bot.services import barcodes

# ---------------------------------------------------------------------------
# نرمال‌سازی متن (ارقام فارسی/عربی → انگلیسی، حذف فاصله/نیم‌فاصله/کاما)
# ---------------------------------------------------------------------------
_FA2EN = {
    "۰": "0",
    "۱": "1",
    "۲": "2",
    "۳": "3",
    "۴": "4",
    "۵": "5",
    "۶": "6",
    "۷": "7",
    "۸": "8",
    "۹": "9",
    "٠": "0",
    "١": "1",
    "٢": "2",
    "٣": "3",
    "٤": "4",
    "٥": "5",
    "٦": "6",
    "٧": "7",
    "٨": "8",
    "٩": "9",
    "ي": "ی",
    "ك": "ک",
    "ة": "ه",
    "ۀ": "ه",
}
# str.translate نیاز به جدول ordinal دارد — دیکشنری str→str بی‌اثر است
_FA2EN_TABLE = str.maketrans(_FA2EN)


def _norm(s) -> str:
    """نرمال‌سازی برای مقایسه‌ی نام ستون‌ها (هدرها)"""
    if s is None:
        return ""
    if isinstance(s, float) and s.is_integer():
        s = int(s)
    s = str(s).translate(_FA2EN_TABLE)
    s = re.sub(r"[\s\u00a0\u200c\u200f\u202a\u202b,]", "", s)
    return s.lower()


def _clean(v) -> str:
    """نرمال‌سازی مقدار یک سلول (بدون lower کردن — برای داده)"""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, float) and pd.isna(v):
        return ""
    s = str(v)
    s = re.sub(r"[\s\u00a0\u200c\u200f\u202a\u202b,]", "", s)
    return s.translate(_FA2EN_TABLE)


# ---------------------------------------------------------------------------
# نام ستون‌ها (بعد از نرمال‌سازی — فاصله‌ها حذف شده‌اند)
# ---------------------------------------------------------------------------
#: What a column has to be called to be *the* barcode column. «کد رهگیری» is in here
#: because that is what the other shop's exports call the same 24-digit thing — and a
#: sheet that has both columns is exactly the case where the bot must ask, not pick.
_BARCODE_HEADERS = {
    "بارکد",
    "باركد",
    "barcode",
    "trackingcode",
    "tracking_code",
    "tracking",
    "کدرهگیری",
}
_CODE_HEADERS = {"کدسفارش", "کدسفارشگیرنده", "orderid", "order_id", "order", "کد"}
_ROW_HEADERS = {"ردیف", "رديف", "ردی", "شماره", "no"}
# ستون نام گیرنده — وقتی ستون «کد سفارش» جدا وجود ندارد، کد داخل همین ستون است
# (مثل «امیرحسین عاشوری ۳۰۶۱۷۶»)
_NAME_HEADERS = {"نامگ", "نامگیرنده", "گیرنده", "نامونامخانوادگیگیرنده", "recipient"}

FIELD_BARCODE = "barcode"
FIELD_CODE = "code"

# Which attribute of ``Layout`` a field lives in: only this module has to know it, so the
# flow can speak in fields and never name a column attribute of its own.
_LAYOUT_ATTR = {FIELD_BARCODE: "barcode_col", FIELD_CODE: "code_col"}

_RE_DATE = re.compile(r"14\d{2}/\d{2}/\d{2}")
# کد ۵-۶ رقمی داخل متن (مثلاً چسبیده به نام گیرنده)
_RE_CODE_IN_TEXT = re.compile(r"(?<![0-9])([0-9]{5,6})(?![0-9])")

#: How many columns a question may offer — a Telegram keyboard with 30 buttons is
#: not a question, it is a chore.
MAX_OPTIONS = 8


class RowLimitError(ValueError):
    """More rows than ``MAX_ROWS`` — refuse instead of grinding the bot to dust."""


# ---------------------------------------------------------------------------
# What the file looks like (schema detection)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Column:
    """One candidate column, as the user has to recognise it in a button."""

    index: int
    header: str
    sample: str

    def label(self) -> str:
        """``C · «بارکد» · 610001573845…`` — short enough for a button."""
        head = self.header or "(بدون هدر)"
        if len(head) > 18:
            head = head[:17] + "…"
        sample = self.sample[:10] + ("…" if len(self.sample) > 10 else "")
        tail = f" · {sample}" if sample else ""
        return f"{_col_letter(self.index)} · «{head}»{tail}"


@dataclass(frozen=True)
class Question:
    """«کدام ستون؟» — asked instead of guessing when two headers are plausible."""

    field: str  # FIELD_BARCODE | FIELD_CODE
    prompt: str
    options: tuple[Column, ...]
    allow_skip: bool = False

    def answers(self, index: int) -> dict[str, Any]:
        return {self.field: index}


@dataclass(frozen=True)
class Layout:
    """The columns this file will be read through, and how each was decided."""

    header_row: int = -1  # 0-based row index of the header, -1 = none
    barcode_col: int | None = None
    code_col: int | None = None
    row_col: int | None = None
    name_col: int | None = None
    sheet: str = ""
    #: «هدر» when the header said so, «انتخاب تو» when the user picked it
    decided: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def with_choice(self, field: str, index: int) -> Layout:
        """This layout with one column replaced by what the user picked.

        ``-1`` means «بدون این ستون» — the field is switched off, not moved. The label is
        recorded with it, so a summary can still tell a header match from a human choice.
        """
        attrs = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "decided"}
        attrs[_LAYOUT_ATTR[field]] = None if index < 0 else index
        labels = dict(self.decided)
        labels[field] = "انتخاب تو"
        return Layout(**attrs, decided=tuple(sorted(labels.items())))

    def as_dict(self) -> dict[str, Any]:
        return {
            "header_row": self.header_row,
            "barcode_col": self.barcode_col,
            "code_col": self.code_col,
            "row_col": self.row_col,
            "name_col": self.name_col,
            "sheet": self.sheet,
            "decided": list(self.decided),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Layout:
        return cls(
            header_row=int(data.get("header_row", -1)),
            barcode_col=_as_int(data.get("barcode_col")),
            code_col=_as_int(data.get("code_col")),
            row_col=_as_int(data.get("row_col")),
            name_col=_as_int(data.get("name_col")),
            sheet=str(data.get("sheet") or ""),
            decided=tuple(tuple(x) for x in (data.get("decided") or ())),
        )

    def schema_line(self) -> str:
        """Tell the user which columns were used — never read a file silently.

        A column the owner switched off is reported too: «کد سفارش ← ندارد (انتخاب تو)»
        is the difference between «the bot forgot my code column» and «I told it not to
        read one».
        """
        decided = dict(self.decided)
        parts = []
        for name, label in (("barcode", "بارکد"), ("code", "کد سفارش"), ("row", "ردیف")):
            how = decided.get(name)
            if how is None:
                continue
            index = getattr(self, f"{name}_col" if name != "row" else "row_col")
            parts.append(
                f"{label} ← {_col_letter(index)} ({how})" if index is not None else f"{label} ← ندارد ({how})"
            )
        return " · ".join(parts)


def _as_int(value: object) -> int | None:
    try:
        return None if value is None else int(str(value))
    except (TypeError, ValueError):
        return None


def _col_letter(index: int) -> str:
    """0-based column index → Excel letter (0→A, 25→Z, 26→AA)."""
    letters = ""
    n = int(index) + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters or "A"


# ---------------------------------------------------------------------------
# خواندن فایل
# ---------------------------------------------------------------------------
def _read_excel_or_csv(path, ext):
    if ext == ".xlsx":
        import zipfile
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > 2000 or sum(item.file_size for item in members) > 128 * 1024 * 1024:
                raise RowLimitError("حجم بازشده/تعداد فایل‌های XLSX از سقف ایمن بیشتر است")
        return pd.read_excel(path, header=None, dtype=object, nrows=settings.max_rows + 1)
    last_err = None
    for enc in ("utf-8-sig", "utf-8", "cp1256"):
        try:
            return pd.read_csv(path, header=None, dtype=object, keep_default_na=False, encoding=enc, nrows=settings.max_rows + 1)
        except (UnicodeDecodeError, pd.errors.ParserError) as e:
            last_err = e
    raise ValueError(f"فایل CSV قابل خواندن نیست: {last_err}")


def _sheet_name(path: str) -> str:
    """The first sheet's name, for the row links (never fatal)."""
    try:
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True)
        try:
            return str(wb.sheetnames[0])
        finally:
            wb.close()
    except Exception:  # pragma: no cover
        return "Sheet1"


def _shape_score(df: pd.DataFrame, col: int, header_row: int) -> tuple[int, str]:
    """How much this column looks like barcodes, and its first non-empty value.

    Only used to *rank the options of a question* — never to pick a column by
    itself. A 24-digit number is the fingerprint of a Tisa tracking code.
    """
    long_digits = 0
    any_digits = 0
    sample = ""
    for r in range(header_row + 1, min(len(df), header_row + 60)):
        v = _clean(_cell(df, r, col))
        if not v:
            continue
        if not sample:
            sample = v
        if v.isdigit():
            any_digits += 1
            if len(v) >= 12:
                long_digits += 1
    return (long_digits * 2 + any_digits), sample


def _headers_of(df: pd.DataFrame, row: int) -> list[str]:
    """The header cells of ``row``; empty when the file has no header row."""
    if row < 0 or row >= len(df):
        return []
    return [str(c).strip() for c in df.iloc[row].tolist()]


def scan_table(
    df: pd.DataFrame,
    answers: dict[str, int | None] | None = None,
    *,
    sheet: str = "",
    prior_labels: dict[str, str] | None = None,
) -> tuple[Layout, tuple[Question, ...]]:
    """Find the header row and the columns, or ask which one is meant.

    ``answers`` carries what the user already picked (``-1`` means «این ستون نیست»
    / «بدون این ستون»); a picked column is never overruled by a header, and an
    ambiguous one is never guessed. ``prior_labels`` says how each inherited column was
    decided the first time, so re-reading a file after an answer does not relabel the
    columns nobody touched.
    """
    inherited = dict(prior_labels or {})
    answers = {k: v for k, v in (answers or {}).items() if v is not None}

    header_rows = []
    for i in range(min(20, len(df))):
        cells = [_norm(c) for c in df.iloc[i].tolist()]
        if any(c in _BARCODE_HEADERS for c in cells) or any(c in _CODE_HEADERS for c in cells):
            header_rows.append(i)
    # -1 when the file has no header at all: then row 1 is data, not a label.
    picked_row = answers.get("header_row")
    header_row = int(picked_row) if picked_row is not None else (header_rows[0] if header_rows else -1)
    raw_headers = _headers_of(df, header_row)
    normed = [_norm(c) for c in raw_headers]

    def candidates(pool: set[str]) -> list[Column]:
        found = [j for j, c in enumerate(normed) if c in pool]
        return [Column(j, raw_headers[j], _clean(_cell(df, header_row + 1, j))) for j in found]

    shapes = {j: _shape_score(df, j, header_row) for j in range(max(len(raw_headers), df.shape[1]))}
    decided: dict[str, str] = {}
    questions: list[Question] = []

    barcode = answers.get(FIELD_BARCODE)
    if barcode is not None:
        decided[FIELD_BARCODE] = inherited.get(FIELD_BARCODE, "انتخاب تو")
    else:
        pool = candidates(_BARCODE_HEADERS)
        if len(pool) == 1:
            barcode = pool[0].index
            decided[FIELD_BARCODE] = "هدر"
        else:
            # Ask about what is plausible, not about every column in the sheet: the
            # two barcode-named ones, or — when nothing is named — the columns whose
            # values look like barcodes.
            options = pool or [
                Column(j, raw_headers[j] if j < len(raw_headers) else "", shapes[j][1])
                for j in sorted(shapes, key=lambda j: -shapes[j][0])[:MAX_OPTIONS]
                if shapes[j][1] or j < len(raw_headers)
            ]
            if not options:
                raise ValueError(
                    "ستون «بارکد» در فایل پیدا نشد. فایل باید جدول سفارش‌ها "
                    "(ردیف / بارکد / تاریخ ثبت / نام گیرنده / کد سفارش / …) باشد."
                )
            why = "دو ستون با نام بارکد وجود دارد" if len(pool) > 1 else "هیچ ستونی «بارکد» نام ندارد"
            questions.append(
                Question(
                    FIELD_BARCODE,
                    f"{why} — کدام ستون بارکد/کد رهگیری است؟",
                    tuple(options[:MAX_OPTIONS]),
                )
            )

    code = answers.get(FIELD_CODE)
    if code is not None:
        decided[FIELD_CODE] = inherited.get(FIELD_CODE, "انتخاب تو")
    else:
        pool = candidates(_CODE_HEADERS)
        if len(pool) == 1:
            code = pool[0].index
            decided[FIELD_CODE] = "هدر"
        elif len(pool) > 1:
            questions.append(
                Question(
                    FIELD_CODE,
                    "چند ستون شبیه «کد سفارش» است — کدام را بردارم؟ "
                    "اگر هیچ‌کدام نبود، «بدون کد سفارش» را بزن.",
                    tuple(pool),
                    allow_skip=True,
                )
            )

    rows_pool = candidates(_ROW_HEADERS)
    name_pool = candidates(_NAME_HEADERS)
    layout = Layout(
        header_row=header_row,
        barcode_col=None if barcode is not None and barcode < 0 else _as_int(barcode),
        code_col=None if code is not None and code < 0 else _as_int(code),
        row_col=rows_pool[0].index if rows_pool else None,
        name_col=name_pool[0].index if name_pool else None,
        sheet=sheet,
        decided=tuple(sorted(decided.items())),
    )
    return layout, tuple(questions)


def _cell(df, row_index, col):
    """یک سلول با احتساب ستونِ ناموجود و مقدار NaN."""
    if col is None:
        return ""
    if col >= df.shape[1] or row_index >= len(df):
        return ""
    v = df.iat[row_index, col]
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return v


def _collect_rows(
    df, header_idx, bc_col, code_col, row_col, name_col, *, name_for_code: bool = True
) -> list[dict[str, Any]]:
    """سطرهای داده.

    سلول‌ها با ``_cell`` خوانده می‌شوند (نه یک closure داخل حلقه): نسخهٔ قبلی
    متغیر حلقه را دیر-بند می‌کرد که با هر تغییر کوچک، مقدار سطرِ اشتباه می‌دهد.

    ``name_for_code`` بستنِ همان راهِ جایگزین است: وقتی کاربر صریحاً گفته «بدون کد
    سفارش»، برداشتنِ کد از نام گیرنده یعنی کاری که او رد کرده — و خلاصه هم خلافش
    را می‌گوید.
    """
    rows: list[dict[str, Any]] = []
    for r in range(header_idx + 1, len(df)):
        get = lambda col, _r=r: _cell(df, _r, col)  # noqa: E731

        b_raw, c_raw, rn_raw = get(bc_col), get(code_col), get(row_col)
        b, c = _clean(b_raw), _clean(c_raw)

        # ستون «کد سفارش» جدا نبود یا خالی بود → کد را از داخل نام گیرنده بردار
        # (مثل «امیرحسین عاشوری ۳۰۶۱۷۶»)
        code_from_name = False
        if not c and name_for_code and name_col is not None:
            m = _RE_CODE_IN_TEXT.search(str(get(name_col) or "").translate(_FA2EN_TABLE))
            if m:
                c = m.group(1)
                code_from_name = True

        rn = _clean(rn_raw) or str(len(rows) + 1)
        has_digits_b = any(ch.isdigit() for ch in b)
        has_digits_c = any(ch.isdigit() for ch in c)
        if not has_digits_b and not has_digits_c:
            continue  # سطر خالی یا سطر «جمع کل» — رد می‌شود
        numeric_b = isinstance(b_raw, (int, float)) and not isinstance(b_raw, bool)
        rows.append(
            {
                "rownum": rn,
                "barcode": b,
                "code": c,
                "barcode_numeric": numeric_b,
                "barcode_raw": b_raw,
                "code_from_name": code_from_name,
                "sheet_row": r + 1,
            }
        )
    return rows


def _read_pdf(path) -> list[dict[str, Any]]:
    """استخراج جدول از PDF خروجی سامانه (چیدمان ثابت ستون‌ها)"""
    import pymupdf

    doc = pymupdf.open(path)
    if len(doc) > 500:
        doc.close()
        raise RowLimitError("بیش از ۵۰۰ صفحه در PDF؛ فایل را تقسیم کن")
    rows: list[dict[str, Any]] = []
    for pno in range(len(doc)):
        page = doc[pno]
        d = page.get_text("rawdict")
        spans_by_y: dict[float, list[tuple[float, float, str]]] = {}
        for block in d.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    x0, y0, x1 = span["bbox"][0], span["bbox"][1], span["bbox"][2]
                    text = "".join(ch["c"] for ch in span.get("chars", [])).strip().translate(_FA2EN_TABLE)
                    if not text:
                        continue
                    key = round(y0, 1)
                    spans_by_y.setdefault(key, []).append((x0, x1, text))
        for _y, spans in spans_by_y.items():
            # یک سطر می‌تواند هم سلول متنی و هم فهرست سلول‌های میانی داشته باشد
            _guard_rows(len(rows))
            rec: dict[str, Any] = {}
            for x0, x1, t in spans:
                if x0 > 535 and t.isdigit() and len(t) <= 4:
                    rec["n"] = t
                elif 420 <= x0 <= 535 and len(t) >= 12 and t.isdigit():
                    rec["bc"] = t
                elif 235 <= x0 <= 420 and x1 <= 425 and not t.isdigit():
                    rec.setdefault("mid", []).append(t)
            if "bc" in rec and "n" in rec:
                mid = "".join(rec.get("mid") or [])
                mid_clean = _RE_DATE.sub("", mid)  # حذف تاریخ
                m = _RE_CODE_IN_TEXT.search(mid_clean)  # کد سفارش
                rows.append(
                    {
                        "rownum": rec["n"],
                        "barcode": rec["bc"],
                        "code": m.group(1) if m else "",
                        "barcode_numeric": False,
                        "barcode_raw": rec["bc"],
                        "code_from_name": False,
                        "page": pno + 1,
                        "sheet_row": None,
                    }
                )
    doc.close()
    if not rows:
        raise ValueError("ساختار PDF شناخته نشد (ستون‌های جدول پیدا نشد)")
    return rows


# ---------------------------------------------------------------------------
# بررسی مشکلات
# ---------------------------------------------------------------------------
def _where(row: dict[str, Any], layout: Layout | None = None, col: int | None = None) -> str:
    """کجا این سطر را پیدا کنیم — لینک، نه حدس.

    The cell named is the one that is wrong (the barcode column, or the order-code
    column), so «برو به آن سلول» in Excel lands on the problem itself.
    """
    sheet = layout.sheet if layout is not None else ""
    sheet_row = row.get("sheet_row")
    if sheet and sheet_row:
        if col is None and layout is not None:
            col = layout.barcode_col
        return f"{sheet}!{_col_letter(col or 0)}{sheet_row}"
    if sheet_row:
        # A CSV has lines, not cells: saying «Sheet1!B2» would be a made-up address.
        return f"سطر {sheet_row} فایل"
    if row.get("page"):
        return f"صفحهٔ {row['page']}"
    return f"ردیف {row.get('rownum', '?')}"


def _barcode_state(row: dict[str, Any]) -> tuple[str, str]:
    """وضعیت بارکد یک سطر: ('ok'|'warn'|'error', شرح)

    هر سه حالت در گزارش ثبت می‌شود، اما فقط `ok` و `warn` وارد tracking.csv می‌شوند:
    اکسل اعداد ۲۴ رقمی را به float تبدیل و خراب می‌کند و آن عدد خراب در فایل
    خروجی کاملاً معقول به‌نظر می‌رسد — پس نوشتنش خطرناک‌تر از خالی‌گذاشتنش است.
    """
    b = row["barcode"]
    if not b:
        return "error", "بارکد خالی"
    raw = row.get("barcode_raw")
    if _destroyed(row):
        return "error", ("بارکد به‌صورت عدد ذخیره شده و دقتش از بین رفته — از فایل متنی/PDF اصلی استفاده کن")
    state, note = barcodes.classify(b)
    if state != "ok":
        return state, note
    if barcodes.number_is_suspicious(raw, b):
        return "warn", "بارکد در اکسل عددی است؛ اگر ۱۵ رقم را رد کند دقتش از بین می‌رود"
    return "ok", ""


def _order_code_state(row):
    c = row["code"]
    if not c:
        return "warn", "کد سفارش خالی (باید خودت تکمیل کنی)"
    if barcodes.order_code_is_valid(c):
        return "ok", ""
    if barcodes.order_code_is_short(c):
        return "warn", "کد سفارش ۵ رقمی (احتمالاً یک رقم جا افتاده)"
    return "error", f"کد سفارش نامعتبر: «{c}» (باید ۶ رقم باشد)"


def _repeat_label(rows: list[dict[str, Any]]) -> str:
    """«۲ و ۵ و ۸» — with a ceiling, because a barcode pasted down a whole column is
    a real export and the label is read by a human, not by a machine."""
    shown = [str(r["rownum"]) for r in rows[:3]]
    if len(rows) > 3:
        shown.append(f"+{len(rows) - 3} سطر دیگر")
    return " و ".join(shown)


def _analyze(rows: list[dict[str, Any]], layout: Layout | None = None) -> list[tuple]:
    """برمی‌گرداند: list of (ردیف نمایشی, شدت ❌/⚠️, شرح, بارکد, کد, لینک مبدأ)

    هر سطر هم برچسب می‌گیرد (`row['barcode_state']`, `row['code_state']`) تا
    `build_csv` بداند کدام داده قابل ارسال به سامانهٔ رهگیری است.
    """
    problems: list[tuple] = []
    #: duplicates keep the whole row, not just its label: the link has to point at
    #: both places the same barcode appears.
    bc_rows: dict[str, list[dict[str, Any]]] = {}
    code_rows: dict[str, list[dict[str, Any]]] = {}

    def cell(row: dict[str, Any], field_col: int | None) -> str:
        return _where(row, layout, field_col)

    for row in rows:
        n = row["rownum"]
        b, c = row["barcode"], row["code"]
        bc_state, bc_message = _barcode_state(row)
        row["barcode_state"] = bc_state
        row["barcode_note"] = bc_message
        if bc_message:
            problems.append(
                (
                    n,
                    "❌" if bc_state == "error" else "⚠️",
                    bc_message,
                    b,
                    c,
                    cell(row, layout.barcode_col if layout else None),
                )
            )
        if bc_state in ("ok", "warn"):
            bc_rows.setdefault(b, []).append(row)

        code_state, code_message = _order_code_state(row)
        row["code_state"] = code_state
        row["code_note"] = code_message
        if code_message:
            problems.append(
                (
                    n,
                    "❌" if code_state == "error" else "⚠️",
                    code_message,
                    b,
                    c,
                    cell(row, layout.code_col if layout else None),
                )
            )
        if code_state == "ok":
            code_rows.setdefault(c, []).append(row)

    # --- تکراری‌ها -----------------------------------------------------------
    # بارکد تکراری هشدار است نه خطا: یک بسته ممکن است دو سفارش داشته باشد و
    # کدش معتبر است. حذف هر دو سطر از CSV سفارش درست را از دست می‌داد.
    for b, found in bc_rows.items():
        if len(found) > 1:
            problems.append(
                (
                    _repeat_label(found),
                    "⚠️",
                    f"بارکد تکراری در {len(found)} سطر (در CSV می‌ماند؛ بررسی کن)",
                    b,
                    "",
                    " · ".join(cell(r, layout.barcode_col if layout else None) for r in found[:3]),
                )
            )
    for c, found in code_rows.items():
        if len(found) > 1:
            problems.append(
                (
                    _repeat_label(found),
                    "⚠️",
                    "کد سفارش تکراری",
                    "",
                    c,
                    " · ".join(cell(r, layout.code_col if layout else None) for r in found[:3]),
                )
            )

    return problems


# ---------------------------------------------------------------------------
# خروجی‌ها
# ---------------------------------------------------------------------------
def build_csv(rows) -> str:
    """فایل tracking.csv — فقط داده‌ای که معتبر است.

    سطرهای دارای خطا (بارکد خراب‌شده/ناصحیح) اینجا نمی‌آیند و به
    `needs-review.csv` می‌روند؛ کد سفارش خالی همچنان با سلول خالی می‌ماند تا
    خود کاربر تکمیلش کند.
    """
    out = ["order_id,tracking_code"]
    for row in rows:
        if row.get("barcode_state") not in ("ok", "warn"):
            continue
        code = row["code"] if row.get("code_state") in ("ok", "warn") else ""
        out.append(f"{code},{row['barcode']}")
    return "\n".join(out) + "\n"


def _safe_cell(value: object) -> str:
    text = str(value)
    return "'" + text if text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")) else text


def _csv_join(cells: list[str]) -> str:
    """CSV فیلدِ ایمن: کاما/دوزاق/خط جدید در متنِ فارقی را نمی‌شکند."""
    out = []
    for cell in cells:
        text = _safe_cell(cell)
        if any(ch in text for ch in [",", '"', "\n", "\r"]):
            text = '"' + text.replace('"', '""') + '"'
        out.append(text)
    return ",".join(out)


def _destroyed(row: dict[str, Any]) -> bool:
    """بارکدی که اکسل عددش کرده و رقم‌هایش را خورده — یک‌جا، نه در سه جا."""
    raw = row.get("barcode_raw")
    return bool(row.get("barcode")) and barcodes.looks_float_destroyed(raw, row["barcode"])


def _fix_barcode(row: dict[str, Any]) -> str:
    """The barcode cell of the *editable* file: blank when the number is destroyed.

    Putting the destroyed digits back is how they get re-imported as if they were
    real, and the point of the fix file is that it is sent straight back to the bot.
    """
    return "" if _destroyed(row) else str(row.get("barcode") or "")


def _reason(row: dict[str, Any]) -> str:
    """Why this row is in the review file — the sentence from the analysis, not a label.

    «بارکد نامعتبر» would send the owner to problems.csv to learn anything; the row
    already knows what was wrong with it.
    """
    parts = []
    if _destroyed(row):
        # The analysis sentence says «use the text/PDF export next time», which is advice
        # about the *next* file; the cell needs what to do with this one.
        parts.append("بارکد در اکسل خراب شده — خودت از فایل اصلی بردار")
    elif row.get("barcode_state") in ("error", "warn"):
        parts.append(str(row.get("barcode_note") or "بارکد نامعتبر"))
    if row.get("code_state") in ("error", "warn"):
        parts.append(str(row.get("code_note") or "کد سفارش نامعتبر"))
    return " + ".join(parts)


def review_rows(rows) -> list[dict[str, Any]]:
    """سطرهایی که باید دیده شوند: خطاها، و آن هشدارهایی که در CSV هم مانده‌اند.

    نام فایل «needs-review» است نه «needs-fix»، چون یک EAN-13 که پذیرفتیم اینجا می‌آید
    *به‌خاطر اینکه* ارزش یک نگاه دوباره را دارد؛ حذفش از فهرست یعنی پنهان‌کردنِ همان
    حالتی که کدِ معتبرْ کدِ رهگیری نیست.
    """
    return [r for r in rows if _reason(r)]


def unwritten_rows(rows) -> list[dict[str, Any]]:
    """سطرهایی که اصلاً در tracking.csv ننوشته شدند (بارکدشان خطادار است)."""
    return [r for r in rows if r.get("barcode_state") == "error"]


def build_review_csv(rows, layout: Layout | None = None) -> str | None:
    """سطرهای نیازمند اصلاح، با دلیل و لینک سطر — برای اینکه گم نشوند."""
    lines = ["row,source_cell,order_id,barcode,reason"]
    any_row = False
    for row in review_rows(rows):
        any_row = True
        lines.append(
            _csv_join(
                [
                    str(row["rownum"]),
                    _where(row, layout),
                    row["code"],
                    row["barcode"],
                    _reason(row),
                ]
            )
        )
    if not any_row:
        return None
    return "\n".join(lines) + "\n"


def build_review_workbook(rows, layout: Layout | None = None) -> bytes | None:
    """همان سطرها در اکسل — با ستون بارکدِ «متن»، تا اصلاح‌کردن خرابشان نکند.

    سرستون‌ها عمداً نام‌های خودِ جدول هستند («ردیف/بارکد/کد سفارش»): کاربر در اکسل
    بارکد را اصلاح می‌کند و **همان فایل** را دوباره برای ربات می‌فرستد؛ مسیرِ
    خواندنِ دیگری لازم نیست.
    """
    items = review_rows(rows)
    if not items:
        return None
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    wb = Workbook()
    ws = wb.active
    ws.title = "needs-review"
    headers = ["ردیف", "بارکد", "کد سفارش", "دلیل", "محل در فایل اصلی"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in items:
        ws.append(
            [
                str(row["rownum"]),
                _fix_barcode(row),
                str(row["code"]),
                _reason(row),
                _where(row, layout),
            ]
        )
    for r in range(2, len(items) + 2):
        for cell in ws[r]:
            if isinstance(cell.value, str):
                cell.data_type = "s"
        for col in (1, 2, 3):
            ws.cell(row=r, column=col).number_format = "@"  # متن، نه عدد
            ws.cell(row=r, column=col).alignment = Alignment(horizontal="left")
    for letter, width in zip("ABCDE", (8, 30, 12, 22, 20), strict=True):
        ws.column_dimensions[letter].width = width
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_problems_csv(problems) -> str | None:
    """`problems.csv`: هر مشکل با لینک سطرِ مبدأ، نه یک فایل متنی بی‌نقشه."""
    if not problems:
        return None
    lines = ["severity,source_row,source_cell,reason,order_id,barcode"]
    for n, sev, desc, b, c, where in problems:
        lines.append(_csv_join([sev, str(n), where, desc, c, b]))
    return "\n".join(lines) + "\n"


def build_summary(rows, problems, fname, layout: Layout | None = None, notes: tuple[str, ...] = ()) -> str:
    n = len(rows)
    usable = sum(1 for r in rows if r.get("barcode_state") in ("ok", "warn"))
    dropped = len(unwritten_rows(rows))
    c_valid = sum(1 for r in rows if barcodes.order_code_is_valid(r["code"]))
    errs = [p for p in problems if p[1] == "❌"]
    warns = [p for p in problems if p[1] == "⚠️"]

    lines = [
        f"📄 فایل: {fname}",
        f"🔢 تعداد ردیف: {n}",
        f"✅ ردیف قابل استفاده در tracking.csv: {usable} از {n}",
        f"📦 کد سفارش معتبر: {c_valid} از {n}",
        f"❌ خطا: {len(errs)}   ⚠️ هشدار: {len(warns)}",
    ]
    schema = layout.schema_line() if layout is not None else ""
    if schema:
        lines += ["", f"🗺️ ستون‌ها: {schema}"]
    if notes:
        lines += ["", *[f"ℹ️ {note}" for note in notes]]
    if dropped:
        lines += [
            "",
            f"🚫 {dropped} ردیف به‌دلیل بارکد نامعتبر در tracking.csv ننوشته شد"
            " (در needs-review.csv با دلیل آمده؛ برای اصلاح در اکسل needs-review.xlsx را باز کن"
            " و بعد از اصلاح، همان فایل را دوباره بفرست).",
        ]
    if problems:
        lines += ["", "مشکلات (نمونه):"]
        for entry in problems[:8]:
            rn, sev, desc = entry[0], entry[1], entry[2]
            lines.append(f"{sev} ردیف {rn}: {desc}")
        if len(problems) > 8:
            lines.append(f"… و {len(problems) - 8} مورد دیگر (در فایل problems.csv)")
        lines += ["", "📎 فایل tracking.csv ضمیمه شد — کدهای خالی را خودت تکمیل کن."]
    else:
        lines += ["", "🎉 هیچ مشکلی پیدا نشد! فایل tracking.csv آماده است."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# نتیجه
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Report:
    """Everything one file produced. ``questions`` non-empty means: ask, don't write."""

    csv_text: str = ""
    summary: str = ""
    problems_csv: str | None = None
    review_csv: str | None = None
    review_xlsx: bytes | None = None
    #: rows the owner must look at (errors *and* warnings)
    needs_review: int = 0
    #: rows that could not be written to tracking.csv at all
    dropped: int = 0
    layout: Layout | None = None
    questions: tuple[Question, ...] = ()
    fname: str = ""
    notes: tuple[str, ...] = ()
    rows: int = 0
    usable: int = 0
    errors: int = 0
    warnings: int = 0

    @property
    def needs_answer(self) -> bool:
        return bool(self.questions)

    def as_dict(self) -> dict[str, Any]:
        """What the ledger may keep — the files themselves are never stored."""
        return {
            "rows": self.rows,
            "usable": self.usable,
            "errors": self.errors,
            "warnings": self.warnings,
            "needs_review": self.needs_review,
            "dropped": self.dropped,
            "file": self.fname,
        }


# ---------------------------------------------------------------------------
# نقطه‌ی ورود اصلی
# ---------------------------------------------------------------------------
def _guard_rows(count: int) -> None:
    limit = int(settings.max_rows)
    if limit > 0 and count > limit:
        raise RowLimitError(
            f"فایل حداقل {count:,} ردیف دارد و سقف {limit:,} ردیف (MAX_ROWS) را رد کرده است. "
            "فایل را به دو یا چند بخش تقسیم کن؛ پردازشِ این حجم، ربات را برای همه slow می‌کند."
        )


def process_file(path, fname=None, *, layout: Layout | dict[str, Any] | None = None) -> Report:
    """Read one order file → ``Report``.

    ``layout`` is what the user already chose (see :func:`scan`); without it the
    header row and the columns are detected, and a file whose headers are
    ambiguous comes back with ``questions`` instead of a guessed answer.
    """
    ext = os.path.splitext(str(path))[1].lower()
    fname = fname or os.path.basename(str(path))
    chosen = layout if isinstance(layout, Layout) else (Layout.from_dict(layout) if layout else None)

    if ext == ".pdf":
        rows = _read_pdf(path)
        _guard_rows(len(rows))
        report_layout = chosen or Layout(header_row=-1)
        questions: tuple[Question, ...] = ()
    elif ext in (".xlsx", ".csv"):
        df = _read_excel_or_csv(path, ext)
        _guard_rows(len(df))
        answers: dict[str, int | None] = {}
        if chosen is not None:
            # A column of ``None`` means two different things: never looked for, or
            # switched off on purpose. ``decided`` is what tells them apart, and only
            # the second one must not be re-detected (re-detecting it asks again).
            answers = {"header_row": chosen.header_row}
            for field, column in ((FIELD_BARCODE, chosen.barcode_col), (FIELD_CODE, chosen.code_col)):
                if column is not None:
                    answers[field] = column
                elif field in dict(chosen.decided):
                    answers[field] = -1
        sheet = _sheet_name(path) if ext == ".xlsx" else ""
        report_layout, questions = scan_table(
            df, answers, sheet=sheet, prior_labels=dict(chosen.decided) if chosen else None
        )
        if questions:
            # Ask before writing anything: a wrong barcode column produces an
            # empty file, a wrong order-code column produces a *wrong* one.
            return Report(questions=questions, layout=report_layout, fname=fname)
        if report_layout.barcode_col is None:
            raise ValueError(
                "ستون «بارکد» در فایل پیدا نشد. فایل باید جدول سفارش‌ها "
                "(ردیف / بارکد / تاریخ ثبت / نام گیرنده / کد سفارش / …) باشد."
            )
        # «کد سفارش» سه حالت دارد: ستونش هست، نیست ولی از نام گیرنده درمی‌آید، یا
        # کاربر خودش گفته ندارد. سومین با دومی فرق دارد و همان را از راهِ نام می‌بندد.
        skipped_code = report_layout.code_col is None and FIELD_CODE in dict(report_layout.decided)
        rows = _collect_rows(
            df,
            report_layout.header_row,
            report_layout.barcode_col,
            report_layout.code_col,
            report_layout.row_col,
            report_layout.name_col,
            name_for_code=not skipped_code,
        )
    else:
        raise ValueError(f"فرمت «{ext}» پشتیبانی نمی‌شود (فقط xlsx / csv / pdf).")

    if not rows:
        raise ValueError("هیچ ردیف داده‌ای در فایل پیدا نشد.")

    notes: list[str] = []
    if report_layout.barcode_col is not None and report_layout.code_col is None:
        lifted = sum(1 for r in rows if r.get("code_from_name"))
        if skipped_code:
            notes.append(
                "ستون کد سفارش را خودت «بدون» انتخاب کردی؛ هیچ کدی (حتی از نام گیرنده) خوانده نشد."
            )
        elif lifted:
            notes.append(
                f"{lifted} کد سفارش از ستونِ نام گیرنده برداشته شد — این‌ها را چشم‌بسته تأیید نکن."
            )
        else:
            notes.append("ستون «کد سفارش» نبود و در نام گیرنده هم کدی پیدا نشد؛ کدِ همهٔ ردیف‌ها خالی است.")
    destroyed = sum(1 for r in rows if _destroyed(r))
    if destroyed:
        notes.append(
            f"{destroyed} بارکد در اکسل به‌صورت عدد ذخیره شده بود و رقم‌هایش رفته است — "
            "خروجیِ متنی (CSV) یا همان PDFِ سامانه را بفرست، اکسل نه."
        )

    problems = _analyze(rows, report_layout)
    usable = sum(1 for r in rows if r.get("barcode_state") in ("ok", "warn"))
    return Report(
        csv_text=build_csv(rows),
        summary=build_summary(rows, problems, fname, report_layout, tuple(notes)),
        problems_csv=build_problems_csv(problems),
        review_csv=build_review_csv(rows, report_layout),
        review_xlsx=build_review_workbook(rows, report_layout),
        layout=report_layout,
        notes=tuple(notes),
        fname=fname,
        rows=len(rows),
        usable=usable,
        errors=sum(1 for p in problems if p[1] == "❌"),
        warnings=sum(1 for p in problems if p[1] == "⚠️"),
        needs_review=len(review_rows(rows)),
        dropped=len(unwritten_rows(rows)),
    )


__all__ = [
    "FIELD_BARCODE",
    "FIELD_CODE",
    "Column",
    "Layout",
    "Question",
    "Report",
    "RowLimitError",
    "build_csv",
    "build_problems_csv",
    "build_review_csv",
    "build_review_workbook",
    "build_summary",
    "process_file",
    "review_rows",
    "scan_table",
    "unwritten_rows",
]
