from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)


def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="👤 معلومات ملفي"),
                KeyboardButton(text="🔗 رابط إحالتي"),
            ],
            [
                KeyboardButton(text="💸 سحب رصيد"),
                KeyboardButton(text="🎁 كود هدية"),
            ],
            [
                KeyboardButton(text="📢 اشترك بالعروض"),
                KeyboardButton(text="📨 تواصل معنا"),
            ],
            [KeyboardButton(text="🤖 اسأل الذكاء الاصطناعي")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="اختر من القائمة…",
    )


def admin_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ إضافة رصيد", callback_data="adm:add_balance"),
                InlineKeyboardButton(text="➖ خصم رصيد", callback_data="adm:deduct_balance"),
            ],
            [
                InlineKeyboardButton(text="🎁 إنشاء كود هدية", callback_data="adm:create_gift"),
                InlineKeyboardButton(text="🔎 سجلات مستخدم", callback_data="adm:user_logs"),
            ],
            [
                InlineKeyboardButton(text="📊 الإحصائيات", callback_data="adm:stats"),
                InlineKeyboardButton(text="💰 معلومات الأرصدة", callback_data="adm:balances"),
            ],
            [
                InlineKeyboardButton(text="🚫 حظر مستخدم", callback_data="adm:ban"),
                InlineKeyboardButton(text="♻️ فك حظر", callback_data="adm:unban"),
            ],
            [InlineKeyboardButton(text="💸 طلبات السحب", callback_data="adm:withdrawals")],
            [
                InlineKeyboardButton(text="📣 إضافة قناة اشتراك", callback_data="adm:add_channel"),
                InlineKeyboardButton(text="🗑 حذف قناة", callback_data="adm:del_channel"),
            ],
            [
                InlineKeyboardButton(text="🛍 إضافة عرض", callback_data="adm:add_offer"),
                InlineKeyboardButton(text="🗑 حذف عرض", callback_data="adm:del_offer"),
            ],
            [
                InlineKeyboardButton(text="⚙️ إعدادات", callback_data="adm:settings"),
                InlineKeyboardButton(text="📢 إذاعة عامة", callback_data="adm:broadcast"),
            ],
            [InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="adm:home")],
        ]
    )


def normalize_join_url(value):
    value = (value or "").strip()

    if not value:
        return ""

    if value.startswith("@"):
        username = value[1:].strip()
        if username:
            return f"https://t.me/{username}"
        return ""

    if value.startswith("t.me/"):
        return f"https://{value}"

    if value.startswith(("https://t.me/", "http://t.me/")):
        return value

    return ""


def force_sub_keyboard(channels):
    rows = []

    for channel in channels:
        # asyncpg.Record يمكن التعامل معه كمجموعة حقول
        try:
            title = channel["title"]
            raw_url = channel["join_url"]
        except (KeyError, TypeError):
            continue

        url = normalize_join_url(raw_url)

        if not url:
            continue

        rows.append([
            InlineKeyboardButton(
                text=f"📢 {title or 'اشترك بالقناة'}",
                url=url,
            )
        ])

    rows.append([
        InlineKeyboardButton(
            text="✅ تحقّق من الاشتراك",
            callback_data="check_sub",
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def withdrawal_actions(wid: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ قبول",
                    callback_data=f"wd:approve:{wid}",
                ),
                InlineKeyboardButton(
                    text="❌ رفض",
                    callback_data=f"wd:reject:{wid}",
                ),
            ]
        ]
    )


def back_button(callback="back:main"):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="↩️ رجوع", callback_data=callback)]
        ]
    )
