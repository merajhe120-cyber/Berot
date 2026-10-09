import asyncio
import logging
import os
import re
import html
from decimal import Decimal, InvalidOperation
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
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


# =========================================================
# HELPERS
# =========================================================

def is_admin(uid: int) -> bool:
    return uid in settings.admin_ids


def safe_html(value) -> str:
    return html.escape(str(value or ""))


def money(value) -> str:
    return f"{Decimal(str(value or 0)):,.2f}".replace(",", "٬")


async def get_bot_username() -> str:
    me = await bot.get_me()
    return me.username or ""


async def parse_referrer(args: str | None) -> int | None:
    if not args:
        return None

    match = re.fullmatch(r"ref_(\d+)", args.strip())

    return int(match.group(1)) if match else None


async def register_from_message(
    message: Message,
    args: str | None = None,
):
    referrer_id = await parse_referrer(args)

    created, _ = await db.ensure_user(
        message.from_user,
        referrer_id,
    )

    return created


async def is_banned(uid: int) -> bool:
    user = await db.get_user(uid)

    return bool(user and user["is_banned"])


async def guard(message: Message) -> bool:
    if await is_banned(message.from_user.id):
        await message.answer(
            "🚫 تم حظرك من استخدام البوت."
        )
        return False

    return True


async def check_forced_subscription(user_id: int) -> bool:
    channels = await db.list_channels()

    if not channels:
        return True

    for channel in channels:
        try:
            member = await bot.get_chat_member(
                channel["chat_id"],
                user_id,
            )

            if member.status in {
                ChatMemberStatus.LEFT,
                ChatMemberStatus.KICKED,
                ChatMemberStatus.RESTRICTED,
            }:
                return False

        except Exception:
            log.exception(
                "Subscription check failed for channel %s",
                channel.get("chat_id"),
            )
            return False

    return True


async def send_force_sub(message: Message):
    channels = await db.list_channels()

    await message.answer(
        "🔒 <b>الاشتراك مطلوب</b>\n\n"
        "اشترك بالقنوات المطلوبة ثم اضغط "
        "«تحقّق من الاشتراك».",
        reply_markup=force_sub_keyboard(channels),
    )


async def prompt_state(
    uid: int,
    state: str,
    data=None,
):
    await db.set_state(uid, state, data or {})


async def clear_state(uid: int):
    await db.set_state(uid, None, {})


# =========================================================
# PHONE VERIFICATION
# =========================================================

def normalize_syrian_phone(phone: str) -> str | None:
    """
    يقبل الصيغ التالية:
    09XXXXXXXX
    9XXXXXXXX
    9639XXXXXXXX
    009639XXXXXXXX
    +9639XXXXXXXX

    ويحوّلها إلى:
    +9639XXXXXXXX
    """

    if not phone:
        return None

    value = phone.strip()

    # إزالة الفراغات والشرطات والأقواس
    value = re.sub(r"[\s\-().]", "", value)

    if value.startswith("00963"):
        value = "+" + value[2:]

    elif value.startswith("963"):
        value = "+" + value

    elif value.startswith("09"):
        value = "+963" + value[1:]

    elif re.fullmatch(r"9\d{8}", value):
        value = "+963" + value

    # الصيغة الدولية السورية المطلوبة
    if re.fullmatch(r"\+9639\d{8}", value):
        return value

    return None


async def is_phone_verified(user_id: int) -> bool:
    return await db.is_phone_verified(user_id)


def phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="📱 مشاركة رقم هاتفي",
                    request_contact=True,
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="اضغط لمشاركة رقم هاتفك",
    )


async def request_phone(message: Message):
    await clear_state(message.from_user.id)

    await message.answer(
        "📱 <b>التحقق من رقم الهاتف</b>\n\n"
        "للمتابعة واستخدام البوت، يجب مشاركة رقم هاتفك "
        "السوري المرتبط بحساب تيليجرام.\n\n"
        "اضغط على زر «مشاركة رقم هاتفي» بالأسفل.\n\n"
        "⚠️ يجب مشاركة جهة الاتصال الخاصة بك، "
        "وليس رقم شخص آخر.",
        reply_markup=phone_keyboard(),
    )


async def ensure_access(message: Message) -> bool:
    """
    حماية وظائف المستخدم:
    1. التأكد من عدم حظر المستخدم.
    2. التأكد من الاشتراك الإجباري.
    3. التأكد من التحقق من رقم الهاتف.
    """

    if not await guard(message):
        return False

    if not await check_forced_subscription(
        message.from_user.id
    ):
        await send_force_sub(message)
        return False

    if not await is_phone_verified(message.from_user.id):
        await request_phone(message)
        return False

    await db.finalize_referral(message.from_user.id)

    return True


