"""Restart the bot through supervisor.

Button «🔄 ری‌استارت» → confirmation screen → `supervisorctl restart <program>`.

The supervisorctl child is spawned in its own session so it survives the
bot process being stopped mid-restart. Before running it, we persist the
chat/message id to data/restart_pending.json; on the next startup
(post_init → notify_restart_complete) the bot edits that message to
"✅ back online" — real proof the restart cycle worked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from bot import rbac
from bot.config import data_dir, settings
from bot.constants import CB
from bot.utils.ui import answer_and, answer_and_edit
from dotenv import dotenv_values

logger = logging.getLogger(__name__)

# An absolute path from `data_dir()`: it works regardless of supervisor's cwd *and*
# follows TISA_DATA_DIR, so a deploy that cleans the code directory cannot strand a
# half-finished restart note next to the repo.
PENDING_FILE = data_dir() / "restart_pending.json"
STARTUP_NOTIFY_MAX_AGE = 300  # seconds — ignore stale pending files

# Common supervisord config / socket locations. Debian/Ubuntu system paths
# first, then per-user setups (e.g. shared hosting: ~/supervisord.conf).
_COMMON_CONFS = (
    "/etc/supervisor/supervisord.conf",
    "/etc/supervisord.conf",
    str(Path.home() / "supervisord.conf"),
    str(Path.home() / "etc" / "supervisord.conf"),
    str(Path.home() / ".supervisord.conf"),
)
#: مکان‌های رایج سوکت سوپروایزر. به /tmp آخر نگاه می‌کنیم چون در هاست‌های با
#: PrivateTmp یا TMPDIR متفاوت، سوکت همنامِ قابل‌پیش‌بینی در /tmp می‌تواند متعلق
#: به حساب دیگری باشد (و آن وقت supervisorctl فقط خطای اتصال می‌دهد و امنیتی
#: شکسته نمی‌شود، اما پیدا کردنش کند می‌شود). مسیر سیستم اول می‌آید.
def _common_sockets() -> tuple[str, ...]:
    import tempfile
    tmp = tempfile.gettempdir()  # به TMPDIR/TMP احترام می‌گذارد
    return (
        "/var/run/supervisor.sock",
        "/run/supervisor.sock",
        "/var/run/supervisord.sock",
        str(Path(tmp) / "supervisor.sock"),
    )


def _runtime_supervisor_config() -> tuple[str, str, str, str]:
    """Read supervisor settings fresh for every click.

    This matters on shared hosting: changing SUPERVISOR_PROGRAM in .env cannot
    restart the old process first, so values captured at Python import time
    would otherwise leave us stuck on the previous program name.
    """
    values = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
    return (
        str(values.get("SUPERVISOR_PROGRAM") or settings.supervisor_program).strip(),
        str(values.get("SUPERVISORCTL_BIN") or settings.supervisorctl_bin).strip(),
        str(values.get("SUPERVISOR_CONF") or settings.supervisor_conf).strip(),
        str(values.get("SUPERVISOR_URL") or settings.supervisor_url).strip(),
    )


def _candidate_commands() -> list[list[str]]:
    """Build supervisorctl invocations to try, in order."""
    program, bin_, supervisor_conf, supervisor_url = _runtime_supervisor_config()
    tail = ["restart", program]

    if supervisor_conf or supervisor_url:
        cmd = [bin_]
        if supervisor_conf:
            cmd += ["-c", supervisor_conf]
        if supervisor_url:
            url = supervisor_url
            # -s wants a URL; tolerate a bare socket path in .env
            if "://" not in url:
                url = f"unix://{url}"
            cmd += ["-s", url]
        return [[*cmd, *tail]]

    candidates = [[bin_, *tail]]
    for conf in _COMMON_CONFS:
        if Path(conf).is_file():
            candidates.append([bin_, "-c", conf, *tail])
    for sock in _common_sockets():
        if Path(sock).exists():
            candidates.append([bin_, "-s", f"unix://{sock}", *tail])
    return candidates


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ بله، ری‌استارت کن", callback_data=CB.RESTART_CONFIRM)],
            [InlineKeyboardButton("⬅️ بازگشت به منو", callback_data=CB.MAIN_MENU)],
        ]
    )


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ بازگشت به منو", callback_data=CB.MAIN_MENU)]]
    )


async def cb_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Restart button → ask for confirmation."""
    query = update.callback_query
    user = update.effective_user
    if not user or not rbac.is_sudo(user.id):
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    await answer_and_edit(
        query,
        "🔄 <b>ری‌استارت ربات</b>\n\n"
        f"ربات از طریق supervisor ری‌استارت می‌شود:\n"
        f"<code>{' '.join(_candidate_commands()[0])}</code>\n\n"
        "مطمئنی؟",
        reply_markup=_confirm_keyboard(),
        parse_mode="HTML",
    )


