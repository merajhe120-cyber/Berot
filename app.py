
import asyncio
import html
import json
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

        except Exception:
            log.exception(
                "Subscription check failed for channel %s",
                ch["chat_id"],
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


def normalize_state_data(data):
    """
    asyncpg قد يعيد JSONB كنص JSON حسب إعدادات الاتصال.
    نحوله إلى قاموس قبل استخدام بيانات الجلسة.
    """
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return {}

    return data if isinstance(data, dict) else {}


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
    )


@dp.message(F.text == "💸 سحب رصيد")
async def withdrawal_start(message: Message):
    if not await ensure_access(message):
        return

    info = await db.get_setting("withdrawal_info")
    await prompt_state(message.from_user.id, "withdraw_amount")

    await message.answer(
        f"💸 <b>السحب</b>\n\n{info}\n\n"
        "أرسل الآن المبلغ الذي تريد سحبه:"
    )


@dp.message(F.text == "🎁 كود هدية")
async def gift_start(message: Message):
    if not await ensure_access(message):
        return

    await prompt_state(message.from_user.id, "gift_code")
    await message.answer("🎁 أرسل كود الهدية الآن:")


@dp.message(F.text == "📨 تواصل معنا")
async def contact_start(message: Message):
    if not await ensure_access(message):
        return

    await prompt_state(message.from_user.id, "contact")
    await message.answer("📨 أرسل رسالتك، وسيتم تحويلها للإدارة.")


@dp.message(F.text == "📢 اشترك بالعروض")
async def offers(message: Message):
    if not await ensure_access(message):
        return

    rows = await db.list_offers()

    if not rows:
        await message.answer("📢 لا توجد عروض حالياً.")
        return

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    for row in rows:
        text = f'🛍 <b>{row["title"]}</b>\n\n{row["description"]}'

        if row["url"]:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(
                        text="🔗 فتح العرض",
                        url=row["url"],
                    )
                ]]
            )
            await message.answer(text, reply_markup=keyboard)
        else:
            await message.answer(text)


@dp.message(F.text == "🤖 اسأل الذكاء الاصطناعي")
async def ai_start(message: Message):
    if not await ensure_access(message):
        return

    if not settings.openai_api_key:
        await message.answer(
            "🤖 الذكاء الاصطناعي غير مفعّل حالياً. "
            "أضف OPENAI_API_KEY في Render."
        )
        return

    await prompt_state(message.from_user.id, "ai")
    await message.answer(
        "🤖 اكتب سؤالك الآن، وسأجيبك بالذكاء الاصطناعي."
    )


# ---------------------------- command and menu handlers ----------------------------

@dp.message(Command("admin"))
async def admin_command(message: Message):
    uid = message.from_user.id

    if not is_admin(uid):
        await message.answer(
            "⛔ غير مصرح لك بفتح لوحة الأدمن.\n"
            f"🆔 رقم حسابك: <code>{uid}</code>\n"
            "تأكد من إضافة رقمك في ADMIN_IDS داخل Render."
        )
        return

    await clear_state(uid)
    await message.answer(
        f"🛠 <b>لوحة إدارة {settings.bot_name}</b>",
        reply_markup=admin_menu(),
    )


@dp.message(Command("myid"))
async def my_id_command(message: Message):
    uid = message.from_user.id
    status = "أدمن ✅" if is_admin(uid) else "ليس ضمن ADMIN_IDS"
    await message.answer(
        f"🆔 رقم حسابك: <code>{uid}</code>\n"
        f"الحالة: {status}"
    )


@dp.message(Command("menu"))
async def show_menu_command(message: Message):
    if not await ensure_access(message):
        return

    await clear_state(message.from_user.id)
    await message.answer(
        "📋 القائمة الرئيسية",
        reply_markup=main_menu(),
    )


@dp.message(F.text == "↩️ القائمة الرئيسية")
async def back_to_main_menu(message: Message):
    if not await ensure_access(message):
        return

    await clear_state(message.from_user.id)
    await message.answer(
        "📋 القائمة الرئيسية",
        reply_markup=main_menu(),
    )