async def ensure_callback_access(
    callback: CallbackQuery,
) -> bool:

    if await is_banned(callback.from_user.id):
        await callback.answer(
            "🚫 أنت محظور.",
            show_alert=True,
        )
        return False

    if not await check_forced_subscription(
        callback.from_user.id
    ):
        await callback.answer(
            "يجب الاشتراك بالقنوات أولاً.",
            show_alert=True,
        )

        if callback.message:
            await callback.message.answer(
                "🔒 يجب الاشتراك بالقنوات المطلوبة أولاً.",
                reply_markup=force_sub_keyboard(
                    await db.list_channels()
                ),
            )

        return False

    if not await is_phone_verified(callback.from_user.id):
        await callback.answer(
            "📱 يجب التحقق من رقم هاتفك أولاً.",
            show_alert=True,
        )

        if callback.message:
            await request_phone(callback.message)

        return False

    return True


# =========================================================
# START AND SUBSCRIPTION
# =========================================================

@dp.message(CommandStart())
async def start(
    message: Message,
    command: CommandStart,
):
    await register_from_message(
        message,
        command.args,
    )

    if not await guard(message):
        return

    if not await check_forced_subscription(
        message.from_user.id
    ):
        await send_force_sub(message)
        return

    await db.finalize_referral(
        message.from_user.id
    )

    await clear_state(message.from_user.id)

    if not await is_phone_verified(
        message.from_user.id
    ):
        await request_phone(message)
        return

    welcome = await db.get_setting(
        "welcome_message",
        "أهلاً بك ✨",
    )

    await message.answer(
        f"✨ <b>{safe_html(settings.bot_name)}</b>\n\n"
        f"{safe_html(welcome)}",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data == "check_sub")
async def check_sub(callback: CallbackQuery):
    if await is_banned(callback.from_user.id):
        await callback.answer(
            "🚫 أنت محظور.",
            show_alert=True,
        )
        return

    if not await check_forced_subscription(
        callback.from_user.id
    ):
        await callback.answer(
            "لم يكتمل الاشتراك بعد.",
            show_alert=True,
        )
        return

    await db.finalize_referral(
        callback.from_user.id
    )

    await callback.answer(
        "تم التحقق من الاشتراك ✅"
    )

    if callback.message:
        await callback.message.answer(
            "✅ تم التحقق من اشتراكك."
        )

        if not await is_phone_verified(
            callback.from_user.id
        ):
            await request_phone(callback.message)
            return

        await callback.message.answer(
            f"✨ أهلاً بك في <b>{safe_html(settings.bot_name)}</b>",
            reply_markup=main_menu(),
        )


# =========================================================
# ACCEPT SHARED CONTACT
# =========================================================

@dp.message(F.contact)
async def verify_phone_contact(message: Message):
    if not message.from_user:
        return

    if await is_banned(message.from_user.id):
        await message.answer(
            "🚫 تم حظرك من استخدام البوت."
        )
        return

    if not await check_forced_subscription(
        message.from_user.id
    ):
        await send_force_sub(message)
        return

    contact = message.contact

    # لا نقبل مشاركة رقم شخص آخر
    if contact.user_id != message.from_user.id:
        await message.answer(
            "❌ يجب مشاركة رقم هاتفك الشخصي من زر "
            "«مشاركة رقم هاتفي» داخل البوت.",
            reply_markup=phone_keyboard(),
        )
        return

    phone = normalize_syrian_phone(
        contact.phone_number
    )

    if not phone:
        await message.answer(
            "❌ الرقم المشارك ليس بصيغة رقم سوري صالح.\n\n"
            "يجب أن يكون الرقم السوري بصيغة مثل:\n"
            "<code>+9639XXXXXXXX</code>\n\n"
            "أعد المحاولة باستخدام رقمك السوري.",
            reply_markup=phone_keyboard(),
        )
        return

    try:
        await db.set_phone_verified(
            message.from_user.id,
            phone,
        )

    except Exception:
        log.exception(
            "Could not save verified phone for user %s",
            message.from_user.id,
        )

        await message.answer(
            "⚠️ حدث خطأ أثناء حفظ رقم الهاتف. "
            "حاول مرة أخرى لاحقاً.",
            reply_markup=phone_keyboard(),
        )
        return

    await clear_state(message.from_user.id)

    await db.finalize_referral(
        message.from_user.id
    )

    await message.answer(
        "✅ <b>تم التحقق من رقم هاتفك بنجاح!</b>\n\n"
        "أصبح بإمكانك استخدام البوت الآن.",
        reply_markup=ReplyKeyboardRemove(),
    )

    welcome = await db.get_setting(
        "welcome_message",
        "أهلاً بك ✨",
    )

    await message.answer(
        f"✨ <b>{safe_html(settings.bot_name)}</b>\n\n"
        f"{safe_html(welcome)}",
        reply_markup=main_menu(),
    )


# =========================================================
# MAIN MENU BUTTONS
# =========================================================