async def cb_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirmed → persist pending-marker, run supervisorctl restart."""
    query = update.callback_query
    user = update.effective_user
    if not user or not rbac.is_sudo(user.id):
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    logger.warning("Restart requested by user %s via supervisor", user.id if user else "?")

    msg = await answer_and(query, query.edit_message_text("♻️ در حال ری‌استارت از طریق supervisor…"))

    # Marker so the freshly-started process can confirm success in this chat.
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(
        json.dumps(
            {"chat_id": msg.chat_id, "message_id": msg.message_id, "ts": time.time()}
        ),
        encoding="utf-8",
    )

    try:
        attempts: list[str] = []
        success = False
        for cmd in _candidate_commands():
            # start_new_session=True → detached from our process group, so it
            # survives supervisor stopping us and still issues the `start` part.
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            output = (out_b or b"").decode(errors="replace").strip()
            attempts.append(f"$ {' '.join(cmd)}\n{output or '—'} (rc={proc.returncode})")
            # Connection problems → supervisord not reachable this way; try next.
            if "refused connection" in output or "no such file" in output.lower():
                continue
            success = proc.returncode == 0 and "ERROR" not in output
            break
    except FileNotFoundError:
        PENDING_FILE.unlink(missing_ok=True)
        await msg.edit_text(
            f"❌ <code>{settings.supervisorctl_bin}</code> پیدا نشد.\n"
            "مسیر آن را در <code>SUPERVISORCTL_BIN</code> فایل .env تنظیم کن.",
            reply_markup=_menu_keyboard(),
            parse_mode="HTML",
        )
        return
    except TimeoutError:
        # Most likely we're being stopped right now — let the restart proceed.
        return

    if success:
        # supervisorctl says the program restarted. Normally WE are that
        # program and supervisor kills us any moment now — the new process
        # will edit the message via the pending marker. Wait quietly instead
        # of racing it; if we're STILL alive afterwards, the restarted
        # program wasn't us → misconfiguration.
        await asyncio.sleep(15)
        logger.error("Restart reported success but bot is still alive:\n%s", "\n\n".join(attempts))
        PENDING_FILE.unlink(missing_ok=True)
        await msg.edit_text(
            "⚠️ supervisor برنامه را ری‌استارت کرد ولی این ربات هنوز زنده است!\n\n"
            f"یعنی <code>SUPERVISOR_PROGRAM={_runtime_supervisor_config()[0]}</code> "
            "به برنامه‌ی دیگری اشاره می‌کند، نه به خود ربات.\n"
            "با <code>supervisorctl status</code> نام درست را پیدا کن.",
            reply_markup=_menu_keyboard(),
            parse_mode="HTML",
        )
        return

    # All attempts failed (supervisord unreachable, bad program name, ...).
    report = "\n\n".join(attempts)
    logger.error("supervisorctl restart failed, bot still alive:\n%s", report)
    PENDING_FILE.unlink(missing_ok=True)
    await msg.edit_text(
        "❌ ری‌استارت انجام نشد — ربات هنوز زنده است.\n\n"
        f"<code>{report[:700]}</code>\n\n"
        "راهنما:\n"
        "• «refused connection» یعنی supervisord در دسترس نیست — مقدار "
        "<code>SUPERVISOR_CONF</code> (مثلاً <code>/etc/supervisor/supervisord.conf</code>) "
        "یا <code>SUPERVISOR_URL</code> را در .env تنظیم کن.\n"
        "• «no such process» یعنی <code>SUPERVISOR_PROGRAM</code> اشتباه است "
        "(با <code>supervisorctl status</code> نام درست را ببین).\n"
        "• «permission denied» یعنی یوزر ربات به سوکت supervisor دسترسی ندارد.",
        reply_markup=_menu_keyboard(),
        parse_mode="HTML",
    )


async def notify_restart_complete(app: Application) -> None:
    """Called from post_init on every startup: if a restart was pending,
    edit the «در حال ری‌استارت» message to confirm the bot is back."""
    if not PENDING_FILE.exists():
        return
    try:
        data = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        PENDING_FILE.unlink(missing_ok=True)
        return
    PENDING_FILE.unlink(missing_ok=True)

    if time.time() - float(data.get("ts", 0)) > STARTUP_NOTIFY_MAX_AGE:
        return  # stale marker from an old crash — don't ping anyone

    text = "✅ ربات با موفقیت ری‌استارت شد و دوباره آنلاین است."
    try:
        await app.bot.edit_message_text(
            chat_id=data["chat_id"],
            message_id=data["message_id"],
            text=text,
            reply_markup=_menu_keyboard(),
        )
    except Exception:
        try:
            await app.bot.send_message(
                chat_id=data["chat_id"], text=text, reply_markup=_menu_keyboard()
            )
        except Exception:
            logger.exception("Could not deliver restart confirmation")


def register(app: Application) -> None:
    app.add_handler(CallbackQueryHandler(cb_ask, pattern=f"^{CB.RESTART_ASK}$"))
    app.add_handler(CallbackQueryHandler(cb_confirm, pattern=f"^{CB.RESTART_CONFIRM}$"))
