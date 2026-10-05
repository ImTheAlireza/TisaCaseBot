"""Ping — diagnostics button to verify the bot is alive and responsive."""

from __future__ import annotations

import html
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from bot import rbac
from bot.constants import CB
from bot.config import settings
from bot.services.shop_network import probe_shop_network
from bot.services.woocommerce import ping_woocommerce
from bot.services.wordpress_media import test_wordpress_media
from bot.services.woocommerce_product_test import test_product_with_image

logger = logging.getLogger(__name__)


def _dry_run_block() -> str:
    """Refuse the write-tests while a rehearsal is on.

    ``🖼️`` and ``📦`` really write to the shop (and delete after themselves), so with
    ``TISA_DRY_RUN=1`` they would break the one promise the flag makes. Turning the
    flag off is the fix, and saying so is cheaper than silently performing a write the
    user just asked the bot not to do anywhere.
    """
    return (
        "🧪 <b>حالت آزمایشی (TISA_DRY_RUN) روشن است</b>\n\n"
        "این تست برخلاف حالت آزمایشی، <b>واقعاً روی سایت می‌نویسد</b> و بعد پاکش می‌کند؛ "
        "پس با فلگ روشن اجرا نمی‌شود.\n"
        "برای اجرای آن، در <code>.env</code> بنویس <code>TISA_DRY_RUN=no</code> و ربات را ری‌استارت کن.\n\n"
        "برای همین تست بدون نوشتن: حالت آزمایشی را روشن بگذار و یک محصول را تا «✅ تأیید و ساخت» "
        "برو — همان payload ساخته می‌شود و فقط ارسال نمی‌شود."
    )


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🏓 تست خود ربات", callback_data=CB.PING)],
            [InlineKeyboardButton("🌐 تست اتصال ووکامرس", callback_data=CB.WOO_PING)],
            [InlineKeyboardButton("🖼️ تست آپلود تصویر", callback_data=CB.WP_MEDIA_PING)],
            [InlineKeyboardButton("📦 تست ساخت محصول با تصویر", callback_data=CB.WOO_PRODUCT_PING)],
            [InlineKeyboardButton("🛰 شبکهٔ سایت (بدون ترمینال)", callback_data=CB.SHOP_NETWORK_PING)],
            [InlineKeyboardButton("⬅️ بازگشت به منو", callback_data=CB.MAIN_MENU)],
        ]
    )


async def cb_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer the Ping button with a round-trip time measurement."""
    query = update.callback_query
    user = update.effective_user
    # Ping is sudo-only (hidden from admins, but re-checked here anyway).
    if not user or not rbac.is_sudo(user.id):
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    started = time.perf_counter()
    await query.answer("Pong! 🏓")
    rtt_ms = (time.perf_counter() - started) * 1000

    logger.info("Ping from user %s — %.0f ms", update.effective_user.id, rtt_ms)

    await query.edit_message_text(
        f"🏓 <b>Pong!</b>\n\n"
        f"API round-trip: <code>{rtt_ms:.0f} ms</code>\n"
        f"Bot is up and responding. ✅",
        reply_markup=_back_keyboard(),
        parse_mode="HTML",
    )



async def cb_woocommerce_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check the bot's actual outbound connection to WooCommerce REST API."""
    query = update.callback_query
    user = update.effective_user
    if not user or not rbac.is_sudo(user.id):
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await query.answer("در حال تست اتصال...")
    if not all((settings.woocommerce_url, settings.woocommerce_key, settings.woocommerce_secret)):
        await query.edit_message_text(
            "🌐 <b>تست اتصال ووکامرس</b>\n\n"
            "❌ اطلاعات اتصال کامل نیست. این متغیرها را در <code>.env</code> تنظیم کن:\n"
            "<code>WOOCOMMERCE_URL</code>\n<code>WOOCOMMERCE_CONSUMER_KEY</code>\n<code>WOOCOMMERCE_CONSUMER_SECRET</code>",
            reply_markup=_back_keyboard(), parse_mode="HTML",
        )
        return
    result = await ping_woocommerce(
        settings.woocommerce_url,
        settings.woocommerce_key,
        settings.woocommerce_secret,
        version=settings.woocommerce_version,
    )
    icon = "✅" if result.ok else "❌"
    status = f"HTTP {result.status_code}" if result.status_code else "بدون پاسخ HTTP"
    await query.edit_message_text(
        f"🌐 <b>تست اتصال ووکامرس</b>\n\n{icon} <b>نتیجه:</b> {result.message}\n"
        f"<b>وضعیت:</b> <code>{status}</code>\n"
        f"<b>زمان پاسخ:</b> <code>{result.elapsed_ms:.0f} ms</code>",
        reply_markup=_back_keyboard(), parse_mode="HTML",
    )