@dp.message(
    F.text.in_([
        "👤 الملف الشخصي",
        "👤 معلومات ملفي",
    ])
)
async def profile(message: Message):
    if not await ensure_access(message):
        return

    user = await db.get_user(
        message.from_user.id
    )

    if not user:
        await message.answer(
            "⚠️ لم يتم العثور على ملفك. أرسل /start."
        )
        return

    await message.answer(
        "👤 <b>معلومات ملفك</b>\n\n"
        f"🆔 المعرف: <code>{user['id']}</code>\n"
        f"💰 الرصيد: <b>{money(user['balance'])}</b>\n"
        f"👥 الإحالات: <b>{user['referrals']}</b>"
    )


@dp.message(
    F.text.in_([
        "👥 الإحالات",
        "🔗 رابط إحالتي",
    ])
)
async def referral(message: Message):
    if not await ensure_access(message):
        return

    username = await get_bot_username()

    link = (
        f"https://t.me/{username}"
        f"?start=ref_{message.from_user.id}"
    )

    reward = await db.get_setting(
        "referral_reward",
        "0",
    )

    await message.answer(
        "🔗 <b>رابط إحالتك</b>\n\n"
        f"<code>{link}</code>\n\n"
        f"🎁 مكافأة كل إحالة جديدة: "
        f"<b>{safe_html(reward)}</b>"
    )


@dp.message(
    F.text.in_([
        "💸 سحب الأرباح",
        "💸 سحب رصيد",
    ])
)
async def withdrawal_start(message: Message):
    if not await ensure_access(message):
        return

    info = await db.get_setting(
        "withdrawal_info",
        "",
    )

    await prompt_state(
        message.from_user.id,
        "withdraw_amount",
    )

    await message.answer(
        "💸 <b>السحب</b>\n\n"
        f"{safe_html(info)}\n\n"
        "أرسل الآن المبلغ الذي تريد سحبه:"
    )


@dp.message(
    F.text.in_([
        "🎁 كود الهدية",
        "🎁 كود هدية",
    ])
)
async def gift_start(message: Message):
    if not await ensure_access(message):
        return

    await prompt_state(
        message.from_user.id,
        "gift_code",
    )

    await message.answer(
        "🎁 أرسل كود الهدية الآن:"
    )


@dp.message(
    F.text.in_([
        "📞 التواصل",
        "📨 تواصل معنا",
    ])
)
async def contact_start(message: Message):
    if not await ensure_access(message):
        return

    await prompt_state(
        message.from_user.id,
        "contact",
    )

    await message.answer(
        "📨 أرسل رسالتك، وسيتم تحويلها للإدارة."
    )


@dp.message(
    F.text.in_([
        "🎯 العروض",
        "📢 اشترك بالعروض",
    ])
)
async def offers(message: Message):
    if not await ensure_access(message):
        return

    rows = await db.list_offers()

    if not rows:
        await message.answer(
            "📢 لا توجد عروض حالياً."
        )
        return

    for row in rows:
        text = (
            f"🛍 <b>{safe_html(row['title'])}</b>\n\n"
            f"{safe_html(row['description'])}"
        )

        if row.get("url"):
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔗 فتح العرض",
                            url=row["url"],
                        )
                    ]
                ]
            )

            await message.answer(
                text,
                reply_markup=keyboard,
            )

        else:
            await message.answer(text)


@dp.message(
    F.text.in_([
        "🤖 الذكاء الاصطناعي",
        "🤖 اسأل الذكاء الاصطناعي",
    ])
)
async def ai_start(message: Message):
    if not await ensure_access(message):
        return

    if not settings.openai_api_key:
        await message.answer(
            "🤖 الذكاء الاصطناعي غير مفعّل حالياً.\n"
            "أضف OPENAI_API_KEY في إعدادات Render."
        )
        return

    await prompt_state(
        message.from_user.id,
        "ai",
    )

    await message.answer(
        "🤖 اكتب سؤالك الآن."
    )


# =========================================================
# USER AND ADMIN STATE MESSAGES
# =========================================================

