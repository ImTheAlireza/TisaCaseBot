"""Application settings, loaded once from environment / .env file.

Every value is parsed *defensively*: a typo in ``.env`` must never crash the bot
at import time (it would leave supervisor crash-looping with an unreadable
traceback). Bad values fall back to the documented default and the problem is
logged, so ``LOG_LEVEL=DEBUG`` or the ``📊 وضعیت`` screen can surface it.
"""

from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Anchored to the repo root, so `python3 /path/main.py` from any cwd works
# (supervisor does not always set `directory=`).
REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_env_file(path: Path) -> None:
    """Read `.env` if python-dotenv is installed — a convenience, never a need.

    A shared host without pip access still runs the bot: supervisor or the shell
    provides the variables. Dying at import for this was how a deploy became a
    crash-loop with no readable reason.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:      # ModuleNotFoundError, or a broken install
        return
    load_dotenv(path)


_load_env_file(REPO_ROOT / ".env")


def data_dir() -> Path:
    """Where the bot's small JSON stores live (roles, ledger, vocabulary, …).

    Default is ``<repo>/data``. ``TISA_DATA_DIR`` moves it, for two reasons: on a
    shared host the writable state does not belong inside the code directory (a
    ``git clean`` or a redeploy deletes the shop's history), and the test suite must
    not write into the real ``recent_products.json`` — it did, and once the ledger
    became something the bot *reads* before publishing, that stopped being harmless.
    """
    raw = _raw("TISA_DATA_DIR")
    return Path(raw).expanduser() if raw else Path(__file__).resolve().parents[1] / "data"


def _raw(name: str, default: str = "") -> str:
    return (os.getenv(name) or "").strip() or default.strip()


def _as_str(name: str, default: str) -> tuple[str, str | None]:
    value = _raw(name, default)
    return value, None


def _as_float(name: str, default: float) -> tuple[float, str | None]:
    raw = _raw(name, "")
    if not raw:
        return default, None
    try:
        value = float(raw)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("must be positive and finite")
        return value, None
    except ValueError:
        return default, f"{name}={raw!r} یک عدد نیست؛ پیش‌فرض {default} استفاده شد."


def _as_int(name: str, default: int) -> tuple[int, str | None]:
    raw = _raw(name, "")
    if not raw:
        return default, None
    try:
        return int(raw.replace(",", "").replace("٬", "")), None
    except ValueError:
        return default, f"{name}={raw!r} یک عدد صحیح نیست؛ پیش‌فرض {default} استفاده شد."


def _as_bool(name: str, default: bool) -> tuple[bool, str | None]:
    raw = _raw(name, "").lower()
    if not raw:
        return default, None
    if raw in {"1", "true", "yes", "y", "on", "ok"}:
        return True, None
    if raw in {"0", "false", "no", "n", "off"}:
        return False, None
    return default, f"{name}={raw!r} منطقی نیست؛ پیش‌فرض {default} استفاده شد."


def _as_ids(name: str) -> tuple[frozenset[int], str | None]:
    raw = _raw(name, "").replace(";", ",")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    ids: set[int] = set()
    bad: list[str] = []
    short: list[str] = []
    for part in parts:
        if re.fullmatch(r"-?\d{1,20}", part):
            value = int(part)
            if value <= 0:
                bad.append(part)
                continue
            ids.add(value)
            # Real Telegram user ids are long. A short one is almost always a
            # copy-paste mistake — but rejecting it outright used to leave the
            # owner with no sudo at all and only a log line to explain why, so
            # it is accepted and flagged instead.
            if len(part) < 4:
                short.append(part)
        else:
            bad.append(part)
    warnings: list[str] = []
    if bad:
        warnings.append(f"{name}: مقدار نامعتبر {bad} نادیده گرفته شد.")
    if short:
        warnings.append(f"{name}: شناسهٔ کوتاه {short} پذیرفته شد؛ مطمئن شو id کاملِ کاربر است.")
    return frozenset(ids), (" ".join(warnings) or None)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    # Telegram user ID(s) of the bot owner — the only *sudo*. Full access.
    sudo_ids: frozenset[int] = field(default_factory=frozenset)
    log_level: str = "INFO"
    supervisor_program: str = "tisabot"
    supervisorctl_bin: str = "supervisorctl"
    supervisor_conf: str = ""
    supervisor_url: str = ""
    # Phone-post processor (migrated from the former OPTION bot).
    album_wait_seconds: float = 1.8
    max_download_mb: float = 20.0
    image_quality: int = 88
    ai_base_url: str = ""
    ai_token: str = ""
    ai_model: str = ""
    ai_timeout_seconds: float = 30.0
    # Fixed Telegram chat receiving the product-processing audit trail.
    # There is deliberately NO hard-coded default: shipping the wrong one here
    # silently sends every shop's product data to someone else's chat group.
    log_chat_id: int | None = None
    woocommerce_url: str = ""
    woocommerce_key: str = ""
    woocommerce_secret: str = ""
    woocommerce_version: str = "wc/v3"
    wordpress_url: str = ""
    wordpress_username: str = ""
    wordpress_app_password: str = ""
    # --- sanity gates for the parsed product (see bot/services/money.py) ------
    # A price outside this range is almost certainly a misread (a date, a
    # weight, a tracking code) and must be confirmed, never silently published.
    price_min: int = 1_000
    price_max: int = 500_000_000
    # Built-in heuristic: a bare 3-digit amount means thousands of toman.
    bare_three_digit_means_thousands: bool = True
    # Opt-in stricter gate; model-less products are simple products by default.
    require_models: bool = False
    #: TISA_DRY_RUN=1 → the whole publish path runs, but the socket is replaced by a
    #: fake transport: nothing is created on the shop. Built so that a release can be
    #: rehearsed on the real site without polluting the catalog.
    woo_dry_run: bool = False
    # Accepted tracking-barcode lengths (the Tisa systems print 24 digits).
    barcode_lengths: frozenset[int] = field(default_factory=lambda: frozenset({24}))
    # Flow behaviour.
    flow_timeout_seconds: int = 900
    temp_ttl_hours: int = 12
    max_file_mb: float = 25.0
    max_rows: int = 200_000
    process_timeout_seconds: float = 120.0
    #: حافظه‌ای که کارگرِ فایل می‌تواند *روی پایهٔ خودش* خرج کند (MB). سقفِ کلِ
    #: پردازه نیست: ایمپورت‌های خودِ کارگر (pandas و…) روی همین عدد سوار می‌شوند.
    #: روی هاستِ کوچک، کمکردنش فایل‌ها را زودتر و صادقانه رد می‌کند؛ بالا بردنش
    #: اجازه می‌دهد فایل‌های بزرگ‌تر خوانده شوند. جزئیات در bot/services/worker.py.
    worker_memory_mb: float = 768.0
    #: VERBOSE_LOG=1 → besides the one product card, the full line-by-line trace of
    #: that product is sent to the log chat too (and written to the log file). Off by
    #: default on purpose: the card is what you read on a normal day.
    verbose_log: bool = False
    # Anything the environment got wrong; surfaced in logs / the status screen.
    problems: tuple[str, ...] = ()

    @property
    def limits_line(self) -> str:
        """سقف‌های فایل و ردیف، در یک خط — همان چیزی که چند صفحه نشان می‌دهند.

        این‌جا تعریف شده چون فقط از فیلدهای همین کلاس می‌خواند؛ صفحه‌ها نباید هر
        کدام نسخه‌شان را بنویسند و ماژول‌ها هم (قانونِ خانه) یکدیگر را import
        نمی‌کنند. پس «📊 وضعیت» همان عددی را می‌گوید که مبدل فایل قبل از دانلود
        به کاربر وعده داده است.
        """
        return (
            f"فایل تا {self.max_file_mb:g} MB، تا {self.max_rows:,} ردیف، "
            f"پردازش تا {self.process_timeout_seconds:g} ثانیه."
        )

    @classmethod
    def from_env(cls) -> Settings:
        token = _raw("BOT_TOKEN")
        problems: list[str] = []
        if not token:
            raise RuntimeError(
                "BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
            )

        def note(problem: str | None) -> None:
            if problem:
                problems.append(problem)

        sudo_ids, problem = _as_ids("SUDO_IDS")
        note(problem)
        if not sudo_ids:
            note("SUDO_IDS خالی است؛ هیچ کاربری دسترسی سودو ندارد.")

        album_wait, problem = _as_float("ALBUM_WAIT_SECONDS", 1.8)
        note(problem)
        if album_wait < 0.2:
            album_wait = 0.2
            note("ALBUM_WAIT_SECONDS خیلی کم بود؛ روی ۰٫۲ تنظیم شد.")
        max_download_mb, problem = _as_float("MAX_DOWNLOAD_MB", 20.0)
        note(problem)
        image_quality, problem = _as_int("IMAGE_QUALITY", 88)
        note(problem)
        image_quality = max(30, min(100, image_quality))
        ai_timeout, problem = _as_float("AI_TIMEOUT_SECONDS", 30.0)
        note(problem)

        log_chat_id, problem = _as_int("LOG_CHAT_ID", 0)
        note(problem)
        if log_chat_id and len(str(abs(log_chat_id))) < 8:
            note(f"LOG_CHAT_ID={log_chat_id} شبیه شناسهٔ معتبر چت نیست؛ خاموش شد.")
            log_chat_id = 0

        price_min, problem = _as_int("PRICE_MIN", 1_000)
        note(problem)
        price_max, problem = _as_int("PRICE_MAX", 500_000_000)
        note(problem)
        if price_min <= 0 or price_max <= price_min:
            note("PRICE_MAX باید بزرگ‌تر از PRICE_MIN باشد؛ به پیش‌فرض برگشت.")
            price_min, price_max = 1_000, 500_000_000

        require_models, problem = _as_bool("REQUIRE_MODELS", False)
        note(problem)
        woo_dry_run, problem = _as_bool("TISA_DRY_RUN", False)
        note(problem)
        verbose_log, problem = _as_bool("VERBOSE_LOG", False)
        note(problem)
        bare_thousands, problem = _as_bool("BARE_THREE_DIGIT_THOUSANDS", True)
        note(problem)

        barcode_lengths, problem = _as_int_list("BARCODE_LENGTHS", {24})
        note(problem)

        flow_timeout, problem = _as_int("FLOW_TIMEOUT_SECONDS", 900)
        note(problem)
        if flow_timeout < 60:
            flow_timeout = 60
            note("FLOW_TIMEOUT_SECONDS باید حداقل ۶۰ باشد؛ روی ۶۰ تنظیم شد.")
        temp_ttl, problem = _as_int("TEMP_TTL_HOURS", 12)
        note(problem)
        if temp_ttl <= 0:
            temp_ttl = 12
            note("TEMP_TTL_HOURS باید مثبت باشد؛ پیش‌فرض ۱۲ استفاده شد.")
        max_file_mb, problem = _as_float("MAX_FILE_MB", 25.0)
        note(problem)
        max_rows, problem = _as_int("MAX_ROWS", 200_000)
        note(problem)
        if max_rows <= 0:
            max_rows = 200_000
            note("MAX_ROWS باید مثبت باشد؛ پیش‌فرض ۲۰۰٬۰۰۰ استفاده شد.")
        process_timeout, problem = _as_float("PROCESS_TIMEOUT_SECONDS", 120.0)
        note(problem)
        worker_memory, problem = _as_float("WORKER_MEMORY_MB", 768.0)
        note(problem)
        if not (worker_memory >= 64):
            worker_memory = 768.0
            note("WORKER_MEMORY_MB باید حداقل ۶۴ باشد؛ پیش‌فرض ۷۶۸ استفاده شد.")

        log_level = _raw("LOG_LEVEL", "INFO").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            note(f"LOG_LEVEL={log_level!r} نامعتبر است؛ INFO استفاده شد.")
            log_level = "INFO"

        settings = cls(
            bot_token=token,
            sudo_ids=sudo_ids,
            log_level=log_level,
            supervisor_program=_raw("SUPERVISOR_PROGRAM", "tisabot"),
            supervisorctl_bin=_raw("SUPERVISORCTL_BIN", "supervisorctl"),
            supervisor_conf=_raw("SUPERVISOR_CONF"),
            supervisor_url=_raw("SUPERVISOR_URL"),
            album_wait_seconds=album_wait,
            max_download_mb=max_download_mb,
            image_quality=image_quality,
            ai_base_url=_raw("AI_BASE_URL"),
            ai_token=_raw("AI_TOKEN"),
            ai_model=_raw("AI_MODEL"),
            ai_timeout_seconds=ai_timeout,
            log_chat_id=log_chat_id or None,
            woocommerce_url=_raw("WOOCOMMERCE_URL"),
            woocommerce_key=_raw("WOOCOMMERCE_CONSUMER_KEY"),
            woocommerce_secret=_raw("WOOCOMMERCE_CONSUMER_SECRET"),
            woocommerce_version=_raw("WOOCOMMERCE_API_VERSION", "wc/v3"),
            wordpress_url=_raw("WORDPRESS_URL"),
            wordpress_username=_raw("WORDPRESS_USERNAME"),
            wordpress_app_password=_raw("WORDPRESS_APP_PASSWORD").replace(" ", ""),
            price_min=price_min,
            price_max=price_max,
            bare_three_digit_means_thousands=bare_thousands,
            require_models=require_models,
            woo_dry_run=woo_dry_run,
            verbose_log=verbose_log,
            barcode_lengths=barcode_lengths,
            flow_timeout_seconds=flow_timeout,
            temp_ttl_hours=temp_ttl,
            max_file_mb=max_file_mb,
            max_rows=max_rows,
            process_timeout_seconds=max(5.0, process_timeout),
            worker_memory_mb=worker_memory,
            problems=tuple(problems),
        )
        for problem in problems:
            logger.warning("config: %s", problem)
        return settings

    def is_sudo(self, user_id: int | None) -> bool:
        """True if the user is the bot owner (set via SUDO_IDS in .env)."""
        return bool(user_id) and user_id in self.sudo_ids


def _as_int_list(name: str, default: set[int]) -> tuple[frozenset[int], str | None]:
    raw = _raw(name, "").replace(";", ",")
    if not raw:
        return frozenset(default), None
    values: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if re.fullmatch(r"[0-9]+", part) and 4 <= int(part) <= 40:
            values.add(int(part))
        elif part:
            return frozenset(default), f"{name}: «{part}» طول معتبری ندارد؛ پیش‌فرض {sorted(default)}."
    if not values:
        return frozenset(default), None
    return frozenset(values), None


settings = Settings.from_env()