async def cb_shop_network_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Four read-only GETs that name the broken layer — for an admin with no shell access.

    Deliberately *not* skipped in dry-run: nothing here writes, and the outage this reports on is
    precisely the reason somebody cannot reach the site to test by hand.
    """
    query = update.callback_query
    user = update.effective_user
    if not user or not rbac.is_sudo(user.id):
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await query.answer("در حال پرسیدن چهار سؤال از سایت…")
    result = await probe_shop_network()
    body = "\n".join(html.escape(line) for line in result.report().splitlines())
    await query.edit_message_text(
        f"🛰 <b>شبکهٔ سایت (از همین سرور، بدون نوشتن)</b>\n\n{body}\n\n"
        f"<i>{result.elapsed_ms:.0f} ms · همهٔ درخواست‌ها GET هستند</i>",
        reply_markup=_back_keyboard(), parse_mode="HTML",
    )


async def cb_media_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Upload and immediately delete a 1x1 test image through WP Media API."""
    query = update.callback_query
    user = update.effective_user
    if not user or not rbac.is_sudo(user.id):
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    if settings.woo_dry_run:
        await query.answer("🧪 حالت آزمایشی روشن است", show_alert=True)
        await query.edit_message_text(_dry_run_block(), reply_markup=_back_keyboard(), parse_mode="HTML")
        return
    await query.answer("در حال تست آپلود تصویر...")
    result = await test_wordpress_media()
    icon = "✅" if result.ok else "❌"
    details = f"\n<b>HTTP:</b> <code>{result.status_code or 'بدون پاسخ'}</code>\n<b>زمان:</b> <code>{result.elapsed_ms:.0f} ms</code>"
    if result.media_id:
        details += f"\n<b>Media ID:</b> <code>{result.media_id}</code>\n<b>حذف خودکار:</b> {'✅' if result.deleted else '❌'}"
    await query.edit_message_text(
        f"🖼️ <b>تست WordPress Media API</b>\n\n{icon} {result.message}{details}",
        reply_markup=_back_keyboard(), parse_mode="HTML",
    )



async def cb_product_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a temporary draft with the test image, then delete both."""
    query = update.callback_query
    user = update.effective_user
    if not user or not rbac.is_sudo(user.id):
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    if settings.woo_dry_run:
        await query.answer("🧪 حالت آزمایشی روشن است", show_alert=True)
        await query.edit_message_text(_dry_run_block(), reply_markup=_back_keyboard(), parse_mode="HTML")
        return
    await query.answer("در حال ساخت محصول تستی...")
    result = await test_product_with_image()
    icon = "✅" if result.ok else "❌"
    text = (
        f"📦 <b>تست ساخت محصول با تصویر</b>\n\n{icon} {result.message}\n"
        f"<b>Media HTTP:</b> <code>{result.media_status or 'بدون پاسخ'}</code>\n"
        f"<b>Product HTTP:</b> <code>{result.product_status or 'بدون پاسخ'}</code>\n"
        f"<b>Product ID:</b> <code>{result.product_id or '-'}</code>\n"
        f"<b>پاک‌سازی:</b> {'✅' if result.cleaned else '❌'}\n"
        f"<b>زمان:</b> <code>{result.elapsed_ms:.0f} ms</code>"
    )
    await query.edit_message_text(text, reply_markup=_back_keyboard(), parse_mode="HTML")


def register(app: Application) -> None:
    app.add_handler(CallbackQueryHandler(cb_ping, pattern=f"^{CB.PING}$"))
    app.add_handler(CallbackQueryHandler(cb_woocommerce_ping, pattern=f"^{CB.WOO_PING}$"))
    app.add_handler(CallbackQueryHandler(cb_media_ping, pattern=f"^{CB.WP_MEDIA_PING}$"))
    app.add_handler(CallbackQueryHandler(cb_product_ping, pattern=f"^{CB.WOO_PRODUCT_PING}$"))
    app.add_handler(CallbackQueryHandler(cb_shop_network_ping, pattern=f"^{CB.SHOP_NETWORK_PING}$"))