async def handle_admin_state(
    message: Message,
    state: str,
    text: str,
):
    uid = message.from_user.id

    if state in {
        "adm_add_balance",
        "adm_deduct_balance",
    }:
        await clear_state(uid)

        try:
            uid_text, amount_text = [
                item.strip()
                for item in text.split("|", 1)
            ]

            target_uid = int(uid_text)
            amount = Decimal(
                amount_text.replace(",", ".")
            )

            if amount <= 0:
                raise ValueError

            delta = (
                amount
                if state == "adm_add_balance"
                else -amount
            )

            new_balance = await db.change_balance(
                target_uid,
                delta,
                (
                    "admin_add_balance"
                    if delta > 0
                    else "admin_deduct_balance"
                ),
                {"amount": str(amount)},
            )

            if new_balance is None:
                await message.answer(
                    "❌ المستخدم غير موجود.",
                    reply_markup=admin_menu(),
                )
                return

            await message.answer(
                "✅ تم تعديل الرصيد.\n"
                f"الرصيد الجديد: <b>{money(new_balance)}</b>",
                reply_markup=admin_menu(),
            )

            try:
                action_text = (
                    "إضافة"
                    if delta > 0
                    else "خصم"
                )

                await bot.send_message(
                    target_uid,
                    f"💰 تم {action_text} "
                    f"<b>{money(amount)}</b> من رصيدك.\n"
                    f"الرصيد الحالي: <b>{money(new_balance)}</b>",
                )
            except Exception:
                pass

        except Exception:
            await message.answer(
                "❌ الصيغة الصحيحة:\n"
                "<code>USER_ID|AMOUNT</code>",
                reply_markup=admin_menu(),
            )

        return

    if state == "adm_gift":
        await clear_state(uid)

        try:
            parts = [
                item.strip()
                for item in text.split("|")
            ]

            if len(parts) < 3:
                raise ValueError

            code = parts[0]
            amount = Decimal(parts[1])
            uses = int(parts[2])
            expires = None

            if amount <= 0 or uses <= 0:
                raise ValueError

            if len(parts) >= 4 and parts[3]:
                from datetime import datetime, timezone

                expires = datetime.strptime(
                    parts[3],
                    "%Y-%m-%d",
                ).replace(tzinfo=timezone.utc)

            await db.create_gift(
                code.upper(),
                amount,
                uses,
                expires,
            )

            await message.answer(
                "✅ تم إنشاء كود الهدية.\n"
                f"الكود: <code>{safe_html(code.upper())}</code>\n"
                f"القيمة: <b>{money(amount)}</b>\n"
                f"عدد الاستخدامات: <b>{uses}</b>",
                reply_markup=admin_menu(),
            )

        except Exception:
            await message.answer(
                "❌ الصيغة:\n"
                "<code>CODE|AMOUNT|MAX_USES|YYYY-MM-DD</code>\n"
                "التاريخ اختياري.",
                reply_markup=admin_menu(),
            )

        return

    if state in {
        "adm_logs",
        "adm_ban",
        "adm_unban",
    }:
        await clear_state(uid)

        try:
            target_uid = int(text)
        except ValueError:
            await message.answer(
                "❌ رقم المستخدم غير صحيح.",
                reply_markup=admin_menu(),
            )
            return

        if state == "adm_logs":
            user = await db.get_user(target_uid)
            logs = await db.user_logs(target_uid)

            if not user:
                await message.answer(
                    "المستخدم غير موجود.",
                    reply_markup=admin_menu(),
                )
                return

            output = (
                f"🔎 <b>المستخدم {target_uid}</b>\n"
                f"💰 الرصيد: {money(user['balance'])}\n"
                f"👥 الإحالات: {user['referrals']}\n\n"
            )

            for row in logs[:15]:
                output += (
                    f"• {row['created_at']:%Y-%m-%d %H:%M}"
                    f" — {safe_html(row['action'])}\n"
                )

            await message.answer(
                output,
                reply_markup=admin_menu(),
            )

        else:
            await db.set_banned(
                target_uid,
                state == "adm_ban",
            )

            await message.answer(
                "✅ تم "
                + (
                    "حظر المستخدم."
                    if state == "adm_ban"
                    else "إلغاء حظر المستخدم."
                ),
                reply_markup=admin_menu(),
            )

        return

    if state == "adm_add_channel":
        await clear_state(uid)

        try:
            chat_id, title, url = [
                item.strip()
                for item in text.split("|", 2)
            ]

            await db.add_channel(
                chat_id,
                title,
                url,
            )

            await message.answer(
                "✅ تمت إضافة القناة.\n"
                "تأكد أن البوت يستطيع الوصول إلى القناة "
                "للتحقق من الاشتراك.",
                reply_markup=admin_menu(),
            )

        except Exception:
            await message.answer(
                "❌ الصيغة:\n"
                "<code>CHAT_ID|TITLE|JOIN_URL</code>",
                reply_markup=admin_menu(),
            )

        return

    if state == "adm_add_offer":
        await clear_state(uid)

        try:
            parts = text.split("|", 2)

            title = parts[0].strip()
            description = parts[1].strip()
            url = (
                parts[2].strip()
                if len(parts) > 2
                else ""
            )

            if not title or not description:
                raise ValueError

            await db.add_offer(
                title,
                description,
                url,
            )

            await message.answer(
                "✅ تمت إضافة العرض.",
                reply_markup=admin_menu(),
            )

        except Exception:
            await message.answer(
                "❌ الصيغة:\n"
                "<code>TITLE|DESCRIPTION|URL</code>",
                reply_markup=admin_menu(),
            )

        return

    if state == "adm_broadcast":
        await clear_state(uid)

        rows = await db.all_user_ids()
        sent = 0
        failed = 0

        status_message = await message.answer(
            f"📢 بدء الإذاعة إلى {len(rows)} مستخدم..."
        )

        for row in rows:
            try:
                if await is_banned(row["id"]):
                    continue

                await bot.send_message(
                    row["id"],
                    text,
                )

                sent += 1

            except Exception:
                failed += 1

            await asyncio.sleep(0.04)

        await status_message.edit_text(
            "📢 <b>انتهت الإذاعة</b>\n\n"
            f"✅ تم الإرسال: {sent}\n"
            f"❌ فشل: {failed}",
            reply_markup=admin_menu(),
        )

        return

    settings_map = {
        "adm_set_referral": "referral_reward",
        "adm_set_minwd": "min_withdrawal",
        "adm_set_welcome": "welcome_message",
        "adm_set_contact": "admin_contact",
        "adm_set_wdinfo": "withdrawal_info",
    }

    if state in settings_map:
        await clear_state(uid)

        key = settings_map[state]

        if key in {
            "referral_reward",
            "min_withdrawal",
        }:
            try:
                value = Decimal(
                    text.replace(",", ".")
                )

                if value < 0:
                    raise ValueError

            except Exception:
                await message.answer(
                    "❌ القيمة غير صحيحة.",
                    reply_markup=admin_menu(),
                )
                return

        await db.set_setting(key, text)

        await message.answer(
            "✅ تم حفظ الإعداد.",
            reply_markup=admin_menu(),
        )


