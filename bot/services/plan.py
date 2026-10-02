"""One source of truth for «what will actually be created».

The bot used to count variations in three places that disagreed:

* the Telegram preview called ``color_matrix.variation_count`` (no dedupe),
* the REST writer rebuilt the axes itself in ``woocommerce_direct._attributes``
  (dedupe + drop any axis that collapses to one value),
* the ZIP importer rebuilt them a third time from ``product.json``.

So the preview could honestly print «۴ واریژن» while WooCommerce received a
product with one attribute axis — or even a *simple* product. The README promise
«پیش‌نمایش، تعداد واقعی variation را نشان می‌دهد — همان عددی که ساخته می‌شود»
was only true by luck.

Everything now goes through :func:`build_plan`. The preview shows
``plan.count``, the REST payload uses ``plan.woo_attributes()`` and the ZIP
manifest uses ``plan.manifest_attributes()``. They cannot drift, because they
are the same object.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Iterable, Sequence

from bot.services.color_matrix import (
    build_combinations,
    is_model_attribute,
    restrict_combinations,
)


def clean_values(values: Iterable[Any]) -> list[str]:
    """Strip, drop empties, dedupe (first spelling wins)."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        text = re.sub(r"\s+", " ", str(value)).strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _matrix_key(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


@dataclass
class VariationPlan:
    """The exact matrix that will be created, plus what was dropped on the way."""

    axes: list[tuple[str, list[str]]] = field(default_factory=list)
    combos: list[dict[str, str]] = field(default_factory=list)
    dropped: list[tuple[str, int, int]] = field(default_factory=list)  # (name, in, out)
    models: list[str] = field(default_factory=list)
    restrictions: dict[str, list[str]] = field(default_factory=dict)
    stock_matrix: dict[str, dict[str, int | None]] = field(default_factory=dict)
    matrix_missing: list[tuple[str, str]] = field(default_factory=list)
    matrix_axis_errors: list[str] = field(default_factory=list)
    matrix_unavailable: int = 0

    @property
    def count(self) -> int:
        return len(self.combos)

    @property
    def is_variable(self) -> bool:
        return bool(self.axes)

    @property
    def naive_count(self) -> int:
        total = 1
        for _name, values in self.axes:
            total *= max(1, len(values))
        return total

    @property
    def restricted(self) -> bool:
        return self.naive_count != self.count

    def attribute_names(self) -> list[str]:
        return [name for name, _values in self.axes]

    def woo_attributes(self) -> list[dict[str, Any]]:
        """The ``attributes`` array for ``POST /wp-json/wc/v3/products``."""
        return [
            {"name": name, "visible": True, "variation": True, "options": values}
            for name, values in self.axes
        ]

    def manifest_attributes(self) -> dict[str, list[str]]:
        """The ``attributes`` object for ``product.json`` (ZIP importer)."""
        return {name: list(values) for name, values in self.axes if name != "مدل"}

    def matrix_stock_for(self, combo: dict[str, str]) -> int | None:
        """Return the explicit quantity for a planned model × design pair."""
        if not self.stock_matrix:
            return None
        design = combo.get("طرح") or (next(iter(self.stock_matrix)) if len(self.stock_matrix) == 1 else "")
        row = next(
            (values for label, values in self.stock_matrix.items() if _matrix_key(label) == _matrix_key(design)),
            None,
        )
        if row is None:
            return None
        model = combo.get("مدل")
        model_labels = list(dict.fromkeys(label for values in self.stock_matrix.values() for label in values))
        if not model and len(model_labels) == 1:
            model = model_labels[0]
        return next(
            (quantity for label, quantity in row.items() if _matrix_key(label) == _matrix_key(model)),
            None,
        )

    def summary(self) -> str:
        if not self.axes:
            # The dropped axes must still be named here: three colour lines that
            # were one color are something the seller can fix, and a card that
            # only says "simple product" reads as if the bot ignored them.
            text = "هیچ ویژگی قابل‌انتخابی نمانده؛ محصول simple ساخته می‌شود."
            if self.dropped:
                text += "\n" + _dropped_text(self.dropped)
            return self._matrix_summary(text)
        parts = [f"{name} ({len(values)})" for name, values in self.axes]
        text = " | ".join(parts)
        if self.restricted:
            text += f" ← {self.naive_count} ترکیب کامل، {self.count} ترکیب معتبر"
        else:
            text += f" = {self.count} واریژن"
        if self.dropped:
            text += "\n" + _dropped_text(self.dropped)
        return self._matrix_summary(text)

    def _matrix_summary(self, text: str) -> str:
        if not self.stock_matrix:
            return text
        models = list(dict.fromkeys(
            label for row in self.stock_matrix.values() for label in row
        ))
        total = sum(
            quantity for row in self.stock_matrix.values() for quantity in row.values()
            if quantity is not None
        )
        text += (
            f"\nموجودی ماتریسی: {len(self.stock_matrix)} طرح × {len(models)} دسته؛ "
            f"{total:,} عدد"
        )
        if self.matrix_unavailable:
            text += f"؛ {self.matrix_unavailable} ترکیب ساخته نمی‌شود (−)"
        if self.matrix_missing:
            text += f"؛ ⚠️ {len(self.matrix_missing)} خانهٔ موجودی نامشخص"
        if self.matrix_axis_errors:
            text += "؛ ⚠️ ماتریس با محورهای ویژگی جور نیست"
        return text


def _dropped_text(dropped: list[tuple[str, int, int]]) -> str:
    """One line saying which attribute collapsed and why it is not an axis."""
    return "؛ ".join(f"«{name}» با {had} مقدار به {left} رسید و حذف شد" for name, had, left in dropped)


