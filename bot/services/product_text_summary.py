"""Extract and format models/attributes from product captions and text."""
from __future__ import annotations

import html


def format_product_summary(models: list[str], attributes: dict[str, list[str]]) -> str:
    """Compact admin-facing result; pipe-separated values are easy to copy."""
    clean_models = list(dict.fromkeys(html.escape(str(model).strip()) for model in models if str(model).strip()))
    feature_lines: list[str] = []
    for name, raw_values in attributes.items():
        values = list(dict.fromkeys(html.escape(str(value).strip()) for value in raw_values if str(value).strip()))
        if values:
            feature_lines.append(f"{html.escape(str(name).strip())}: {' | '.join(values)}")
    return (
        "🔎 <b>نتیجهٔ تشخیص از کپشن و متن</b>\n"
        f"مدل‌ها: {(' | '.join(clean_models) if clean_models else 'پیدا نشد')}\n"
        f"ویژگی‌ها: {('؛ '.join(feature_lines) if feature_lines else 'پیدا نشد')}"
    )