@dp.message()
async def all_text(message: Message):
    if not message.from_user:
        return

    # السماح للأوامر بمعالجة رسائلها بشكل منفصل
    if message.text and message.text.startswith("/"):
        return

    if not await guard(message):
        return

    if not await check_forced_subscription(
        message.from_user.id
    ):
        await send_force_sub(message)
        return

    # لا نسمح باستخدام وظائف البوت قبل التحقق من الهاتف.
    if not await is_phone_verified(
        message.from_user.id
    ):
        await request_phone(message)
        return

    await db.finalize_referral(
        message.from_user.id
    )

    state, data = await db.get_state(
        message.from_user.id
    )

    text = (message.text or "").strip()

    if (
        is_admin(message.from_user.id)
        and state
        and state.startswith("adm_")
    ):
        await handle_admin_state(
            message,
            state,
            text,
        )
        return

    if state == "withdraw_amount":
        await clear_state(message.from_user.id)

        try:
            amount = Decimal(
                text.replace(",", ".")
            )
        except InvalidOperation:
            await message.answer(
                "❌ أرسل مبلغاً رقمياً صحيحاً."
            )
            return

        if amount <= 0:
            await message.answer(
                "❌ يجب أن يكون المبلغ أكبر من صفر."
            )
            return

        min_withdrawal = Decimal(
            await db.get_setting(
                "min_withdrawal",
                "5000",
            )
        )

        if amount < min_withdrawal:
            await message.answer(
                "❌ الحد الأدنى للسحب هو "
                f"<b>{money(min_withdrawal)}</b>."
            )
            return

        result = await db.create_withdrawal(
            message.from_user.id,
            amount,
        )

        if result == "pending":
            await message.answer(
                "⏳ لديك طلب سحب قيد المراجعة بالفعل."
            )
            return

        if result is None:
            await message.answer(
                "❌ رصيدك غير كافٍ لهذا المبلغ."
            )
            return

        user = await db.get_user(
            message.from_user.id
        )

        await message.answer(
            "✅ تم إرسال طلب السحب.\n"
            f"رقم الطلب: <code>#{result}</code>\n"
            f"المبلغ: <b>{money(amount)}</b>\n\n"
            "سيتم مراجعته من الإدارة."
        )

        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(
                    admin_id,
                    "💸 <b>طلب سحب جديد</b>\n\n"
                    f"رقم الطلب: <code>#{result}</code>\n"
                    f"المستخدم: <code>{user['id']}</code>\n"
                    f"المبلغ: <b>{money(amount)}</b>\n"
                    f"الإحالات: <b>{user['referrals']}</b>",
                    reply_markup=withdrawal_actions(
                        result
                    ),
                )
            except Exception:
                log.exception(
                    "Could not notify admin about withdrawal"
                )

        return

    if state == "gift_code":
        await clear_state(message.from_user.id)

        ok, result = await db.redeem_gift(
            message.from_user.id,
            text,
        )

        if ok:
            await message.answer(
                "🎉 تمت إضافة "
                f"<b>{money(result)}</b> إلى رصيدك."
            )
        else:
            await message.answer(
                f"❌ {safe_html(result)}"
            )

        return

    if state == "contact":
        await clear_state(message.from_user.id)

        user = await db.get_user(
            message.from_user.id
        )

        sent = False

        contact_target = await db.get_setting(
            "admin_contact",
            "",
        )

        targets = settings.admin_ids

        if contact_target.strip().lstrip("-").isdigit():
            targets = {
                int(contact_target.strip())
            }

        for admin_id in targets:
            try:
                await bot.send_message(
                    admin_id,
                    "📨 <b>رسالة من مستخدم</b>\n\n"
                    f"المعرف: <code>{user['id']}</code>\n"
                    f"المستخدم: @{safe_html(user['username'] or '-')}\n\n"
                    f"{safe_html(text)}",
                )

                sent = True

            except Exception:
                log.exception(
                    "Could not forward user message"
                )

        await message.answer(
            "✅ تم إرسال رسالتك للإدارة."
            if sent
            else "⚠️ تعذر إرسال الرسالة حالياً."
        )

        return

    if state == "ai":
        await clear_state(message.from_user.id)

        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(
                api_key=settings.openai_api_key
            )

            response = await client.responses.create(
                model=settings.openai_model,
                instructions=(
                    f"أنت مساعد محترم داخل {settings.bot_name}. "
                    "أجب بالعربية عند الحاجة وباختصار مفيد."
                ),
                input=text,
            )

            answer = (
                response.output_text.strip()
                or "لم أستطع توليد إجابة."
            )

            await message.answer(
                safe_html(answer)
            )

        except Exception:
            log.exception("OpenAI request failed")

            await message.answer(
                "⚠️ حدث خطأ مؤقت في خدمة الذكاء الاصطناعي."
            )

        return

    if text:
        await message.answer(
            "اختر أحد الأزرار من القائمة الرئيسية.",
            reply_markup=main_menu(),
        )


