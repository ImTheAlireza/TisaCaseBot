"""Deterministic parser for explicit design × model stock tables.

The format is deliberately small and auditable; stock is never guessed from a
photo or from an arbitrary number in a caption::

    موجودی ماتریسی:
    طرح | iPhone 13 | iPhone 13 Pro/13 Pro Max
    پروانه سرخابی | 77 | 47
    پاپیون آبی | 0 | -

A non-negative integer is the quantity (zero means a valid but sold-out
variation); ``-`` means that combination is not sold and must not be created.
The last complete/attempted matrix in PRODUCT INFO wins over CAPTION, just as
an explicit seller correction should.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from bot.services import money


_HEADING_RE = re.compile(
    r"(?i)^\s*(?:موجودی\s*(?:ماتریسی|ماتریس|جدولی)|stock\s*matrix)\s*[:：]?\s*$"
)
_DESIGN_HEADER = {"طرح", "طراحی", "design", "style"}
_UNAVAILABLE = {"-", "—", "–"}


def _cell_key(value: str) -> str:
    """Whitespace/case-insensitive identity for a matrix label."""
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def _quantity(value: str) -> tuple[bool, int | None]:
    """Parse one quantity; ``None`` is the explicit unavailable marker."""
    clean = (value or "").strip()
    if clean in _UNAVAILABLE:
        return True, None
    digits = money.digits(clean).replace(",", "").replace("٬", "").replace("،", "")
    if not re.fullmatch(r"\d{1,7}", digits):
        return False, None
    return True, int(digits)


@dataclass
class StockMatrix:
    """A parsed table, retaining errors so malformed stock can block publishing."""

    found: bool = False
    source: str = ""
    models: list[str] = field(default_factory=list)
    # design option -> model/category -> quantity; None means explicitly unavailable.
    quantities: dict[str, dict[str, int | None]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    row_count: int = 0

    @property
    def designs(self) -> list[str]:
        return list(self.quantities)

    @property
    def valid(self) -> bool:
        return self.found and bool(self.models) and bool(self.quantities) and not self.errors

    @property
    def sellable_cells(self) -> int:
        return sum(
            1 for row in self.quantities.values() for quantity in row.values()
            if quantity is not None
        )

    @property
    def total_stock(self) -> int:
        return sum(
            quantity for row in self.quantities.values() for quantity in row.values()
            if quantity is not None
        )


def parse_stock_matrix(text: str, *, source: str = "") -> StockMatrix:
    """Parse the last explicitly headed stock matrix in ``text``."""
    lines = (text or "").splitlines()
    starts = [index for index, line in enumerate(lines) if _HEADING_RE.fullmatch(line)]
    if not starts:
        return StockMatrix()

    start = starts[-1]
    result = StockMatrix(found=True, source=source)
    cursor = start + 1
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1
    if cursor >= len(lines) or "|" not in lines[cursor]:
        result.errors.append("بعد از «موجودی ماتریسی:» سطر سرستون‌ها را با | بنویس.")
        return result

    header = [cell.strip() for cell in lines[cursor].split("|")]
    cursor += 1
    if len(header) < 2 or _cell_key(header[0]) not in _DESIGN_HEADER:
        result.errors.append("خانهٔ اول سرستون باید «طرح» باشد و بعد نام مدل‌ها بیاید.")
        return result
    result.models = header[1:]
    if any(not model for model in result.models):
        result.errors.append("نام دستهٔ گوشی در سرستون خالی است.")
    seen_models: set[str] = set()
    for model in result.models:
        key = _cell_key(model)
        if key in seen_models:
            result.errors.append(f"دستهٔ گوشی تکراری در سرستون: «{model}».")
        seen_models.add(key)

    seen_designs: set[str] = set()
    while cursor < len(lines):
        line = lines[cursor]
        cursor += 1
        if not line.strip():
            break
        if "|" not in line:
            break
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) != len(result.models) + 1:
            result.errors.append(
                f"ردیف «{cells[0] if cells else ''}» باید دقیقاً {len(result.models)} عدد/خط تیره داشته باشد."
            )
            continue
        design = cells[0]
        if not design:
            result.errors.append("نام طرح در یکی از ردیف‌های ماتریس خالی است.")
            continue
        design_key = _cell_key(design)
        if design_key in seen_designs:
            result.errors.append(f"طرح تکراری در ماتریس: «{design}».")
            continue
        seen_designs.add(design_key)
        result.row_count += 1
        values: dict[str, int | None] = {}
        row_valid = True
        for model, cell in zip(result.models, cells[1:], strict=True):
            quantity_valid, parsed = _quantity(cell)
            if not quantity_valid:
                result.errors.append(
                    f"موجودی «{design} × {model}» باید عدد صحیح نامنفی یا «-» باشد؛ نه «{cell}»."
                )
                row_valid = False
            else:
                values[model] = parsed
        if row_valid:
            if not any(quantity is not None for quantity in values.values()):
                result.errors.append(f"طرح «{design}» هیچ ترکیب قابل‌فروشی ندارد؛ ردیف را بررسی کن.")
            result.quantities[design] = values
        else:
            # Keep the design visible in the preview, but an error prevents use.
            result.quantities[design] = values

    if not result.models:
        result.errors.append("ماتریس هیچ دستهٔ گوشی ندارد.")
    if result.row_count == 0:
        result.errors.append("ماتریس هیچ ردیف طرح معتبری ندارد.")
    return result


def parse_stock_matrix_sources(sources: Iterable[tuple[str, str]]) -> StockMatrix:
    """First source with a matrix wins (PRODUCT INFO should be passed first)."""
    for source, text in sources:
        parsed = parse_stock_matrix(text, source=source)
        if parsed.found:
            return parsed
    return StockMatrix()


def strip_stock_matrix_sections(text: str) -> str:
    """Remove explicitly headed matrix tables before title/color/AI extraction."""
    lines = (text or "").splitlines()
    out: list[str] = []
    inside = False
    saw_table_line = False
    for line in lines:
        if _HEADING_RE.fullmatch(line):
            inside = True
            saw_table_line = False
            continue
        if inside:
            if not line.strip() and not saw_table_line:
                # Permit a visual blank between the heading and column header.
                continue
            if not line.strip() and saw_table_line:
                inside = False
                out.append(line)
                continue
            if "|" in line:
                saw_table_line = True
                continue
            inside = False
        out.append(line)
    return "\n".join(out)
