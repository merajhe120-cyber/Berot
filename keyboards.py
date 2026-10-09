from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)


def main_menu():
    """القائمة الرئيسية للمستخدم."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="👤 الملف الشخصي"),
                KeyboardButton(text="👥 الإحالات"),
            ],
            [
                KeyboardButton(text="💸 سحب الأرباح"),
                KeyboardButton(text="🎁 كود الهدية"),
            ],
            [
                KeyboardButton(text="🎯 العروض"),
                KeyboardButton(text="📞 التواصل"),
            ],
            [
                KeyboardButton(text="🤖 الذكاء الاصطناعي"),
            ],
        ],
        resize_keyboard=True,
        input_field_placeholder="اختر من القائمة 👇",
    )


def admin_menu():
    """قائمة تحكم الإدارة."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ إضافة رصيد",
                    callback_data="adm:add_balance",
                ),
                InlineKeyboardButton(
                    text="➖ خصم رصيد",
                    callback_data="adm:deduct_balance",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎁 إنشاء كود هدية",
                    callback_data="adm:create_gift",
                ),
                InlineKeyboardButton(
                    text="📋 سجلات المستخدم",
                    callback_data="adm:user_logs",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📊 الإحصائيات",
                    callback_data="adm:stats",
                ),
                InlineKeyboardButton(
                    text="💰 أرصدة المستخدمين",
                    callback_data="adm:balances",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🚫 حظر مستخدم",
                    callback_data="adm:ban",
                ),
                InlineKeyboardButton(
                    text="✅ إلغاء الحظر",
                    callback_data="adm:unban",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💸 طلبات السحب",
                    callback_data="adm:withdrawals",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="➕ إضافة قناة",
                    callback_data="adm:add_channel",
                ),
                InlineKeyboardButton(
                    text="➖ حذف قناة",
                    callback_data="adm:del_channel",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="➕ إضافة عرض",
                    callback_data="adm:add_offer",
                ),
                InlineKeyboardButton(
                    text="➖ حذف عرض",
                    callback_data="adm:del_offer",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⚙️ الإعدادات",
                    callback_data="adm:settings",
                ),
                InlineKeyboardButton(
                    text="📢 إذاعة رسالة",
                    callback_data="adm:broadcast",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🏠 القائمة الرئيسية",
                    callback_data="adm:home",
                ),
            ],
        ]
    )


def normalize_join_url(url: str) -> str:
    """توحيد رابط القناة."""
    url = (url or "").strip()

    if not url:
        return ""

    if url.startswith("@"):
        return f"https://t.me/{url[1:]}"

    if url.startswith("t.me/"):
        return f"https://{url}"

    if not url.startswith(("https://", "http://")):
        return f"https://{url}"

    return url


def force_sub_keyboard(channels):
    """أزرار الاشتراك الإجباري والتحقق من الاشتراك."""
    rows = []

    for channel in channels or []:
        title = channel.get("title") or "القناة"
        join_url = normalize_join_url(
            channel.get("join_url") or ""
        )

        if join_url:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"📢 الاشتراك في {title}",
                        url=join_url,
                    )
                ]
            )

    rows.append(
        [
            InlineKeyboardButton(
                text="🔄 تحقق من الاشتراك",
                callback_data="check_sub",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def withdrawal_actions(withdrawal_id):
    """أزرار قبول أو رفض طلب السحب."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ قبول الطلب",
                    callback_data=(
                        f"adm:withdraw_approve:{withdrawal_id}"
                    ),
                ),
                InlineKeyboardButton(
                    text="❌ رفض الطلب",
                    callback_data=(
                        f"adm:withdraw_reject:{withdrawal_id}"
                    ),
                ),
            ]
        ]
    )


def back_button(callback_data="adm:home", text="🔙 رجوع"):
    """زر رجوع قابل لإعادة الاستخدام."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=text,
                    callback_data=callback_data,
                )
            ]
        ]
    )
