import asyncio
import logging
import sys
import os
import random
import time
from datetime import datetime, timedelta
import pytz
import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)

# ==========================================
# CONFIGURATION
# ==========================================
# REPLACE WITH YOUR DETAILS FOR LOCAL TEST, BUT USE ENV VARS ON RENDER
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789")) 
PORT = int(os.getenv("PORT", 8080))
DB_NAME = "bot_database.db"

# Timezone for Bangladesh
BDT = pytz.timezone('Asia/Dhaka')

# Initialize Bot and Dispatcher
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ==========================================
# DATABASE MANAGER (Async SQLite)
# ==========================================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        # Users Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance REAL DEFAULT 0.0,
                joined_date TEXT,
                referrer_id INTEGER,
                verified_emoji INTEGER DEFAULT 0,
                verified_channels INTEGER DEFAULT 0,
                last_bonus_claim TEXT,
                is_banned INTEGER DEFAULT 0
            )
        """)
        # Settings Table (Dynamic Config)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # Initialize Default Settings if not exist
        defaults = {
            "refer_reward": "5.0",
            "daily_bonus": "2.0",
            "min_withdraw": "50.0",
            "withdraw_enabled": "1",  # 1 = Yes, 0 = No
            "payment_channel_link": "https://t.me/telegram",
            "welcome_channel_link": "", # Optional
            "multi_account": "0" # 0 = Disabled
        }
        for k, v in defaults.items():
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
        
        # Channels Table (Mandatory Joins)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT,
                channel_name TEXT,
                channel_link TEXT
            )
        """)
        
        # Tasks Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                link TEXT,
                reward REAL,
                type TEXT, -- 'join' or 'visit'
                is_active INTEGER DEFAULT 1
            )
        """)

        # Completed Tasks (Anti-Abuse)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS completed_tasks (
                user_id INTEGER,
                task_id INTEGER,
                UNIQUE(user_id, task_id)
            )
        """)

        # Withdrawals
        await db.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                wallet TEXT,
                status TEXT, -- 'pending', 'approved', 'rejected'
                request_date TEXT
            )
        """)
        await db.commit()

# --- DB Helpers ---
async def get_setting(key):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else None

async def update_setting(key, value):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        await db.commit()

async def get_user(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        return await cursor.fetchone()

async def add_user(user_id, username, referrer_id=None):
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute(
                "INSERT INTO users (user_id, username, joined_date, referrer_id) VALUES (?, ?, ?, ?)",
                (user_id, username, datetime.now(BDT).strftime("%Y-%m-%d %H:%M:%S"), referrer_id)
            )
            await db.commit()
            return True
        except:
            return False

async def update_balance(user_id, amount):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, user_id))
        await db.commit()

# ==========================================
# STATES & FSM
# ==========================================
class UserStates(StatesGroup):
    EmojiVerify = State()
    ChannelVerify = State()
    MainMenu = State()
    WithdrawAmount = State()
    WithdrawWallet = State()

class AdminStates(StatesGroup):
    MainMenu = State()
    EditSetting = State()
    AddTaskTitle = State()
    AddTaskLink = State()
    AddTaskReward = State()
    AddChannelId = State()
    AddChannelLink = State()
    Broadcast = State()

# ==========================================
# KEYBOARDS
# ==========================================
def main_menu_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="💰 Tasks"), KeyboardButton(text="🎁 Daily Bonus")],
        [KeyboardButton(text="👤 Account"), KeyboardButton(text="💸 Withdraw")],
        [KeyboardButton(text="📢 Refer"), KeyboardButton(text="💳 Payment Channel")]
    ], resize_keyboard=True)

def admin_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 User Mgmt", callback_data="admin_users"),
         InlineKeyboardButton(text="⚙️ Settings", callback_data="admin_settings")],
        [InlineKeyboardButton(text="📝 Tasks", callback_data="admin_tasks"),
         InlineKeyboardButton(text="📢 Channels", callback_data="admin_channels")],
        [InlineKeyboardButton(text="💸 Withdrawals", callback_data="admin_withdraws"),
         InlineKeyboardButton(text="📣 Broadcast", callback_data="admin_broadcast")]
    ])

# ==========================================
# STEP 1: EMOJI VERIFICATION
# ==========================================
@router.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    
    # Check Referral
    args = message.text.split()
    referrer = None
    if len(args) > 1 and args[1].isdigit():
        referrer = int(args[1])
        if referrer == message.from_user.id: referrer = None

    if not user:
        await add_user(message.from_user.id, message.from_user.username, referrer)
        user = await get_user(message.from_user.id)

    # If banned
    if user['is_banned']:
        return await message.answer("🚫 You are banned from using this bot.")

    # Step 1 Check
    if not user['verified_emoji']:
        emojis = ['🍎', '🍌', '🍒', '🍉', '🍇', '🍓', '😄', '🚀']
        correct = random.choice(emojis)
        random.shuffle(emojis)
        
        await state.update_data(correct_emoji=correct)
        
        buttons = []
        row = []
        for em in emojis[:6]: # Show 6 options
            row.append(InlineKeyboardButton(text=em, callback_data=f"emoji_{em}"))
            if len(row) == 3:
                buttons.append(row)
                row = []
        
        await message.answer(
            f"🔐 <b>Step 1: Emoji Verification</b>\n\nPlease tap the correct emoji to verify you're human: {correct}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )
        await state.set_state(UserStates.EmojiVerify)
        return

    # Step 2 Check
    if not user['verified_channels']:
        await send_channel_check(message, state)
        return

    # Step 3 Main Menu
    await message.answer("👋 Welcome back to the Main Menu!", reply_markup=main_menu_kb())
    await state.set_state(UserStates.MainMenu)

@router.callback_query(UserStates.EmojiVerify, F.data.startswith("emoji_"))
async def emoji_verify(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    correct = data.get('correct_emoji')
    selected = call.data.split("_")[1]

    if selected == correct:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET verified_emoji=1 WHERE user_id=?", (call.from_user.id,))
            await db.commit()
        await call.message.delete()
        await call.answer("✅ Verification Success!")
        await send_channel_check(call.message, state)
    else:
        await call.answer("❌ Wrong Emoji! Try again.", show_alert=True)
        # Reload start to shuffle
        await start_handler(call.message, state)

# ==========================================
# STEP 2: CHANNEL JOIN
# ==========================================
async def send_channel_check(message: Message, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM channels")
        channels = await cursor.fetchall()
    
    if not channels:
        # No channels setup, auto-verify
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET verified_channels=1 WHERE user_id=?", (message.chat.id,))
            await db.commit()
        await message.answer("✅ Account Verified! Opening Main Menu...", reply_markup=main_menu_kb())
        await state.set_state(UserStates.MainMenu)
        return

    kb = []
    for ch in channels:
        kb.append([InlineKeyboardButton(text=f"📢 Join {ch['channel_name']}", url=ch['channel_link'])])
    
    kb.append([InlineKeyboardButton(text="✅ Check Join", callback_data="check_join_all")])
    
    await message.answer(
        "🔐 <b>Step 2: Mandatory Join</b>\n\nYou must join all channels below to continue:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )
    await state.set_state(UserStates.ChannelVerify)

@router.callback_query(UserStates.ChannelVerify, F.data == "check_join_all")
async def check_join_callback(call: CallbackQuery, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM channels")
        channels = await cursor.fetchall()

    not_joined = []
    for ch in channels:
        try:
            # Need to handle channel_id (starts with -100) or username
            chat_member = await bot.get_chat_member(chat_id=ch['channel_id'], user_id=call.from_user.id)
            if chat_member.status not in ['member', 'administrator', 'creator']:
                not_joined.append(ch['channel_name'])
        except Exception as e:
            # If bot is not admin in channel, it might fail. Assume joined or warn admin.
            # strict mode: fail user
            print(f"Error checking channel {ch['channel_id']}: {e}")
            pass

    if not_joined:
        await call.answer("❌ You haven't joined all channels!", show_alert=True)
        return

    # Success
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET verified_channels=1 WHERE user_id=?", (call.from_user.id,))
        await db.commit()
    
    await call.message.delete()
    await call.message.answer("🎉 Verification Complete! Welcome!", reply_markup=main_menu_kb())
    await state.set_state(UserStates.MainMenu)

# ==========================================
# USER PANEL HANDLERS (Step 3)
# ==========================================

@router.message(F.text == "💰 Tasks")
async def task_list(message: Message):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        # Get active tasks not done by user
        cursor = await db.execute("""
            SELECT * FROM tasks 
            WHERE is_active=1 
            AND id NOT IN (SELECT task_id FROM completed_tasks WHERE user_id=?)
        """, (message.from_user.id,))
        tasks = await cursor.fetchall()

    if not tasks:
        await message.answer("📂 No tasks available right now. Check back later!")
        return

    for task in tasks:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Go to Task", url=task['link'])],
            [InlineKeyboardButton(text=f"✅ Claim ৳{task['reward']}", callback_data=f"claim_task_{task['id']}_{task['reward']}")]
        ])
        await message.answer(
            f"📋 <b>{task['title']}</b>\n\nReward: ৳{task['reward']}\nType: {task['type']}",
            reply_markup=kb
        )

@router.callback_query(F.data.startswith("claim_task_"))
async def claim_task(call: CallbackQuery):
    # Format: claim_task_ID_REWARD
    _, _, task_id, reward = call.data.split("_")
    reward = float(reward)
    
    # Verify not claimed again (Anti-Race Condition)
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT * FROM completed_tasks WHERE user_id=? AND task_id=?", (call.from_user.id, task_id))
        if await cursor.fetchone():
            await call.answer("❌ Already claimed!", show_alert=True)
            await call.message.delete()
            return
        
        # Add to completed
        await db.execute("INSERT INTO completed_tasks (user_id, task_id) VALUES (?, ?)", (call.from_user.id, task_id))
        # Add Balance
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (reward, call.from_user.id))
        await db.commit()
    
    await call.answer(f"✅ claimed ৳{reward}!", show_alert=True)
    await call.message.delete()

@router.message(F.text == "🎁 Daily Bonus")
async def daily_bonus(message: Message):
    user = await get_user(message.from_user.id)
    last_claim = user['last_bonus_claim']
    bonus_amount = float(await get_setting("daily_bonus"))
    
    can_claim = False
    if not last_claim:
        can_claim = True
    else:
        last_date = datetime.strptime(last_claim, "%Y-%m-%d %H:%M:%S")
        if datetime.now() > last_date + timedelta(hours=24):
            can_claim = True
    
    if can_claim:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET balance = balance + ?, last_bonus_claim = ? WHERE user_id=?", (bonus_amount, now_str, message.from_user.id))
            await db.commit()
        await message.answer(f"🎉 <b>Daily Bonus Claimed!</b>\n\n➕ Received: ৳{bonus_amount}")
    else:
        await message.answer("⏳ <b>Come back tomorrow!</b>\nYou can claim bonus once every 24 hours.")

@router.message(F.text == "👤 Account")
async def account_info(message: Message):
    user = await get_user(message.from_user.id)
    
    # Get referrer count
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users WHERE referrer_id=?", (message.from_user.id,))
        ref_count = (await cursor.fetchone())[0]

    msg = f"""👤 <b>User Profile</b>