@dp.message(F.text == "🙈 إخفاء القائمة")
async def hide_menu(message: Message):
    from aiogram.types import ReplyKeyboardRemove
    await message.answer(
        "تم إخفاء القائمة. لإظهارها مجدداً أرسل /menu",
        reply_markup=ReplyKeyboardRemove(),
    )


# ---------------------------- state messages ----------------------------

@dp.message()
async def all_text(message: Message):
    if not await guard(message):
        return

    state, data = await db.get_state(message.from_user.id)

    # إصلاح مهم: تحويل state_data إلى قاموس إن رجعت كنص JSON.
    data = normalize_state_data(data)

    text = (message.text or "").strip()

    if (
        is_admin(message.from_user.id)
        and state
        and state.startswith("adm_")
    ):
        return await handle_admin_state(message, state, text)

    if not await check_forced_subscription(message.from_user.id):
        await send_force_sub(message)
        return

    await db.finalize_referral(message.from_user.id)

    # الخطوة الأولى: استقبال مبلغ السحب.
    if state == "withdraw_amount":
        try:
            amount = Decimal(text.replace(",", "."))
            if not amount.is_finite() or amount <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            await clear_state(message.from_user.id)
            await message.answer(
                "❌ أرسل مبلغاً رقمياً صحيحاً أكبر من صفر."
            )
            return

        try:
            min_withdrawal = Decimal(
                await db.get_setting("min_withdrawal", "5000")
            )
        except (InvalidOperation, ValueError):
            min_withdrawal = Decimal("5000")

        if amount < min_withdrawal:
            await clear_state(message.from_user.id)
            await message.answer(
                f"❌ الحد الأدنى للسحب هو "
                f"<b>{money(min_withdrawal)}</b>."
            )
            return

        user = await db.get_user(message.from_user.id)
        if not user or Decimal(user["balance"]) < amount:
            await clear_state(message.from_user.id)
            await message.answer("❌ رصيدك غير كافٍ لهذا المبلغ.")
            return

        await prompt_state(
            message.from_user.id,
            "withdraw_tel_cash",
            {"amount": str(amount)},
        )

        await message.answer(
            "📱 <b>رقم حساب Tel Cash</b>\n\n"
            "أرسل رقم حساب Tel Cash الذي تريد استلام مبلغ السحب عليه.\n"
            "تأكد من صحة الرقم قبل إرساله."
        )
        return

    # الخطوة الثانية: استقبال رقم Tel Cash.
    if state == "withdraw_tel_cash":
        raw_number = text

        if not re.fullmatch(r"\+?[0-9][0-9 \-]{5,18}[0-9]", raw_number):
            await message.answer(
                "❌ رقم الحساب غير صحيح.\n"
                "أرسل رقم Tel Cash بالأرقام، ويمكنك استخدام + أو مسافات أو شرطات."
            )
            return

        tel_cash_number = re.sub(r"[ \-]", "", raw_number)

        # محاولة استعادة البيانات من قاعدة البيانات إذا كانت فارغة.
        saved_data = data or {}
        if not saved_data.get("amount"):
            try:
                _, saved_data = await db.get_state(message.from_user.id)
                saved_data = normalize_state_data(saved_data)
            except Exception:
                log.exception("Failed to reload withdrawal state")

        try:
            amount_value = (saved_data or {}).get("amount")
            if amount_value is None:
                raise ValueError("withdrawal amount is missing")

            amount = Decimal(str(amount_value))
            if not amount.is_finite() or amount <= 0:
                raise ValueError("invalid withdrawal amount")

        except (KeyError, InvalidOperation, ValueError, TypeError):
            log.warning(
                "Withdrawal session missing amount for user %s; data=%r",
                message.from_user.id,
                saved_data,
            )
            await clear_state(message.from_user.id)
            await message.answer(
                "❌ لم أتمكن من استعادة مبلغ السحب.\n"
                "اضغط «💸 سحب رصيد» وابدأ من جديد."
            )
            return

        try:
            result = await db.create_withdrawal(
                message.from_user.id,
                amount,
                tel_cash_number,
            )
        except Exception:
            log.exception(
                "Failed to create withdrawal for user %s",
                message.from_user.id,
            )
            await message.answer(
                "⚠️ حدث خطأ أثناء إنشاء طلب السحب. "
                "حاول مرة أخرى بعد قليل."
            )
            return

        if result == "pending":
            await clear_state(message.from_user.id)
            await message.answer(
                "⏳ لديك طلب سحب قيد المراجعة بالفعل."
            )
            return

        if result is None:
            await clear_state(message.from_user.id)
            await message.answer(
                "❌ رصيدك غير كافٍ لهذا المبلغ، أو تعذر إنشاء الطلب."
            )
            return

        await clear_state(message.from_user.id)

        user = await db.get_user(message.from_user.id)
        safe_number = html.escape(tel_cash_number)

        await message.answer(
            f"✅ تم إرسال طلب السحب رقم <code>#{result}</code> "
            f"بقيمة <b>{money(amount)}</b>.\n"
            f"📱 رقم Tel Cash: <code>{safe_number}</code>\n"
            "سيتم مراجعته من الإدارة."
        )

        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(
                    admin_id,
                    f"💸 <b>طلب سحب جديد #{result}</b>\n\n"
                    f'👤 المستخدم: <code>{user["id"]}</code>\n'
                    f"💰 المبلغ: <b>{money(amount)}</b>\n"
                    f"📱 Tel Cash: <code>{safe_number}</code>\n"
                    f'👥 الإحالات: <b>{user["referrals"]}</b>',
                    reply_markup=withdrawal_actions(result),
                )
            except Exception:
                log.exception(
                    "Failed to notify admin %s about withdrawal",
                    admin_id,
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
                f"🎉 تمت إضافة <b>{money(result)}</b> إلى رصيدك."
            )
        else:
            await message.answer(f"❌ {result}")
        return

    if state == "contact":
        await clear_state(message.from_user.id)
        user = await db.get_user(message.from_user.id)
        sent = False

        contact_target = await db.get_setting("admin_contact", "")
        targets = settings.admin_ids

        if contact_target.strip().lstrip("-").isdigit():
            targets = {int(contact_target.strip())}

        for admin_id in targets:
            try:
                await bot.send_message(
                    admin_id,
                    f"📨 <b>رسالة من مستخدم</b>\n\n"
                    f'🆔 <code>{user["id"]}</code>\n'
                    f'👤 @{html.escape(user["username"] or "-")}\n\n'
                    f"{html.escape(text)}",
                )
                sent = True
            except Exception:
                log.exception(
                    "Failed to send contact message to admin %s",
                    admin_id,
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

            client = AsyncOpenAI(api_key=settings.openai_api_key)
            response = await client.responses.create(
                model=settings.openai_model,
                instructions=(
                    f"أنت مساعد محترم داخل {settings.bot_name}. "
                    "أجب بالعربية عند الحاجة وباختصار مفيد."
                ),
                input=text,
            )

            answer = response.output_text.strip() or "لم أستطع توليد إجابة."
            await message.answer(html.escape(answer))

        except Exception:
            log.exception("OpenAI request failed")
            await message.answer(
                "⚠️ حدث خطأ مؤقت في خدمة الذكاء الاصطناعي."
            )
        return

    if text and not text.startswith("/"):
        await message.answer(
            "اختر أحد الأزرار من القائمة، "
            "أو استخدم «اسأل الذكاء الاصطناعي» 🤖.",
            reply_markup=main_menu(),
        )


# ---------------------------- admin callbacks and actions ----------------------------

@dp.callback_query(F.data.startswith("adm:"))
async def admin_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح.", show_alert=True)
        return

    await callback.answer()
    action = callback.data.split(":", 1)[1]
    uid = callback.from_user.id

    if action == "home":
        await clear_state(uid)
        await callback.message.edit_text(
            f"🛠 <b>لوحة إدارة {settings.bot_name}</b>",
            reply_markup=admin_menu(),
        )

    elif action == "add_balance":
        await prompt_state(uid, "adm_add_balance")
        await callback.message.edit_text(
            "➕ أرسل: <code>USER_ID|AMOUNT</code>",
            reply_markup=back_button("adm:home"),
        )

    elif action == "deduct_balance":
        await prompt_state(uid, "adm_deduct_balance")
        await callback.message.edit_text(
            "➖ أرسل: <code>USER_ID|AMOUNT</code>",
            reply_markup=back_button("adm:home"),
        )

    elif action == "create_gift":
        await prompt_state(uid, "adm_gift")
        await callback.message.edit_text(
            "🎁 أرسل: <code>CODE|AMOUNT|MAX_USES|YYYY-MM-DD</code>\n"
            "آخر جزء اختياري.",
            reply_markup=back_button("adm:home"),
        )

    elif action == "user_logs":
        await prompt_state(uid, "adm_logs")
        await callback.message.edit_text(
            "🔎 أرسل ID المستخدم:",
            reply_markup=back_button("adm:home"),
        )

    elif action == "stats":
        stats = await db.stats()
        await callback.message.edit_text(
            f'📊 <b>الإحصائيات</b>\n\n'
            f'👥 المشتركين: <b>{stats["users"]}</b>\n'
            f'💰 مجموع الأرصدة: <b>{money(stats["balance"])}</b>\n'
            f'👥 مجموع الإحالات: <b>{stats["referrals"]}</b>\n'
            f'💸 طلبات سحب معلقة: <b>{stats["pending_withdrawals"]}</b>',
            reply_markup=back_button("adm:home"),
        )

    elif action == "balances":
        stats = await db.stats()
        await callback.message.edit_text(
            f'💰 <b>معلومات الأرصدة</b>\n'
            f'إجمالي الأرصدة: <b>{money(stats["balance"])}</b>',
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
            safe_number = html.escape(
                str(row["tel_cash_number"] or "-")
            )
            await callback.message.answer(
                f'💸 <b>طلب #{row["id"]}</b>\n'
                f'🆔 <code>{row["user_id"]}</code>\n'
                f'💰 <b>{money(row["amount"])}</b>\n'
                f'📱 Tel Cash: <code>{safe_number}</code>\n'
                f'👥 إحالات: <b>{row["referrals"]}</b>\n'
                f'👤 @{html.escape(row["username"] or "-")}',
                reply_markup=withdrawal_actions(row["id"]),
            )

    elif action == "add_channel":
        await prompt_state(uid, "adm_add_channel")
        await callback.message.edit_text(
            "📣 أرسل: <code>CHAT_ID|TITLE|JOIN_URL</code>\n"
            "مثال: <code>@mychannel|قناتي|https://t.me/mychannel</code>",
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

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text=f'🗑 {row["title"]}',
                    callback_data=f'adm_delch:{row["id"]}',
                )]
                for row in rows
            ] + [[InlineKeyboardButton(
                text="↩️ رجوع",
                callback_data="adm:home",
            )]]
        )
        await callback.message.edit_text(
            "اختر القناة للحذف:",
            reply_markup=keyboard,
        )

    elif action == "add_offer":
        await prompt_state(uid, "adm_add_offer")
        await callback.message.edit_text(
            "🛍 أرسل: <code>TITLE|DESCRIPTION|URL</code>\n"
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

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text=f'🗑 {row["title"]}',
                    callback_data=f'adm_deloff:{row["id"]}',
                )]
                for row in rows
            ] + [[InlineKeyboardButton(
                text="↩️ رجوع",
                callback_data="adm:home",
            )]]
        )
        await callback.message.edit_text(
            "اختر العرض للحذف:",
            reply_markup=keyboard,
        )

    elif action == "settings":
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="🎁 مكافأة الإحالة",
                    callback_data="admset:referral",
                )],
                [InlineKeyboardButton(
                    text="💸 الحد الأدنى للسحب",
                    callback_data="admset:minwd",
                )],
                [InlineKeyboardButton(
                    text="📝 رسالة الترحيب",
                    callback_data="admset:welcome",
                )],
                [InlineKeyboardButton(
                    text="📨 وسيلة التواصل",
                    callback_data="admset:contact",
                )],
                [InlineKeyboardButton(
                    text="ℹ️ معلومات السحب",
                    callback_data="admset:wdinfo",
                )],
                [InlineKeyboardButton(
                    text="↩️ رجوع",
                    callback_data="adm:home",
                )],
            ]
        )

        await callback.message.edit_text(
            f'⚙️ <b>الإعدادات الحالية</b>\n\n'
            f'🎁 مكافأة الإحالة: <b>{await db.get_setting("referral_reward")}</b>\n'
            f'💸 الحد الأدنى للسحب: <b>{await db.get_setting("min_withdrawal")}</b>\n'
            f'📨 حساب/وسيلة التواصل: '
            f'<b>{html.escape(str(await db.get_setting("admin_contact") or "غير محدد"))}</b>\n\n'
            f'اختر الإعداد الذي تريد تغييره:',
            reply_markup=keyboard,
        )

    elif action == "broadcast":
        await prompt_state(uid, "adm_broadcast")
        await callback.message.edit_text(
            "📢 أرسل الرسالة التي تريد إذاعتها لكل المستخدمين غير المحظورين.",
            reply_markup=back_button("adm:home"),
        )


