import json
from datetime import datetime, timezone
from decimal import Decimal
import asyncpg

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
    id BIGINT PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    balance NUMERIC(20,2) NOT NULL DEFAULT 0,
    referrals INTEGER NOT NULL DEFAULT 0,
    referred_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
    referral_rewarded BOOLEAN NOT NULL DEFAULT FALSE,
    is_banned BOOLEAN NOT NULL DEFAULT FALSE,
    state TEXT,
    state_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by);
CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
    id BIGSERIAL PRIMARY KEY,
    chat_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    join_url TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS offers (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    url TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS gift_codes (
    code TEXT PRIMARY KEY,
    amount NUMERIC(20,2) NOT NULL CHECK(amount > 0),
    max_uses INTEGER NOT NULL DEFAULT 1 CHECK(max_uses > 0),
    uses INTEGER NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS gift_redemptions (
    code TEXT REFERENCES gift_codes(code) ON DELETE CASCADE,
    user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
    redeemed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(code, user_id)
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount NUMERIC(20,2) NOT NULL CHECK(amount > 0),
    referrals INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals(status, created_at);

CREATE TABLE IF NOT EXISTS logs (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT,
    action TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_logs_user_id ON logs(user_id, created_at DESC);
'''

DEFAULTS = {
    'referral_reward': '100',
    'min_withdrawal': '5000',
    'withdrawal_info': 'الحد الأدنى للسحب: 5000. أرسل المبلغ المطلوب وسيتم مراجعة الطلب من الإدارة.',
    'welcome_message': 'أهلاً بك في صادق Sadek bot ✨\nابدأ بجمع الإحالات واستفد من رصيدك.',
    'admin_contact': '',
}


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(self.dsn, min_size=2, max_size=15, command_timeout=30)
        async with self.pool.acquire() as con:
            await con.execute(SCHEMA)
            # Safe migration for databases created by an earlier Sadek Bot version.
            await con.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_rewarded BOOLEAN NOT NULL DEFAULT FALSE")
            for k, v in DEFAULTS.items():
                await con.execute('INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO NOTHING', k, v)

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def get_setting(self, key: str, default: str = '') -> str:
        async with self.pool.acquire() as con:
            row = await con.fetchrow('SELECT value FROM settings WHERE key=$1', key)
            return row['value'] if row else default

    async def set_setting(self, key: str, value: str):
        async with self.pool.acquire() as con:
            await con.execute('''INSERT INTO settings(key,value) VALUES($1,$2)
                ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value''', key, value)

    async def ensure_user(self, tg_user, referrer_id: int | None = None) -> tuple[bool, Decimal]:
        uid = tg_user.id
        async with self.pool.acquire() as con:
            async with con.transaction():
                existing = await con.fetchrow('SELECT id, balance FROM users WHERE id=$1 FOR UPDATE', uid)
                if existing:
                    await con.execute('''UPDATE users SET username=$2, first_name=$3, updated_at=NOW() WHERE id=$1''',
                                      uid, tg_user.username, tg_user.first_name)
                    return False, Decimal(existing['balance'])
                valid_ref = referrer_id if referrer_id and referrer_id != uid else None
                await con.execute('''INSERT INTO users(id,username,first_name,referred_by) VALUES($1,$2,$3,$4)''',
                                  uid, tg_user.username, tg_user.first_name, valid_ref)
                if valid_ref:
                    ref_exists = await con.fetchval('SELECT 1 FROM users WHERE id=$1 AND is_banned=FALSE', valid_ref)
                    if not ref_exists:
                        await con.execute('UPDATE users SET referred_by=NULL WHERE id=$1', uid)
                        valid_ref = None
                await con.execute('INSERT INTO logs(user_id,action,details) VALUES($1,$2,$3)', uid, 'register', json.dumps({'referrer': valid_ref}))
                return True, Decimal('0')

    async def finalize_referral(self, uid: int):
        """Reward a referral exactly once, after the referred user passes forced subscription."""
        async with self.pool.acquire() as con:
            async with con.transaction():
                user = await con.fetchrow(
                    'SELECT referred_by, referral_rewarded FROM users WHERE id=$1 FOR UPDATE', uid
                )
                if not user or not user['referred_by'] or user['referral_rewarded']:
                    return False
                referrer_id = user['referred_by']
                ref_exists = await con.fetchval(
                    'SELECT 1 FROM users WHERE id=$1 AND is_banned=FALSE', referrer_id
                )
                if not ref_exists:
                    await con.execute('UPDATE users SET referral_rewarded=TRUE WHERE id=$1', uid)
                    return False
                reward = Decimal(await self.get_setting_tx(con, 'referral_reward', '0'))
                await con.execute(
                    'UPDATE users SET balance=balance+$1, referrals=referrals+1, updated_at=NOW() WHERE id=$2',
                    reward, referrer_id
                )
                await con.execute(
                    'UPDATE users SET referral_rewarded=TRUE, updated_at=NOW() WHERE id=$1', uid
                )
                await con.execute(
                    'INSERT INTO logs(user_id,action,details) VALUES($1,$2,$3)',
                    referrer_id, 'referral_reward', json.dumps({'new_user': uid, 'amount': str(reward)})
                )
                return True

    async def get_setting_tx(self, con, key, default=''):
        row = await con.fetchrow('SELECT value FROM settings WHERE key=$1', key)
        return row['value'] if row else default

    async def get_user(self, uid: int):
        async with self.pool.acquire() as con:
            return await con.fetchrow('SELECT * FROM users WHERE id=$1', uid)

    async def set_state(self, uid: int, state: str | None, data: dict | None = None):
        async with self.pool.acquire() as con:
            await con.execute('UPDATE users SET state=$2,state_data=$3,updated_at=NOW() WHERE id=$1', uid, state, json.dumps(data or {}))

    async def get_state(self, uid: int):
        async with self.pool.acquire() as con:
            row = await con.fetchrow('SELECT state,state_data FROM users WHERE id=$1', uid)
            if not row:
                return None, {}
            return row['state'], row['state_data'] or {}

    async def change_balance(self, uid: int, amount: Decimal, action: str, details=None):
        async with self.pool.acquire() as con:
            async with con.transaction():
                row = await con.fetchrow('SELECT balance FROM users WHERE id=$1 FOR UPDATE', uid)
                if not row:
                    return None
                new_balance = Decimal(row['balance']) + amount
                if new_balance < 0:
                    raise ValueError('insufficient_balance')
                await con.execute('UPDATE users SET balance=$2,updated_at=NOW() WHERE id=$1', uid, new_balance)
                await con.execute('INSERT INTO logs(user_id,action,details) VALUES($1,$2,$3)', uid, action, json.dumps(details or {'amount': str(amount)}))
                return new_balance

    async def create_gift(self, code, amount, max_uses, expires_at=None):
        async with self.pool.acquire() as con:
            await con.execute('INSERT INTO gift_codes(code,amount,max_uses,expires_at) VALUES($1,$2,$3,$4)', code, amount, max_uses, expires_at)

    async def redeem_gift(self, uid, code):
        code = code.strip().upper()
        async with self.pool.acquire() as con:
            async with con.transaction():
                row = await con.fetchrow('SELECT * FROM gift_codes WHERE code=$1 FOR UPDATE', code)
                if not row or not row['enabled'] or row['uses'] >= row['max_uses']:
                    return False, 'الكود غير صالح أو انتهت استخداماته.'
                if row['expires_at'] and row['expires_at'] < datetime.now(timezone.utc):
                    return False, 'انتهت صلاحية هذا الكود.'
                used = await con.fetchval('SELECT 1 FROM gift_redemptions WHERE code=$1 AND user_id=$2', code, uid)
                if used:
                    return False, 'لقد استخدمت هذا الكود مسبقاً.'
                await con.execute('INSERT INTO gift_redemptions(code,user_id) VALUES($1,$2)', code, uid)
                await con.execute('UPDATE gift_codes SET uses=uses+1 WHERE code=$1', code)
                await con.execute('UPDATE users SET balance=balance+$1,updated_at=NOW() WHERE id=$2', row['amount'], uid)
                await con.execute('INSERT INTO logs(user_id,action,details) VALUES($1,$2,$3)', uid, 'gift_redeem', json.dumps({'code': code, 'amount': str(row['amount'])}))
                return True, str(row['amount'])

    async def list_channels(self):
        async with self.pool.acquire() as con:
            return await con.fetch('SELECT * FROM channels WHERE enabled=TRUE ORDER BY id')

    async def add_channel(self, chat_id, title, join_url):
        async with self.pool.acquire() as con:
            await con.execute('''INSERT INTO channels(chat_id,title,join_url) VALUES($1,$2,$3)
                ON CONFLICT(chat_id) DO UPDATE SET title=EXCLUDED.title,join_url=EXCLUDED.join_url,enabled=TRUE''', chat_id, title, join_url or None)

    async def delete_channel(self, channel_id):
        async with self.pool.acquire() as con:
            await con.execute('UPDATE channels SET enabled=FALSE WHERE id=$1', channel_id)

    async def add_offer(self, title, description, url):
        async with self.pool.acquire() as con:
            await con.execute('INSERT INTO offers(title,description,url) VALUES($1,$2,$3)', title, description, url or None)

    async def list_offers(self):
        async with self.pool.acquire() as con:
            return await con.fetch('SELECT * FROM offers WHERE enabled=TRUE ORDER BY id DESC')

    async def delete_offer(self, offer_id):
        async with self.pool.acquire() as con:
            await con.execute('UPDATE offers SET enabled=FALSE WHERE id=$1', offer_id)

    async def create_withdrawal(self, uid, amount):
        async with self.pool.acquire() as con:
            async with con.transaction():
                user = await con.fetchrow('SELECT balance,referrals FROM users WHERE id=$1 FOR UPDATE', uid)
                if not user or Decimal(user['balance']) < amount:
                    return None
                pending = await con.fetchval("SELECT 1 FROM withdrawals WHERE user_id=$1 AND status='pending'", uid)
                if pending:
                    return 'pending'
                row = await con.fetchrow('''INSERT INTO withdrawals(user_id,amount,referrals) VALUES($1,$2,$3) RETURNING id''', uid, amount, user['referrals'])
                await con.execute('INSERT INTO logs(user_id,action,details) VALUES($1,$2,$3)', uid, 'withdrawal_requested', json.dumps({'id': row['id'], 'amount': str(amount)}))
                return row['id']

    async def list_withdrawals(self, status='pending', limit=20):
        async with self.pool.acquire() as con:
            return await con.fetch('''SELECT w.*,u.username,u.first_name,u.balance FROM withdrawals w
                JOIN users u ON u.id=w.user_id WHERE w.status=$1 ORDER BY w.id ASC LIMIT $2''', status, limit)

    async def process_withdrawal(self, wid: int, approve: bool, note=''):
        async with self.pool.acquire() as con:
            async with con.transaction():
                row = await con.fetchrow('SELECT * FROM withdrawals WHERE id=$1 FOR UPDATE', wid)
                if not row or row['status'] != 'pending':
                    return None
                if approve:
                    user = await con.fetchrow('SELECT balance FROM users WHERE id=$1 FOR UPDATE', row['user_id'])
                    if Decimal(user['balance']) < Decimal(row['amount']):
                        await con.execute("UPDATE withdrawals SET status='rejected',note=$2,processed_at=NOW() WHERE id=$1", wid, 'الرصيد الحالي غير كافٍ')
                        return ('rejected', row['user_id'], Decimal(row['amount']))
                    await con.execute('UPDATE users SET balance=balance-$1,updated_at=NOW() WHERE id=$2', row['amount'], row['user_id'])
                    await con.execute("UPDATE withdrawals SET status='approved',note=$2,processed_at=NOW() WHERE id=$1", wid, note or 'approved')
                    await con.execute('INSERT INTO logs(user_id,action,details) VALUES($1,$2,$3)', row['user_id'], 'withdrawal_approved', json.dumps({'id': wid, 'amount': str(row['amount'])}))
                    return ('approved', row['user_id'], Decimal(row['amount']))
                await con.execute("UPDATE withdrawals SET status='rejected',note=$2,processed_at=NOW() WHERE id=$1", wid, note or 'rejected')
                await con.execute('INSERT INTO logs(user_id,action,details) VALUES($1,$2,$3)', row['user_id'], 'withdrawal_rejected', json.dumps({'id': wid}))
                return ('rejected', row['user_id'], Decimal(row['amount']))

    async def search_user(self, uid):
        return await self.get_user(uid)

    async def user_logs(self, uid, limit=30):
        async with self.pool.acquire() as con:
            return await con.fetch('SELECT * FROM logs WHERE user_id=$1 ORDER BY id DESC LIMIT $2', uid, limit)

    async def count_users(self):
        async with self.pool.acquire() as con:
            return await con.fetchval('SELECT COUNT(*) FROM users')

    async def stats(self):
        async with self.pool.acquire() as con:
            return await con.fetchrow('''SELECT COUNT(*) users,
                COALESCE(SUM(balance),0) balance,
                COALESCE(SUM(referrals),0) referrals,
                (SELECT COUNT(*) FROM withdrawals WHERE status='pending') pending_withdrawals
                FROM users''')

    async def all_user_ids(self):
        async with self.pool.acquire() as con:
            return await con.fetch('SELECT id FROM users WHERE is_banned=FALSE ORDER BY id')

    async def set_banned(self, uid, banned: bool):
        async with self.pool.acquire() as con:
            await con.execute('UPDATE users SET is_banned=$2 WHERE id=$1', uid, banned)
            await con.execute('INSERT INTO logs(user_id,action,details) VALUES($1,$2,$3)', uid, 'ban' if banned else 'unban', '{}')
