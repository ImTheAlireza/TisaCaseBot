"""Resolve the regular price for a product variation.

Model-specific overrides are the most precise rule, then the legacy iPhone / Android
price groups, then the product's common price. This is shared by preview, validation,
and the WooCommerce payload so they cannot disagree about what a model costs.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from bot.services.color_matrix import model_signature


def price_for_model(
    model: str,
    common: int,
    group_prices: Mapping[str, int] | None = None,
    model_prices: Mapping[str, int] | None = None,
) -> int:
    """Return the effective regular price for ``model`` (0 means unresolved)."""
    groups = group_prices or {}
    overrides = model_prices or {}
    if model:
        signature = model_signature(model)
        matches = [
            int(value)
            for label, value in overrides.items()
            if model_signature(str(label)) == signature and int(value or 0) > 0
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1 and len(set(matches)) == 1:
            return matches[0]

    is_iphone = bool(re.search(r"(?i)\b(?:iphone|apple)\b|آیفون|ایفون|اپل", model or ""))
    group = "iphone" if is_iphone else "android"
    group_price = int(groups.get(group) or 0)
    return group_price or int(common or 0)


def match_model_labels(models: list[str]) -> dict[str, list[str]]:
    """Map normalized model signatures to all exact option labels sharing them."""
    signatures: dict[str, list[str]] = {}
    for model in models:
        if model:
            signatures.setdefault(model_signature(model), []).append(model)
    return signatures


def unresolved_models(
    models: list[str],
    common: int,
    group_prices: Mapping[str, int] | None = None,
    model_prices: Mapping[str, int] | None = None,
) -> list[str]:
    """Models that would be built with no price at all.

    A continuation of :func:`price_for_model` for validation and the preview: a
    model with no override, no group price and no common price has no honest
    value, and the caller must ask instead of publishing a zero price.
    """
    return [
        model
        for model in models
        if model and price_for_model(model, common, group_prices, model_prices) <= 0
    ]


def map_labels(
    labels: Mapping[str, int], models: Sequence[str]
) -> tuple[dict[str, int], list[str]]:
    """Match typed price labels to the product's exact model options.

    Returns ``(matched, unmatched)``: a label matching exactly one model keeps the
    exact option spelling, everything else is reported instead of guessed.
    """
    signatures = match_model_labels(list(models))
    matched: dict[str, int] = {}
    unmatched: list[str] = []
    for label, value in labels.items():
        matches = signatures.get(model_signature(str(label)), [])
        if len(matches) == 1 and int(value or 0) > 0:
            matched[matches[0]] = int(value)
        else:
            unmatched.append(str(label))
    return matched, unmatched


__all__ = ["map_labels", "match_model_labels", "price_for_model", "unresolved_models"]