🆔 ID: <code>{user['user_id']}</code>
👤 Name: {user['username'] or 'Unknown'}
💰 Balance: <b>৳{user['balance']:.2f}</b>
👥 Referrals: {ref_count}

📅 Joined: {user['joined_date']}"""
    await message.answer(msg)

@router.message(F.text == "📢 Refer")
async def refer_info(message: Message):
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={message.from_user.id}"
    reward = await get_setting("refer_reward")
    
    await message.answer(
        f"📢 <b>Invite Friends & Earn</b>\n\n"
        f"Get ৳{reward} for every friend who joins and verifies!\n\n"
        f"🔗 <b>Your Link:</b>\n{ref_link}",
        disable_web_page_preview=True
    )

@router.message(F.text == "💳 Payment Channel")
async def payment_channel_link(message: Message):
    link = await get_setting("payment_channel_link")
    await message.answer(f"💳 Join our payment proof channel:\n{link}")

# --- Withdraw System ---
@router.message(F.text == "💸 Withdraw")
async def withdraw_start(message: Message, state: FSMContext):
    enabled = await get_setting("withdraw_enabled")
    if enabled == "0":
        await message.answer("⚠️ Withdrawals are currently disabled by Admin.")
        return

    user = await get_user(message.from_user.id)
    min_wd = float(await get_setting("min_withdraw"))
    
    if user['balance'] < min_wd:
        await message.answer(f"❌ <b>Insufficient Balance</b>\n\nMinimum withdraw: ৳{min_wd}\nYour Balance: ৳{user['balance']:.2f}")
        return

    await message.answer("💸 <b>Enter Amount to Withdraw:</b>\n(Send only numbers, e.g., 50)")
    await state.set_state(UserStates.WithdrawAmount)

@router.message(UserStates.WithdrawAmount)
async def withdraw_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
    except:
        await message.answer("❌ Invalid amount. Please enter a number.")
        return

    user = await get_user(message.from_user.id)
    min_wd = float(await get_setting("min_withdraw"))

    if amount < min_wd or amount > user['balance']:
        await message.answer("❌ Amount is lower than minimum or higher than your balance.")
        return

    await state.update_data(wd_amount=amount)
    await message.answer("💳 <b>Enter your Wallet Number (Nagad/Bkash):</b>")
    await state.set_state(UserStates.WithdrawWallet)

@router.message(UserStates.WithdrawWallet)
async def withdraw_confirm(message: Message, state: FSMContext):
    wallet = message.text
    data = await state.get_data()
    amount = data['wd_amount']
    
    # Deduct Balance
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET balance = balance - ? WHERE user_id=?", (amount, message.from_user.id))
        # Create Request
        await db.execute(
            "INSERT INTO withdrawals (user_id, amount, wallet, status, request_date) VALUES (?, ?, ?, ?, ?)",
            (message.from_user.id, amount, wallet, "pending", datetime.now().strftime("%Y-%m-%d %H:%M"))
        )
        await db.commit()
    
    await message.answer("✅ <b>Withdrawal Requested!</b>\nPlease wait for admin approval.", reply_markup=main_menu_kb())
    await state.clear()
    
    # Notify Admin
    await bot.send_message(ADMIN_ID, f"🔔 <b>New Withdraw Request!</b>\nUser: {message.from_user.id}\nAmount: ৳{amount}\nWallet: {wallet}")

# ==========================================
# ADMIN PANEL (CRITICAL LOGIC)
# ==========================================
@router.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🛠 <b>Admin Panel</b>", reply_markup=admin_menu_kb())

@router.callback_query(F.data == "admin_home")
async def admin_home(call: CallbackQuery):
    await call.message.edit_text("🛠 <b>Admin Panel</b>", reply_markup=admin_menu_kb())

# --- 1. User Management ---
@router.callback_query(F.data == "admin_users")
async def admin_users(call: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        total_users = (await cursor.fetchone())[0]
    
    await call.message.edit_text(
        f"👥 <b>User Management</b>\n\nTotal Users: {total_users}\n\nTo ban/unban or check specific user, use DB tools or extend this panel.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="admin_home")]])
    )

# --- 2. Settings Management ---
@router.callback_query(F.data == "admin_settings")
async def admin_settings_menu(call: CallbackQuery):
    settings = {
        "refer_reward": "Refer Reward",
        "min_withdraw": "Min Withdraw",
        "daily_bonus": "Daily Bonus",
        "withdraw_enabled": "Toggle Withdraw",
        "payment_channel_link": "Pay Channel Link"
    }
    kb = []
    for k, v in settings.items():
        kb.append([InlineKeyboardButton(text=f"✏️ Edit {v}", callback_data=f"edit_set_{k}")])
    kb.append([InlineKeyboardButton(text="🔙 Back", callback_data="admin_home")])
    
    await call.message.edit_text("⚙️ <b>Settings Manager</b>\nSelect a setting to edit:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("edit_set_"))
async def edit_setting_ask(call: CallbackQuery, state: FSMContext):
    key = call.data.replace("edit_set_", "")
    curr_val = await get_setting(key)
    await state.update_data(setting_key=key)
    await call.message.edit_text(f"📝 <b>Editing: {key}</b>\nCurrent Value: <code>{curr_val}</code>\n\nSend new value:", reply_markup=None)
    await state.set_state(AdminStates.EditSetting)

@router.message(AdminStates.EditSetting)
async def edit_setting_save(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data['setting_key']
    await update_setting(key, message.text)
    await message.answer(f"✅ Setting <b>{key}</b> updated to: {message.text}")
    await state.clear()
    await admin_panel(message)

# --- 3. Task Management ---
@router.callback_query(F.data == "admin_tasks")
async def admin_tasks_menu(call: CallbackQuery):
    kb = [
        [InlineKeyboardButton(text="➕ Add New Task", callback_data="add_task")],
        [InlineKeyboardButton(text="🗑 Delete Task", callback_data="del_task_list")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="admin_home")]
    ]
    await call.message.edit_text("📝 <b>Task Management</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "add_task")
async def add_task_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("👉 Send <b>Task Title</b>:")
    await state.set_state(AdminStates.AddTaskTitle)

@router.message(AdminStates.AddTaskTitle)
async def add_task_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text)
    await message.answer("👉 Send <b>Task Link</b> (URL):")
    await state.set_state(AdminStates.AddTaskLink)

@router.message(AdminStates.AddTaskLink)
async def add_task_link(message: Message, state: FSMContext):
    await state.update_data(link=message.text)
    await message.answer("👉 Send <b>Reward Amount</b> (e.g., 2.5):")
    await state.set_state(AdminStates.AddTaskReward)

@router.message(AdminStates.AddTaskReward)
async def add_task_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        reward = float(message.text)
    except:
        await message.answer("❌ Invalid number. Task creation cancelled.")
        await state.clear()
        return

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO tasks (title, link, reward, type) VALUES (?, ?, ?, ?)",
            (data['title'], data['link'], reward, 'visit')
        )
        await db.commit()
    
    await message.answer("✅ <b>Task Added Successfully!</b>")
    await state.clear()
    await admin_panel(message)

@router.callback_query(F.data == "del_task_list")
async def del_task_list(call: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM tasks WHERE is_active=1")
        tasks = await cursor.fetchall()
    
    kb = []
    for t in tasks:
        kb.append([InlineKeyboardButton(text=f"🗑 {t['title']} (৳{t['reward']})", callback_data=f"del_task_{t['id']}")])
    kb.append([InlineKeyboardButton(text="🔙 Back", callback_data="admin_tasks")])
    
    await call.message.edit_text("🗑 <b>Select Task to Delete:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("del_task_"))
async def delete_task_action(call: CallbackQuery):
    tid = call.data.split("_")[2]
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE tasks SET is_active=0 WHERE id=?", (tid,))
        await db.commit()
    await call.answer("✅ Task Deleted!")
    await admin_tasks_menu(call)

# --- 4. Channel Management ---
@router.callback_query(F.data == "admin_channels")
async def admin_channels_menu(call: CallbackQuery):
    kb = [
        [InlineKeyboardButton(text="➕ Add Channel", callback_data="add_channel")],
        [InlineKeyboardButton(text="🗑 Delete Channel", callback_data="del_channel_list")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="admin_home")]
    ]
    await call.message.edit_text("📢 <b>Channel Management</b>\nThese channels are mandatory for users.", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "add_channel")
async def add_channel_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("👉 Send <b>Channel ID</b> (start with -100) or <b>@username</b>:\n\n*Make sure bot is ADMIN in that channel!*")
    await state.set_state(AdminStates.AddChannelId)

@router.message(AdminStates.AddChannelId)
async def add_channel_id(message: Message, state: FSMContext):
    await state.update_data(cid=message.text)
    await message.answer("👉 Send <b>Channel Invite Link</b>:")
    await state.set_state(AdminStates.AddChannelLink)

@router.message(AdminStates.AddChannelLink)
async def add_channel_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    # Try to get channel info
    try:
        chat = await bot.get_chat(data['cid'])
        title = chat.title
    except:
        title = "Unknown Channel"
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO channels (channel_id, channel_name, channel_link) VALUES (?, ?, ?)",
                         (data['cid'], title, message.text))
        await db.commit()
    
    await message.answer(f"✅ <b>Channel '{title}' Added!</b>")
    await state.clear()
    await admin_panel(message)

@router.callback_query(F.data == "del_channel_list")
async def del_channel_list(call: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM channels")
        chans = await cursor.fetchall()
    
    kb = []
    for c in chans:
        kb.append([InlineKeyboardButton(text=f"🗑 {c['channel_name']}", callback_data=f"del_ch_{c['id']}")])
    kb.append([InlineKeyboardButton(text="🔙 Back", callback_data="admin_channels")])
    
    await call.message.edit_text("🗑 <b>Select Channel to Remove:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("del_ch_"))
async def delete_channel_action(call: CallbackQuery):
    cid = call.data.split("_")[2]
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM channels WHERE id=?", (cid,))
        await db.commit()
    await call.answer("✅ Channel Removed!")
    await admin_channels_menu(call)

# --- 5. Withdrawals Management ---
@router.callback_query(F.data == "admin_withdraws")
async def admin_withdraws(call: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM withdrawals WHERE status='pending'")
        reqs = await cursor.fetchall()
    
    if not reqs:
        await call.message.edit_text("💸 <b>No Pending Withdrawals</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="admin_home")]]))
        return
    
    # Show first pending
    req = reqs[0]
    kb = [
        [InlineKeyboardButton(text="✅ Approve", callback_data=f"wd_pay_{req['id']}")],
        [InlineKeyboardButton(text="❌ Reject", callback_data=f"wd_rej_{req['id']}")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="admin_home")]
    ]
    
    await call.message.edit_text(
        f"💸 <b>Withdraw Request ({len(reqs)} pending)</b>\n\n"
        f"👤 User ID: <code>{req['user_id']}</code>\n"
        f"💰 Amount: ৳{req['amount']}\n"
        f"💳 Wallet: <code>{req['wallet']}</code>\n"
        f"📅 Date: {req['request_date']}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )

@router.callback_query(F.data.startswith("wd_pay_"))
async def approve_withdraw(call: CallbackQuery):
    wid = call.data.split("_")[2]
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE withdrawals SET status='approved' WHERE id=?", (wid,))
        await db.commit()
        
        # Get user to notify
        cursor = await db.execute("SELECT user_id, amount FROM withdrawals WHERE id=?", (wid,))
        row = await cursor.fetchone()
    
    if row:
        try:
            await bot.send_message(row[0], f"✅ <b>Withdrawal Approved!</b>\n\n৳{row[1]} has been sent to your wallet.")
        except: pass

    await call.answer("✅ Approved!")
    await admin_withdraws(call) # Refresh

@router.callback_query(F.data.startswith("wd_rej_"))
async def reject_withdraw(call: CallbackQuery):
    wid = call.data.split("_")[2]
    async with aiosqlite.connect(DB_NAME) as db:
        # Refund balance
        cursor = await db.execute("SELECT user_id, amount FROM withdrawals WHERE id=?", (wid,))
        row = await cursor.fetchone()
        if row:
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (row[1], row[0]))
            await db.execute("UPDATE withdrawals SET status='rejected' WHERE id=?", (wid,))
            await db.commit()
            try:
                await bot.send_message(row[0], f"❌ <b>Withdrawal Rejected.</b>\n\n৳{row[1]} returned to balance.")
            except: pass
            
    await call.answer("❌ Rejected and Refunded!")
    await admin_withdraws(call)

# --- 6. Broadcast ---
@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("📣 <b>Broadcast</b>\n\nSend the message (Text/Image) you want to broadcast to ALL users:")
    await state.set_state(AdminStates.Broadcast)

@router.message(AdminStates.Broadcast)
async def admin_broadcast_send(message: Message, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT user_id FROM users")
        users = await cursor.fetchall()
    
    count = 0
    fail = 0
    status_msg = await message.answer(f"🚀 Starting broadcast to {len(users)} users...")
    
    for row in users:
        try:
            await message.copy_to(chat_id=row[0])
            count += 1
        except:
            fail += 1
        # Avoid flood limits
        if count % 20 == 0:
            await asyncio.sleep(1)
            
    await status_msg.edit_text(f"✅ <b>Broadcast Complete!</b>\n\nSent: {count}\nFailed: {fail}")
    await state.clear()
    await admin_panel(message)


# ==========================================
# RENDER SERVER (WEBHOOK / KEEP-ALIVE)
# ==========================================
async def handle_root(request):
    return web.Response(text=f"Bot is running. {datetime.now()}")

async def start_server():
    app = web.Application()
    app.add_routes([web.get('/', handle_root)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Web server running on port {PORT}")

# ==========================================
# MAIN EXECUTION
# ==========================================
async def main():
    await init_db()
    
    # Start Web Server for Render
    await start_server()
    
    print("🤖 Bot Started...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())
