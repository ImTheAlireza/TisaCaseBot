"""TisaCase management bot — entrypoint.

Usage:
    cp .env.example .env   # fill in BOT_TOKEN
    pip install -r requirements.txt
    python main.py

    python main.py --check-config   # validate .env and exit (no network)
    python main.py --version
"""

import argparse
import os
import sys

from bot import __version__
from bot.utils.logging import setup_logging


def check_config() -> int:
    """Print every configuration problem we found, then exit.

    On a shared host the bot runs under supervisor, where a bad ``.env`` used to
    mean a crash loop and a traceback nobody reads.
    """
    # Imported here rather than at the top: `--version` and `--check-config` have to work on a
    # host that has no .env yet — that is exactly when someone reaches for them.
    from bot.config import data_dir, settings

    problems = list(settings.problems)
    print(f"BOT_TOKEN: {'set' if settings.bot_token else 'MISSING'}")
    print(f"sudo ids : {sorted(settings.sudo_ids) or '— (هیچ!)'}")
    print(f"log chat : {settings.log_chat_id or '— (غیرفعال)'}")
    print(f"woo      : {settings.woocommerce_url or '—'}")
    print(f"wp media : {settings.wordpress_url or '—'}")
    print(f"ai       : {settings.ai_model or '—'} @ {settings.ai_base_url or '—'}")
    # The JSON stores are where roles and publish history live, and every writer
    # swallows its own OSError (a failed save must not kill a flow) — so an
    # unwritable directory used to mean "nothing is remembered", silently, forever.
    state = data_dir()
    state_problem = ""
    try:
        state.mkdir(parents=True, exist_ok=True)
        if not os.access(state, os.W_OK):
            state_problem = f"دایرکتوری state قابل‌نوشتن نیست: {state}"
    except OSError as exc:
        state_problem = f"دایرکتوری state ساخته/خوانده نشد: {state} ({exc})"
    print(f"state   : {state}{'' if not state_problem else '  ← ⚠️'}")
    if state_problem:
        problems.append(state_problem)
    # The outbox is the one store that acts on its own (a scheduled retry publishes a
    # product). A queue that cannot be written is not a degraded feature, it is a broken
    # promise — so it is checked the same way as the state directory, not left to the log.
    from bot.services import outbox as queue

    queue_problem = queue.probe()
    print(f"outbox  : {queue.DB_PATH}{'' if not queue_problem else '  ← ⚠️'}"
          + (f" ({queue.stats()['pending']} در صف)" if not queue_problem else ""))
    if queue_problem:
        problems.append(queue_problem)
    # The ledger of converted files only ever warns about a repeat, so a dead one must
    # not end the run — but it must be on this screen, because nowhere else would say it.
    from bot.services import tracking_ledger

    ledger_count, ledger_problem = tracking_ledger.probe()
    print(f"ledger  : {tracking_ledger.FILE} ({ledger_count} فایل)"
          + (f"  ← ⚠️ {ledger_problem}" if ledger_problem else ""))
    # افزونهٔ ZIP تنها چیزی است که خرابی‌اش «بی‌صدا» است: محصول ساخته می‌شود، فقط
    # تخفیف و موجودی‌اش نمی‌رسد. پس همین‌جا هم گفته می‌شود، نه فقط در عیب‌یابی.
    from bot.services import importer_contract

    importer_state, importer_detail, _ = importer_contract.describe()
    print(f"importer : افزونهٔ ZIP — {importer_detail}{'' if importer_state == 'ok' else '  ← ⚠️'}")
    if settings.woo_dry_run:
        print("dry-run  : 🧪 روشن (TISA_DRY_RUN) — هیچ محصول/تصویری در سایت نوشته نمی‌شود")
    else:
        print("dry-run  : خاموش (منتشر واقعی)")
    if problems:
        print("\n⚠️  مشکلات پیکربندی:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\n✅ پیکربندی سالم است.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="main.py", description="TisaCase management bot")
    parser.add_argument("--check-config", action="store_true", help="validate .env and exit")
    parser.add_argument("--version", action="store_true", help="print the bot version and exit")
    args = parser.parse_args()

    if args.version:
        print(__version__)
        return
    if args.check_config:
        sys.exit(check_config())

    from bot.config import settings        # late, for the same reason as above

    setup_logging(settings.log_level)
    if settings.problems:
        for problem in settings.problems:
            print(f"⚠️  config: {problem}", file=sys.stderr)

    from bot.app import build_application  # imported late so --check-config stays cheap

    app = build_application()
    # `drop_pending_updates=True` used to throw away everything the owner sent
    # while the bot was restarting (photos, order files) — with a restart button
    # in the menu, that was data loss on a schedule. Keep long polling's per-call
    # timeouts aligned with the dedicated, longer-lived HTTPX request client.
    app.run_polling(
        drop_pending_updates=False,
        connect_timeout=10.0,
        read_timeout=40.0,
        write_timeout=15.0,
        pool_timeout=10.0,
    )


if __name__ == "__main__":
    main()
