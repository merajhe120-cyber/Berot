import asyncio
import logging
import os
import re
from decimal import Decimal, InvalidOperation
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, Update
from aiogram.fsm.storage.memory import MemoryStorage
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse

from config import load_settings
from db import Database
from keyboards import (
    main_menu,
    admin_menu,
    force_sub_keyboard,
    withdrawal_actions,
    back_button,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("sadek")

settings = load_settings()
db = Database(settings.database_url)

bot = Bot(
    settings.bot_token,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

dp = Dispatcher(storage=MemoryStorage())


# ---------------------------- helpers ----------------------------

def is_admin(uid: int) -> bool:
    return uid in settings.admin_ids


async def get_bot_username() -> str:
    me = await bot.get_me()
    return me.username


async def check_forced_subscription(user_id: int) -> bool:
    """
    التحقق من اشتراك المستخدم في جميع القنوات الإلزامية.
    يجب أن يكون البوت قادراً على الوصول إلى كل قناة والتحقق من العضوية.
    """
    channels = await db.list_channels()

    if not channels:
        return True

    for ch in channels:
        try:
            member = await bot.get_chat_member(
                ch["chat_id"],
                user_id,
            )

            if member.status in {
                ChatMemberStatus.LEFT,
                ChatMemberStatus.KICKED,
                ChatMemberStatus.RESTRICTED,
            }:
                return False

        except Exception as exc:
            log.warning(
                "Subscription check failed for channel %s: %s",
                ch["chat_id"],
                exc,
            )
            return False

    return True


async def is_banned(uid: int) -> bool:
    row = await db.get_user(uid)
    return bool(row and row["is_banned"])


async def guard(message: Message) -> bool:
    if await is_banned(message.from_user.id):
        await message.answer("🚫 تم حظرك من استخدام البوت.")
        return False

    return True


async def send_force_sub(message: Message):
    channels = await db.list_channels()

    await message.answer(
        "🔒 <b>الاشتراك مطلوب</b>\n\n"
        "اشترك بالقنوات التالية ثم اضغط «تحقّق من الاشتراك».",
        reply_markup=force_sub_keyboard(channels),
    )


async def ensure_access(message: Message) -> bool:
    if not await guard(message):
        return False

    if not await check_forced_subscription(message.from_user.id):
        await send_force_sub(message)
        return False

    await db.finalize_referral(message.from_user.id)
    return True


async def ensure_callback_access(callback: CallbackQuery) -> bool:
    if await is_banned(callback.from_user.id):
        await callback.answer("🚫 أنت محظور.", show_alert=True)
        return False

    if not await check_forced_subscription(callback.from_user.id):
        await callback.answer(
            "يجب الاشتراك بالقنوات أولاً.",
            show_alert=True,
        )

        await callback.message.edit_text(
            "🔒 <b>الاشتراك مطلوب</b>\n\n"
            "اشترك بالقنوات ثم اضغط التحقق.",
            reply_markup=force_sub_keyboard(await db.list_channels()),
        )
        return False

    return True


async def parse_referrer(args: str | None) -> int | None:
    if not args:
        return None

    match = re.fullmatch(r"ref_(\d+)", args.strip())
    return int(match.group(1)) if match else None


async def register_from_message(
    message: Message,
    args: str | None = None,
):
    ref = await parse_referrer(args)
    created, _ = await db.ensure_user(message.from_user, ref)
    return created


def money(value) -> str:
    return f"{Decimal(value):,.2f}".replace(",", "٬")


async def prompt_state(uid: int, state: str, data=None):
    await db.set_state(uid, state, data or {})


async def clear_state(uid: int):
    await db.set_state(uid, None, {})


# ---------------------------- user UI ----------------------------

@dp.message(CommandStart())
async def start(message: Message, command: CommandStart):
    await register_from_message(message, command.args)

    if not await guard(message):
        return

    if not await check_forced_subscription(message.from_user.id):
        await send_force_sub(message)
        return

    await db.finalize_referral(message.from_user.id)
    await clear_state(message.from_user.id)

    welcome = await db.get_setting(
        "welcome_message",
        "أهلاً بك ✨",
    )

    await message.answer(
        f"✨ <b>{settings.bot_name}</b>\n\n{welcome}",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data == "check_sub")
async def check_sub(callback: CallbackQuery):
    if await check_forced_subscription(callback.from_user.id):
        await db.finalize_referral(callback.from_user.id)

        await callback.answer("تم التحقق بنجاح ✅")
        await callback.message.delete()

        await callback.message.answer(
            f"✨ أهلاً بك في <b>{settings.bot_name}</b>",
            reply_markup=main_menu(),
        )
    else:
        await callback.answer(
            "لم يكتمل الاشتراك بعد.",
            show_alert=True,
        )


@dp.message(F.text == "👤 معلومات ملفي")
async def profile(message: Message):
    if not await ensure_access(message):
        return

    user = await db.get_user(message.from_user.id)

    await message.answer(
        f"""👤 <b>معلومات ملفك</b>

🆔 المعرف: <code>{user['id']}</code>
💰 الرصيد: <b>{money(user['balance'])}</b>
👥 الإحالات: <b>{user['referrals']}</b>"""
    )


@dp.message(F.text == "🔗 رابط إحالتي")
async def referral(message: Message):
    if not await ensure_access(message):
        return

    username = await get_bot_username()
    link = f"https://t.me/{username}?start=ref_{message.from_user.id}"

    reward = await db.get_setting("referral_reward", "0")

    await message.answer(
        f"🔗 <b>رابط إحالتك</b>\n\n"
        f"<code>{link}</code>\n\n"
        f"🎁 مكافأة كل إحالة جديدة: <b>{reward}</b>"