def _matrix_cell(
    matrix: dict[str, dict[str, int | None]], design: str, model: str
) -> tuple[bool, int | None]:
    row = next(
        (values for label, values in matrix.items() if _matrix_key(label) == _matrix_key(design)),
        None,
    )
    if row is None:
        return False, None
    for label, quantity in row.items():
        if _matrix_key(label) == _matrix_key(model):
            return (type(quantity) is int or quantity is None), quantity
    return False, None


def build_plan(
    models: Sequence[str],
    attributes: dict[str, Sequence[str]] | None,
    restrictions: dict[str, Sequence[str]] | None = None,
    *,
    model_axis_name: str = "مدل",
    stock_matrix: dict[str, dict[str, int | None]] | None = None,
) -> VariationPlan:
    """Resolve models + attributes + per-model colours into the final matrix.

    Rules kept identical to the previous WooCommerce writer, because that one is
    what actually has to match the store:

    * a model list only becomes an axis when it has ≥ 2 distinct models;
    * every attribute needs ≥ 2 distinct values after dedupe, otherwise it is
      part of the product name, not a variation axis;
    * an attribute named like the model axis never duplicates it;
    * colours are then restricted to the pairs the seller actually listed.
    """
    clean_models = clean_values(models)
    axes: list[tuple[str, list[str]]] = []
    dropped: list[tuple[str, int, int]] = []
    used: set[str] = set()

    if len(clean_models) >= 2:
        axes.append((model_axis_name, clean_models))
        used.add(model_axis_name.casefold())

    for name, values in (attributes or {}).items():
        attribute_name = re.sub(r"\s+", " ", str(name)).strip()
        raw = clean_values(values if isinstance(values, (list, tuple, set)) else [values])
        if not attribute_name or attribute_name.casefold() in used or is_model_attribute(attribute_name):
            if attribute_name and attribute_name.casefold() in used:
                dropped.append((attribute_name, len(raw), 0))
            continue
        used.add(attribute_name.casefold())
        if len(raw) < 2:
            if len(list(values or [])) >= 2:
                dropped.append((attribute_name, len(list(values or [])), len(raw)))
            continue
        axes.append((attribute_name, raw))

    clean_restrictions = {
        str(model): clean_values(colors)
        for model, colors in (restrictions or {}).items()
        if clean_values(colors)
    }
    combos = build_combinations(axes, clean_restrictions)
    if len(axes) > 1 and len(clean_restrictions) > 1:
        # ``build_combinations`` may fall back to the unfiltered matrix when a
        # naming mismatch makes the restriction unusable; keep the real count.
        combos = restrict_combinations(combos, clean_restrictions) or combos

    matrix = {
        str(design): {
            str(model): quantity
            for model, quantity in values.items()
        }
        for design, values in (stock_matrix or {}).items()
        if isinstance(values, dict)
    }
    matrix_missing: list[tuple[str, str]] = []
    matrix_axis_errors: list[str] = []
    matrix_unavailable = 0
    if matrix:
        axis_names = {name.casefold() for name, _values in axes}
        extra_axes = sorted(name for name, _values in axes if name.casefold() not in {"مدل", "طرح"})
        if extra_axes:
            matrix_axis_errors.append("موجودی ماتریسی فقط محورهای «مدل» و «طرح» را پوشش می‌دهد")
        matrix_designs = list(matrix)
        matrix_models = list(dict.fromkeys(
            model for row in matrix.values() for model in row
        ))
        if len(matrix_designs) > 1 and "طرح" not in axis_names:
            matrix_axis_errors.append("محور «طرح» در گزینه‌های محصول پیدا نشد")
        if len(matrix_models) > 1 and model_axis_name.casefold() not in axis_names:
            matrix_axis_errors.append("محور مدل‌های ماتریس در گزینه‌های محصول پیدا نشد")
        if clean_models and matrix_models and {
            _matrix_key(value) for value in clean_models
        } != {_matrix_key(value) for value in matrix_models}:
            matrix_axis_errors.append("دسته‌های گوشیِ محصول با سرستون‌های ماتریس یکی نیست")
        if len(matrix_designs) > 1:
            attributes_designs = next(
                (values for name, values in axes if name.casefold() == "طرح"), []
            )
            if {_matrix_key(value) for value in attributes_designs} != {
                _matrix_key(value) for value in matrix_designs
            }:
                matrix_axis_errors.append("نام طرح‌های محصول با ردیف‌های ماتریس یکی نیست")

        planned: list[dict[str, str]] = []
        for combo in combos:
            design = combo.get("طرح") or (matrix_designs[0] if len(matrix_designs) == 1 else "")
            model = combo.get(model_axis_name) or (matrix_models[0] if len(matrix_models) == 1 else "")
            found, quantity = _matrix_cell(matrix, design, model)
            if not found:
                matrix_missing.append((design or "؟", model or "؟"))
                planned.append(combo)
            elif quantity is None:
                matrix_unavailable += 1
            else:
                planned.append(combo)
        combos = planned

    return VariationPlan(
        axes=axes,
        combos=combos,
        dropped=dropped,
        models=clean_models,
        restrictions=clean_restrictions,
        stock_matrix=matrix,
        matrix_missing=matrix_missing,
        matrix_axis_errors=list(dict.fromkeys(matrix_axis_errors)),
        matrix_unavailable=matrix_unavailable,
    )


def plan_from_dict(data: dict[str, Any]) -> VariationPlan:
    """Same plan, from ``ProductData.to_dict()`` (used by both output paths)."""
    return build_plan(
        data.get("models") or [],
        data.get("attributes") or {},
        data.get("model_colors") or {},
        stock_matrix=data.get("stock_matrix") or None,
    )


__all__ = ["VariationPlan", "build_plan", "clean_values", "plan_from_dict"]