# =========================================================
# ADMIN COMMAND
# =========================================================

@dp.message(Command("admin"))
async def admin_command(message: Message):
    if not is_admin(message.from_user.id):
        return

    await clear_state(
        message.from_user.id
    )

    await message.answer(
        f"🛠 <b>لوحة إدارة {safe_html(settings.bot_name)}</b>",
        reply_markup=admin_menu(),
    )


@dp.message(Command("myid"))
async def myid_command(message: Message):
    await message.answer(
        f"🆔 معرفك هو: <code>{message.from_user.id}</code>"
    )


@dp.message(Command("menu"))
async def menu_command(message: Message):
    if not await ensure_access(message):
        return

    await message.answer(
        "🏠 القائمة الرئيسية",
        reply_markup=main_menu(),
    )


# =========================================================
# ADMIN CALLBACKS
# =========================================================

@dp.callback_query(F.data.startswith("adm:"))
async def admin_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "غير مصرح.",
            show_alert=True,
        )
        return

    await callback.answer()

    action = callback.data.split(":", 1)[1]
    uid = callback.from_user.id

    if action == "home":
        await clear_state(uid)

        await callback.message.edit_text(
            f"🛠 <b>لوحة إدارة {safe_html(settings.bot_name)}</b>",
            reply_markup=admin_menu(),
        )

    elif action == "add_balance":
        await prompt_state(
            uid,
            "adm_add_balance",
        )

        await callback.message.edit_text(
            "➕ أرسل:\n"
            "<code>USER_ID|AMOUNT</code>",
            reply_markup=back_button("adm:home"),
        )

    elif action == "deduct_balance":
        await prompt_state(
            uid,
            "adm_deduct_balance",
        )

        await callback.message.edit_text(
            "➖ أرسل:\n"
            "<code>USER_ID|AMOUNT</code>",
            reply_markup=back_button("adm:home"),
        )

    elif action == "create_gift":
        await prompt_state(
            uid,
            "adm_gift",
        )

        await callback.message.edit_text(
            "🎁 أرسل:\n"
            "<code>CODE|AMOUNT|MAX_USES|YYYY-MM-DD</code>\n"
            "التاريخ اختياري.",
            reply_markup=back_button("adm:home"),
        )

    elif action == "user_logs":
        await prompt_state(
            uid,
            "adm_logs",
        )

        await callback.message.edit_text(
            "🔎 أرسل ID المستخدم:",
            reply_markup=back_button("adm:home"),
        )

    elif action == "stats":
        stats = await db.stats()

        await callback.message.edit_text(
            "📊 <b>الإحصائيات</b>\n\n"
            f"👥 المشتركون: <b>{stats['users']}</b>\n"
            f"💰 مجموع الأرصدة: <b>{money(stats['balance'])}</b>\n"
            f"👥 مجموع الإحالات: <b>{stats['referrals']}</b>\n"
            f"💸 طلبات السحب المعلقة: "
            f"<b>{stats['pending_withdrawals']}</b>",
            reply_markup=back_button("adm:home"),
        )

    elif action == "balances":
        stats = await db.stats()

        await callback.message.edit_text(
            "💰 <b>معلومات الأرصدة</b>\n\n"
            f"إجمالي الأرصدة: <b>{money(stats['balance'])}</b>",
            reply_markup=back_button("adm:home"),
        )

    elif action in {"ban", "unban"}:
        await prompt_state(
            uid,
            "adm_ban" if action == "ban" else "adm_unban",
        )

        await callback.message.edit_text(
            "🚫 أرسل ID المستخدم:",
            reply_markup=back_button("adm:home"),
        )

    elif action == "withdrawals":
        rows = await db.list_withdrawals()

        if not rows:
            await callback.message.edit_text(
                "💸 لا توجد طلبات سحب معلقة.",
                reply_markup=back_button("adm:home"),
            )
            return

        for row in rows:
            await callback.message.answer(
                "💸 <b>طلب سحب</b>\n\n"
                f"رقم الطلب: <code>{row['id']}</code>\n"
                f"المستخدم: <code>{row['user_id']}</code>\n"
                f"المبلغ: <b>{money(row['amount'])}</b>\n"
                f"الإحالات: <b>{row['referrals']}</b>\n"
                f"اسم المستخدم: @{safe_html(row['username'] or '-')}",
                reply_markup=withdrawal_actions(
                    row["id"]
                ),
            )

    elif action == "add_channel":
        await prompt_state(
            uid,
            "adm_add_channel",
        )

        await callback.message.edit_text(
            "📣 أرسل:\n"
            "<code>CHAT_ID|TITLE|JOIN_URL</code>\n"
            "مثال:\n"
            "<code>@mychannel|قناتي|https://t.me/mychannel</code>",
            reply_markup=back_button("adm:home"),
        )

    elif action == "del_channel":
        rows = await db.list_channels()

        if not rows:
            await callback.message.edit_text(
                "لا توجد قنوات.",
                reply_markup=back_button("adm:home"),
            )
            return

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"🗑 {row['title']}",
                        callback_data=f"adm_delch:{row['id']}",
                    )
                ]
                for row in rows
            ]
            + [
                [
                    InlineKeyboardButton(
                        text="↩️ رجوع",
                        callback_data="adm:home",
                    )
                ]
            ]
        )

        await callback.message.edit_text(
            "اختر القناة للحذف:",
            reply_markup=keyboard,
        )

    elif action == "add_offer":
        await prompt_state(
            uid,
            "adm_add_offer",
        )

        await callback.message.edit_text(
            "🛍 أرسل:\n"
            "<code>TITLE|DESCRIPTION|URL</code>\n"
            "الرابط اختياري.",
            reply_markup=back_button("adm:home"),
        )

    elif action == "del_offer":
        rows = await db.list_offers()

        if not rows:
            await callback.message.edit_text(
                "لا توجد عروض.",
                reply_markup=back_button("adm:home"),
            )
            return

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"🗑 {row['title']}",
                        callback_data=f"adm_deloff:{row['id']}",
                    )
                ]
                for row in rows
            ]
            + [
                [
                    InlineKeyboardButton(
                        text="↩️ رجوع",
                        callback_data="adm:home",
                    )
                ]
            ]
        )

        await callback.message.edit_text(
            "اختر العرض للحذف:",
            reply_markup=keyboard,
        )

    elif action == "settings":
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🎁 مكافأة الإحالة",
                        callback_data="admset:referral",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="💸 الحد الأدنى للسحب",
                        callback_data="admset:minwd",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📝 رسالة الترحيب",
                        callback_data="admset:welcome",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📨 وسيلة التواصل",
                        callback_data="admset:contact",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="ℹ️ معلومات السحب",
                        callback_data="admset:wdinfo",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="↩️ رجوع",
                        callback_data="adm:home",
                    )
                ],
            ]
        )

        await callback.message.edit_text(
            "⚙️ <b>إعدادات البوت</b>\n\n"
            "اختر الإعداد الذي تريد تغييره:",
            reply_markup=keyboard,
        )

    elif action == "broadcast":
        await prompt_state(
            uid,
            "adm_broadcast",
        )

        await callback.message.edit_text(
            "📢 أرسل الرسالة التي تريد إذاعتها "
            "للمستخدمين.",
            reply_markup=back_button("adm:home"),
        )