@dp.callback_query(F.data.startswith("adm_delch:"))
async def delete_channel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح.", show_alert=True)
        return

    await db.delete_channel(int(callback.data.split(":")[1]))
    await callback.answer("تم الحذف")
    await callback.message.edit_text(
        "تم حذف القناة.",
        reply_markup=back_button("adm:home"),
    )


@dp.callback_query(F.data.startswith("adm_deloff:"))
async def delete_offer(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح.", show_alert=True)
        return

    await db.delete_offer(int(callback.data.split(":")[1]))
    await callback.answer("تم الحذف")
    await callback.message.edit_text(
        "تم حذف العرض.",
        reply_markup=back_button("adm:home"),
    )


@dp.callback_query(F.data.startswith("admset:"))
async def admin_settings_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح.", show_alert=True)
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
        "contact": "📨 أرسل وسيلة التواصل (username أو رابط):",
        "wdinfo": "ℹ️ أرسل معلومات السحب التي ستظهر للمستخدم:",
    }

    if key not in mapping:
        await callback.answer("إعداد غير معروف.", show_alert=True)
        return

    await prompt_state(callback.from_user.id, mapping[key])
    await callback.answer()
    await callback.message.edit_text(
        prompts[key],
        reply_markup=back_button("adm:home"),
    )


