# صادق Sadek bot 🤖

بوت إحالات وأرصدة Telegram جاهز للنشر على GitHub + Render مع PostgreSQL.

## المزايا
- اشتراك إجباري بعدد غير محدود من القنوات من لوحة الإدارة.
- ملف المستخدم: ID + الرصيد + عدد الإحالات.
- رابط إحالة فريد لكل مستخدم ومكافأة قابلة للتعديل.
- سحب رصيد بحد أدنى قابل للتعديل، ومنع الطلب عند عدم كفاية الرصيد، مع طلبات pending/approve/reject.
- أكواد هدايا بقيمة وعدد استخدامات وتاريخ انتهاء اختياري.
- تواصل معنا وإرسال الرسائل للإدارة.
- عروض قابلة للإضافة والحذف.
- لوحة إدارة كاملة: رصيد، خصم، هدايا، سجلات، إحصائيات، حظر، سحب، قنوات، عروض، إعدادات، إذاعة.
- PostgreSQL عبر asyncpg مع معاملات transaction للحركات المالية المهمة.
- FastAPI webhook مناسب لـ Render، مع `/health`.
- تكامل OpenAI اختياري عبر Responses API.

## متطلبات البيئة
انسخ `.env.example` إلى متغيرات البيئة في Render:

- `BOT_TOKEN`: توكن BotFather.
- `DATABASE_URL`: رابط PostgreSQL.
- `ADMIN_IDS`: أرقام Telegram ID للإدارة، مفصولة بفواصل.
- `WEBHOOK_URL`: رابط Render مثل `https://your-service.onrender.com`.
- `WEBHOOK_SECRET`: قيمة عشوائية طويلة.
- `admin_contact` من لوحة الإدارة يقبل Telegram chat ID؛ هذا هو الحساب الذي يستقبل «تواصل معنا».
- `OPENAI_API_KEY`: اختياري لتفعيل الذكاء الاصطناعي.
- `OPENAI_MODEL`: افتراضياً `gpt-5.6-luna`، ويمكن تغييره إلى نموذج متاح لحسابك. تكامل الذكاء يستخدم Responses API.
- `BOT_NAME`: افتراضياً `صادق Sadek bot`.

## النشر على Render
1. أنشئ PostgreSQL وأخذ `DATABASE_URL`.
2. ارفع المشروع إلى GitHub.
3. في Render: New → Web Service واربط مستودع GitHub.
4. Build: `pip install -r requirements.txt`
5. Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`
6. Health Check: `/health`
7. أضف متغيرات البيئة.
8. بعد أول تشغيل، ضع `WEBHOOK_URL` على رابط الخدمة وأعد النشر إذا لزم.

### مهم جداً للاشتراك الإجباري
أضف البوت إلى القنوات المطلوبة، ويفضل أن يكون مشرفاً حتى يستطيع Telegram إرجاع حالة العضوية بدقة. من لوحة الإدارة أضف `CHAT_ID|TITLE|JOIN_URL`.

### Render Free
الكود يستخدم webhook + health endpoint ولا يعتمد على ping خارجي للتحايل على إيقاف الخطة المجانية. Render توضح أن خدمات الويب المجانية قد تدخل في sleep بعد 15 دقيقة من عدم النشاط؛ للاستمرارية العالية استخدم خطة مناسبة. 

## اختبار محلي
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN='...'
export DATABASE_URL='...'
export ADMIN_IDS='123456789'
export WEBHOOK_URL=''
python -m app
```

في الإنتاج استخدم Render كما في الخطوات أعلاه.