@dp.callback_query(F.data.startswith("adm_delch:"))
async def delete_channel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "غير مصرح.",
            show_alert=True,
        )
        return

    try:
        channel_id = int(
            callback.data.split(":")[1]
        )

        await db.delete_channel(channel_id)

        await callback.answer("تم الحذف")

        await callback.message.edit_text(
            "✅ تم حذف القناة.",
            reply_markup=back_button("adm:home"),
        )

    except Exception:
        log.exception("Failed to delete channel")

        await callback.answer(
            "تعذر حذف القناة.",
            show_alert=True,
        )


@dp.callback_query(F.data.startswith("adm_deloff:"))
async def delete_offer(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "غير مصرح.",
            show_alert=True,
        )
        return

    try:
        offer_id = int(
            callback.data.split(":")[1]
        )

        await db.delete_offer(offer_id)

        await callback.answer("تم الحذف")

        await callback.message.edit_text(
            "✅ تم حذف العرض.",
            reply_markup=back_button("adm:home"),
        )

    except Exception:
        log.exception("Failed to delete offer")

        await callback.answer(
            "تعذر حذف العرض.",
            show_alert=True,
        )


@dp.callback_query(F.data.startswith("admset:"))
async def admin_settings_callback(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "غير مصرح.",
            show_alert=True,
        )
        return

    key = callback.data.split(":")[1]

    mapping = {
        "referral": "adm_set_referral",
        "minwd": "adm_set_minwd",
        "welcome": "adm_set_welcome",
        "contact": "adm_set_contact",
        "wdinfo": "adm_set_wdinfo",
    }

    prompts = {
        "referral": "🎁 أرسل قيمة مكافأة الإحالة:",
        "minwd": "💸 أرسل الحد الأدنى للسحب:",
        "welcome": "📝 أرسل رسالة الترحيب الجديدة:",
        "contact": "📨 أرسل وسيلة التواصل:",
        "wdinfo": "ℹ️ أرسل معلومات السحب:",
    }

    if key not in mapping:
        await callback.answer(
            "إعداد غير معروف.",
            show_alert=True,
        )
        return

    await prompt_state(
        callback.from_user.id,
        mapping[key],
    )

    await callback.answer()

    await callback.message.edit_text(
        prompts[key],
        reply_markup=back_button("adm:home"),
    )