@dp.callback_query(F.data.startswith("wd:"))
async def withdrawal_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح.", show_alert=True)
        return

    _, action, withdrawal_id = callback.data.split(":")
    result = await db.process_withdrawal(
        int(withdrawal_id),
        action == "approve",
    )

    if not result:
        await callback.answer(
            "الطلب غير موجود أو تمت معالجته.",
            show_alert=True,
        )
        return

    status, uid, amount = result
    await callback.answer("تمت المعالجة.")
    await callback.message.edit_reply_markup(reply_markup=None)

    try:
        if status == "approved":
            await bot.send_message(
                uid,
                f"✅ تم قبول طلب السحب <b>#{withdrawal_id}</b> "
                f"وخصم <b>{money(amount)}</b> من رصيدك.",
            )
        else:
            await bot.send_message(
                uid,
                f"❌ تم رفض طلب السحب <b>#{withdrawal_id}</b>. "
                "لم يتم خصم الرصيد.",
            )
    except Exception:
        log.exception(
            "Failed to notify user %s about withdrawal",
            uid,
        )


# ---------------------------- admin text states ----------------------------

async def handle_admin_state(
    message: Message,
    state: str,
    text: str,
):
    if state in {"adm_add_balance", "adm_deduct_balance"}:
        await clear_state(message.from_user.id)

        try:
            uid_string, amount_string = [
                part.strip() for part in text.split("|", 1)
            ]
            uid = int(uid_string)
            amount = Decimal(amount_string)

            if not amount.is_finite() or amount <= 0:
                raise ValueError

            delta = amount if state == "adm_add_balance" else -amount
            action = (
                "admin_add_balance" if delta > 0
                else "admin_deduct_balance"
            )

            new_balance = await db.change_balance(
                uid, delta, action, {"amount": str(amount)}
            )

            if new_balance is None:
                await message.answer(
                    "❌ المستخدم غير موجود.",
                    reply_markup=admin_menu(),
                )
                return

            await message.answer(
                f"✅ تم التعديل. الرصيد الجديد: <b>{money(new_balance)}</b>",
                reply_markup=admin_menu(),
            )

            try:
                operation = "إضافة" if delta > 0 else "خصم"
                await bot.send_message(
                    uid,
                    f"💰 تم {operation} <b>{money(amount)}</b> من رصيدك.\n"
                    f"الرصيد الحالي: <b>{money(new_balance)}</b>",
                )
            except Exception:
                log.exception(
                    "Failed to notify user %s about balance change",
                    uid,
                )

        except Exception:
            await message.answer(
                "❌ الصيغة الصحيحة: <code>USER_ID|AMOUNT</code>",
                reply_markup=admin_menu(),
            )
        return

    if state == "adm_gift":
        await clear_state(message.from_user.id)

        try:
            parts = [part.strip() for part in text.split("|")]
            if len(parts) < 3:
                raise ValueError

            code, amount_string, uses_string = parts[:3]
            amount = Decimal(amount_string)
            uses = int(uses_string)

            if not amount.is_finite() or amount <= 0 or uses <= 0:
                raise ValueError

            expires = None
            if len(parts) >= 4 and parts[3]:
                from datetime import datetime, timezone
                expires = datetime.strptime(
                    parts[3], "%Y-%m-%d"
                ).replace(tzinfo=timezone.utc)

            await db.create_gift(code.upper(), amount, uses, expires)
            await message.answer(
                f"✅ تم إنشاء الكود <code>{html.escape(code.upper())}</code> "
                f"بقيمة <b>{money(amount)}</b> وعدد استخدامات {uses}.",
                reply_markup=admin_menu(),
            )

        except Exception:
            await message.answer(
                "❌ الصيغة: <code>CODE|AMOUNT|MAX_USES|YYYY-MM-DD</code>",
                reply_markup=admin_menu(),
            )
        return

    if state in {"adm_logs", "adm_ban", "adm_unban"}:
        await clear_state(message.from_user.id)

        try:
            uid = int(text)
        except ValueError:
            await message.answer(
                "❌ ID غير صحيح.",
                reply_markup=admin_menu(),
            )
            return

        if state == "adm_logs":
            user = await db.get_user(uid)
            logs = await db.user_logs(uid)

            if not user:
                await message.answer(
                    "المستخدم غير موجود.",
                    reply_markup=admin_menu(),
                )
                return

            output = (
                f"🔎 <b>المستخدم {uid}</b>\n"
                f'💰 الرصيد: {money(user["balance"])}\n'
                f'👥 الإحالات: {user["referrals"]}\n\n'
            )

            for row in logs[:15]:
                output += (
                    f'• {row["created_at"]:%Y-%m-%d %H:%M} '
                    f'— {html.escape(row["action"])}\n'
                )

            await message.answer(output, reply_markup=admin_menu())

        else:
            await db.set_banned(uid, state == "adm_ban")
            operation = "حظر" if state == "adm_ban" else "فك حظر"
            await message.answer(
                f"✅ تم {operation} المستخدم.",
                reply_markup=admin_menu(),
            )
        return

    if state == "adm_add_channel":
        try:
            parts = [part.strip() for part in text.split("|", 2)]
            if len(parts) != 3:
                raise ValueError

            chat_id, title, url = parts
            if not chat_id or not title or not url:
                raise ValueError

            await db.add_channel(chat_id, title, url)
            await clear_state(message.from_user.id)

            log.info(
                "Admin %s added forced-subscription channel %s",
                message.from_user.id,
                chat_id,
            )
            await message.answer(
                "✅ تمت إضافة القناة وحفظها.\n\n"
                "تأكد من أن البوت يستطيع الوصول إلى القناة "
                "والتحقق من عضوية المستخدمين.",
                reply_markup=admin_menu(),
            )

        except Exception:
            log.exception("Failed to add forced-subscription channel")
            await message.answer(
                "❌ لم تنجح إضافة القناة.\n"
                "تأكد من الصيغة:\n"
                "<code>CHAT_ID|TITLE|JOIN_URL</code>\n\n"
                "مثال:\n"
                "<code>@mychannel|قناتي|https://t.me/mychannel</code>",
                reply_markup=admin_menu(),
            )
            await clear_state(message.from_user.id)
        return

    if state == "adm_add_offer":
        await clear_state(message.from_user.id)

        try:
            parts = text.split("|", 2)
            if len(parts) < 2:
                raise ValueError

            title = parts[0].strip()
            description = parts[1].strip()
            url = parts[2].strip() if len(parts) > 2 else ""

            if not title or not description:
                raise ValueError

            await db.add_offer(title, description, url)
            await message.answer(
                "✅ تمت إضافة العرض.",
                reply_markup=admin_menu(),
            )

        except Exception:
            await message.answer(
                "❌ الصيغة: <code>TITLE|DESCRIPTION|URL</code>",
                reply_markup=admin_menu(),
            )
        return

    if state == "adm_broadcast":
        await clear_state(message.from_user.id)
        rows = await db.all_user_ids()
        sent = 0
        failed = 0

        status_message = await message.answer(
            f"📢 بدء الإذاعة إلى {len(rows)} مستخدم…"
        )

        for row in rows:
            try:
                await bot.send_message(row["id"], text)
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.04)

        await status_message.edit_text(
            f"📢 انتهت الإذاعة\n\n"
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
        key = settings_map[state]

        if key in {"referral_reward", "min_withdrawal"}:
            try:
                value = Decimal(text)
                if not value.is_finite() or value < 0:
                    raise ValueError
            except Exception:
                await message.answer(
                    "❌ قيمة غير صحيحة.",
                    reply_markup=admin_menu(),
                )
                return

        try:
            await db.set_setting(key, text)
            await clear_state(message.from_user.id)
            log.info(
                "Admin %s updated setting %s",
                message.from_user.id,
                key,
            )
            await message.answer(
                "✅ تم حفظ الإعداد.",
                reply_markup=admin_menu(),
            )

        except Exception:
            log.exception("Failed to save setting %s", key)
            await message.answer(
                "❌ حدث خطأ أثناء حفظ الإعداد.",
                reply_markup=admin_menu(),
            )
        return


# ---------------------------- heartbeat ----------------------------

async def heartbeat_loop():
    while True:
        await asyncio.sleep(180)
        log.info("HEARTBEAT: bot process is running")


# ---------------------------- FastAPI / Render ----------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    heartbeat_task = None
    await db.connect()
    log.info("Database connection established")

    try:
        if settings.webhook_url:
            await bot.set_webhook(
                url=f"{settings.webhook_url}/telegram/webhook",
                secret_token=settings.webhook_secret,
                allowed_updates=dp.resolve_used_update_types(),
            )
            log.info("Webhook configured")
        else:
            log.warning(
                "WEBHOOK_URL is empty; webhook is not configured."
            )

        heartbeat_task = asyncio.create_task(
            heartbeat_loop(),
            name="bot-heartbeat",
        )
        log.info("Heartbeat task started; interval=180 seconds")
        yield

    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

        try:
            await db.close()
            log.info("Database connection closed")
        finally:
            await bot.session.close()
            log.info("Bot HTTP session closed")


app = FastAPI(
    title="Sadek Telegram Bot",
    lifespan=lifespan,
)


@app.get("/", response_class=PlainTextResponse)
async def root():
    return "Sadek bot is running."


@app.get("/health", response_class=PlainTextResponse)
async def health():
    try:
        await db.pool.fetchval("SELECT 1")
        return "ok"
    except Exception:
        log.exception("Health check failed")
        raise HTTPException(
            status_code=503,
            detail="database unavailable",
        )


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if settings.webhook_secret:
        token = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token"
        )
        if token != settings.webhook_secret:
            raise HTTPException(status_code=403, detail="forbidden")

    data = await request.json()
    update = Update.model_validate(
        data,
        context={"bot": bot},
    )
    await dp.feed_update(bot, update)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
    )