# =========================================================
# WITHDRAWAL APPROVAL AND REJECTION
# =========================================================

@dp.callback_query(F.data.startswith("wd:"))
async def withdrawal_callback(
    callback: CallbackQuery,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "غير مصرح.",
            show_alert=True,
        )
        return

    try:
        _, action, withdrawal_id_text = (
            callback.data.split(":")
        )

        withdrawal_id = int(
            withdrawal_id_text
        )

        if action not in {"approve", "reject"}:
            raise ValueError

        result = await db.process_withdrawal(
            withdrawal_id,
            action == "approve",
        )

        if not result:
            await callback.answer(
                "الطلب غير موجود أو تمت معالجته.",
                show_alert=True,
            )
            return

        status, user_id, amount = result

        await callback.answer(
            "تمت معالجة الطلب."
        )

        await callback.message.edit_reply_markup(
            reply_markup=None
        )

        if status == "approved":
            user_message = (
                f"✅ تم قبول طلب السحب "
                f"<b>#{withdrawal_id}</b>\n"
                f"المبلغ: <b>{money(amount)}</b>"
            )
        else:
            user_message = (
                f"❌ تم رفض طلب السحب "
                f"<b>#{withdrawal_id}</b>.\n"
                "لم يتم خصم الرصيد."
            )

        try:
            await bot.send_message(
                user_id,
                user_message,
            )
        except Exception:
            log.exception(
                "Could not notify user about withdrawal"
            )

    except Exception:
        log.exception(
            "Withdrawal callback failed"
        )

        await callback.answer(
            "تعذرت معالجة الطلب.",
            show_alert=True,
        )


# =========================================================
# FASTAPI / RENDER WEBHOOK
# =========================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()

    if settings.webhook_url:
        await bot.set_webhook(
            url=(
                f"{settings.webhook_url.rstrip('/')}"
                "/telegram/webhook"
            ),
            secret_token=settings.webhook_secret,
            allowed_updates=dp.resolve_used_update_types(),
        )

        log.info("Webhook configured")

    else:
        log.warning(
            "WEBHOOK_URL is empty; webhook is not configured."
        )

    try:
        yield

    finally:
        await db.close()
        await bot.session.close()


app = FastAPI(
    title="Sadek Telegram Bot",
    lifespan=lifespan,
)


@app.get(
    "/health",
    response_class=PlainTextResponse,
)
async def health():
    try:
        await db.pool.fetchval("SELECT 1")
        return "ok"

    except Exception:
        raise HTTPException(
            status_code=503,
            detail="database unavailable",
        )


@app.get(
    "/",
    response_class=PlainTextResponse,
)
async def root():
    return "Sadek bot is running."


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if settings.webhook_secret:
        token = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token"
        )

        if token != settings.webhook_secret:
            raise HTTPException(
                status_code=403,
                detail="forbidden",
            )

    data = await request.json()

    update = Update.model_validate(
        data,
        context={"bot": bot},
    )

    await dp.feed_update(
        bot,
        update,
    )

    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
    )
