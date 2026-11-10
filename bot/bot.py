import os
from dotenv import load_dotenv
import phonenumbers
from phonenumbers import geocoder
import logging
import sqlite3
import requests
import asyncio
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.callback_data import CallbackData
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

# --- LOAD HIDDEN SECURITY ENVIRONMENT VARIABLES ---
load_dotenv()

# --- SYSTEM CONFIGURATION ---
API_TOKEN = os.getenv("API_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8176124462"))
PANEL_API_URL = os.getenv("PANEL_API_URL", "http://172.17.0.1:5001/api")
API_SECRET_KEY = os.getenv("API_SECRET_KEY")
DB_FILE = os.getenv("DB_FILE", "/app/weido_vault.db")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage()) 

user_cb = CallbackData('assign', 'ext')
acl_cb = CallbackData('acl', 'action', 'user_id')
panel_cb = CallbackData('panel', 'action', 'tgt')

class AllocateState(StatesGroup):
    waiting_for_country = State()
    waiting_for_range = State()
    waiting_for_count = State()

class AdminRouteState(StatesGroup):
    waiting_for_country = State()
    waiting_for_range = State()
    waiting_for_count = State()

def get_flag(region_code):
    if not region_code: return "📍"
    return chr(ord(region_code[0]) + 127397) + chr(ord(region_code[1]) + 127397)

def init_vault_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS number_pool (id INTEGER PRIMARY KEY, phone_number TEXT UNIQUE, country TEXT, range_format TEXT, added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS access_control (user_id INTEGER PRIMARY KEY, username TEXT, status TEXT DEFAULT 'pending', lock_expiry TIMESTAMP)''')
    conn.commit()
    conn.close()

init_vault_db()

async def vault_auto_cleaner():
    while True:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM number_pool WHERE added_at < datetime('now', '-1 day')")
            conn.commit()
            conn.close()
        except Exception as e:
            logging.error(f"Cleaner Error: {e}")
        await asyncio.sleep(3600)

def get_master_menu(user_id):
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    if user_id == ADMIN_ID:
        kb.row(KeyboardButton("📁 Upload Session"), KeyboardButton("💰 My Stats"))
        kb.row(KeyboardButton("📤 Allocate Numbers"), KeyboardButton("🗑️ Clear Database"))
        kb.row(KeyboardButton("👑 Admin Control"))
    else:
        kb.row(KeyboardButton("💰 My Stats"), KeyboardButton("📤 Allocate Numbers"))
    return kb

def check_access(user_id):
    if user_id == ADMIN_ID: return "approved"
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM access_control WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else "unregistered"

@dp.message_handler(lambda message: check_access(message.from_user.id) != "approved", state="*")
async def unauthorized_access(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT status, lock_expiry FROM access_control WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    now = datetime.now()

    if not row:
        cursor.execute("INSERT INTO access_control (user_id, username, status) VALUES (?, ?, 'pending')", (user_id, username))
        conn.commit()
        await message.reply("🔒 Access Denied. Your authorization request has been sent to the System Architect.")
        await send_admin_approval(user_id, username)
    else:
        status, lock_expiry = row
        if status == 'pending':
            await message.reply("⏳ Your request is currently pending Admin review.")
        elif status == 'denied':
            if lock_expiry:
                expiry_time = datetime.strptime(lock_expiry, "%Y-%m-%d %H:%M:%S.%f")
                if now < expiry_time:
                    remaining = expiry_time - now
                    hours, remainder = divmod(remaining.seconds, 3600)
                    await message.reply(f"🚫 Request Rejected. You are locked out. Try again in {hours} hours and {remainder // 60} minutes.")
                else:
                    cursor.execute("UPDATE access_control SET status='pending', lock_expiry=NULL WHERE user_id=?", (user_id,))
                    conn.commit()
                    await message.reply("⏳ Previous lock expired. A new authorization request has been sent to the System Architect.")
                    await send_admin_approval(user_id, username)
    conn.close()

async def send_admin_approval(user_id, username):
    inline_kb = InlineKeyboardMarkup(row_width=2)
    inline_kb.insert(InlineKeyboardButton(text="✅ Approve", callback_data=acl_cb.new(action='approve', user_id=user_id)))
    inline_kb.insert(InlineKeyboardButton(text="❌ Deny (24h Lock)", callback_data=acl_cb.new(action='deny', user_id=user_id)))
    await bot.send_message(ADMIN_ID, f"🔔 **Access Request**\nUser: @{username}\nID: `{user_id}`", reply_markup=inline_kb, parse_mode="Markdown")

@dp.callback_query_handler(acl_cb.filter(), state="*")
async def handle_admin_decision(query: types.CallbackQuery, callback_data: dict):
    if query.from_user.id != ADMIN_ID: return
    action = callback_data['action']
    target_user = int(callback_data['user_id'])
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    if action == 'approve':
        cursor.execute("UPDATE access_control SET status='approved', lock_expiry=NULL WHERE user_id=?", (target_user,))
        await bot.send_message(target_user, "🎉 **Access Granted!** Type /start to initialize your terminal.", parse_mode="Markdown")
        await query.message.edit_text(f"✅ Approved User {target_user}")
    else:
        lock_time = datetime.now() + timedelta(days=1)
        cursor.execute("UPDATE access_control SET status='denied', lock_expiry=? WHERE user_id=?", (lock_time, target_user))
        await bot.send_message(target_user, "❌ The System Architect has denied your request. You are locked out for 24 hours.")
        await query.message.edit_text(f"❌ Denied & Locked User {target_user}")
        
    conn.commit()
    conn.close()
    await query.answer()

@dp.message_handler(commands=['start'], state="*")
async def send_welcome(message: types.Message, state: FSMContext):
    await state.finish()
    await message.reply("👋 Weido Bot Master Active. Control center initialized.", reply_markup=get_master_menu(message.from_user.id))

@dp.message_handler(lambda message: message.text == "💰 My Stats", state="*")
async def cmd_status_report(message: types.Message, state: FSMContext):
    await state.finish()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT country, COUNT(*) FROM number_pool GROUP BY country")
    stats = cursor.fetchall()
    conn.close()

    if not stats:
        await message.reply("📊 --- VAULT STATUS REPORT ---\n\n🔹 Vault is currently empty.")
        return

    report = "📊 --- VAULT STATUS REPORT ---\n\n"
    for row in stats: report += f"{row[0]}: {row[1]} numbers available\n"
    await message.reply(report)

@dp.message_handler(lambda message: message.text == "📁 Upload Session", state="*")
async def cmd_upload_help(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await state.finish()
    await message.reply("📂 *How to Upload:*\n\nSimply drag and drop your `.txt` files directly into this chat. The bot will categorize them by Country and Range.", parse_mode="Markdown")

@dp.message_handler(lambda message: message.text == "🗑️ Clear Database", state="*")
async def cmd_clear_database(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    import time as _t
    _pending = globals().setdefault('PENDING_CLEAR', {})
    if _t.time() - _pending.get(message.from_user.id, 0) > 30:
        _pending[message.from_user.id] = _t.time()
        await message.reply("⚠️ This will delete ALL numbers in the vault.\nPress Clear Database again within 30 seconds to confirm.")
        return
    _pending.pop(message.from_user.id, None)
    await state.finish()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM number_pool")
    conn.commit()
    conn.close()
    await message.reply("🗑️ Local Vault Database has been completely reset.")

@dp.message_handler(content_types=['document'], state="*")
async def handle_incoming_txt(message: types.Message, state: FSMContext):
    await state.finish()
    if message.from_user.id != ADMIN_ID:
        await message.reply("❌ Security Alert: Only the System Architect can upload `.txt` vault databases.")
        return
        
    if not message.document.file_name.endswith('.txt'):
        await message.reply("✅ Non-TXT Document cached safely.")
        return

    file_obj = await bot.get_file(message.document.file_id)
    file_buffer = await bot.download_file(file_obj.file_path)
    file_content = file_buffer.read().decode('utf-8')

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    lines = file_content.splitlines()
    
    added_count = 0
    country_stats = {}

    for line in lines:
        raw_num = line.strip().replace('+', '').replace(' ', '')
        if not raw_num.isdigit(): continue
        try:
            parsed_num = phonenumbers.parse("+" + raw_num)
            c_name = geocoder.country_name_for_number(parsed_num, "en")
            region = phonenumbers.region_code_for_number(parsed_num)
            if not c_name: c_name = "Unknown"
            flag = get_flag(region)
            full_country = f"{flag} {c_name}"
            range_format = raw_num[:6] + ("X" * (len(raw_num) - 6)) if len(raw_num) > 6 else raw_num + "X"
        except:
            full_country = "📍 Unknown"
            range_format = "Unknown"

        try:
            cursor.execute("INSERT INTO number_pool (phone_number, country, range_format) VALUES (?, ?, ?)", (raw_num, full_country, range_format))
            added_count += 1
            country_stats[full_country] = country_stats.get(full_country, 0) + 1
        except sqlite3.IntegrityError:
            pass

    conn.commit()
    conn.close()
    
    reply_msg = f"✅ Sent: {added_count} numbers ✅\n*(These will auto-expire in 24 hours)*\n\n*Breakdown:*\n"
    for c_name, count in country_stats.items(): reply_msg += f"{c_name}: {count} loaded\n"
    await message.reply(reply_msg, parse_mode="Markdown")

@dp.message_handler(lambda message: message.text == "👑 Admin Control", state="*")
async def cmd_admin_control(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await state.finish()
    
    kb = InlineKeyboardMarkup(row_width=1)
    kb.insert(InlineKeyboardButton("📞 Route to Zoiper (1001)", callback_data=panel_cb.new(action='route', tgt='0')))
    kb.insert(InlineKeyboardButton("👥 Manage Users (Revoke)", callback_data=panel_cb.new(action='manage', tgt='0')))
    kb.insert(InlineKeyboardButton("❌ Close Menu", callback_data=panel_cb.new(action='close', tgt='0')))
    
    await message.reply("👑 **Admin Control Center**\nSelect an operation:", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query_handler(panel_cb.filter(), state="*")
async def handle_admin_panel(query: types.CallbackQuery, callback_data: dict, state: FSMContext):
    if query.from_user.id != ADMIN_ID: return
    action = callback_data['action']
    
    if action == 'close':
        await query.message.delete()
        
    elif action == 'route':
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT country FROM number_pool WHERE country IS NOT NULL")
        countries = cursor.fetchall()
        conn.close()

        if not countries:
            await query.message.edit_text("❌ Vault is empty.")
            return

        markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        for c in countries: markup.insert(KeyboardButton(c[0]))
        markup.row(KeyboardButton("❌ Cancel"))

        await query.message.delete()
        await bot.send_message(ADMIN_ID, "👑 Zoiper Route Init.\n\nSelect target Country:", reply_markup=markup)
        await AdminRouteState.waiting_for_country.set()
        
    elif action == 'manage':
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, username FROM access_control WHERE status='approved'")
        users = cursor.fetchall()
        conn.close()
        
        if not users:
            await query.answer("No active employees found.", show_alert=True)
            return
            
        kb = InlineKeyboardMarkup(row_width=1)
        for u in users:
            kb.insert(InlineKeyboardButton(f"❌ Kick @{u[1]}", callback_data=panel_cb.new(action='kick', tgt=str(u[0]))))
        kb.insert(InlineKeyboardButton("🔙 Back", callback_data=panel_cb.new(action='back', tgt='0')))
        
        await query.message.edit_text("👥 **Active Employees:**\nClick to immediately revoke access.", reply_markup=kb, parse_mode="Markdown")

    elif action == 'kick':
        tgt_user = int(callback_data['tgt'])
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM access_control WHERE user_id=?", (tgt_user,))
        conn.commit()
        conn.close()
        
        await query.answer("User access revoked!", show_alert=True)
        try:
            await bot.send_message(tgt_user, "❌ Your access to the Weido Bot has been revoked by the System Architect.")
        except:
            pass
        await query.message.delete()

    elif action == 'back':
        kb = InlineKeyboardMarkup(row_width=1)
        kb.insert(InlineKeyboardButton("📞 Route to Zoiper (1001)", callback_data=panel_cb.new(action='route', tgt='0')))
        kb.insert(InlineKeyboardButton("👥 Manage Users (Revoke)", callback_data=panel_cb.new(action='manage', tgt='0')))
        kb.insert(InlineKeyboardButton("❌ Close Menu", callback_data=panel_cb.new(action='close', tgt='0')))
        await query.message.edit_text("👑 **Admin Control Center**\nSelect an operation:", reply_markup=kb, parse_mode="Markdown")

@dp.message_handler(lambda message: message.text == "📤 Allocate Numbers", state="*")
async def cmd_allocate_start(message: types.Message, state: FSMContext):
    await state.finish()
    try:
        response = requests.get(f"{PANEL_API_URL}/get_users", headers={"X-API-KEY": API_SECRET_KEY})
        if response.status_code != 200:
            await message.reply("❌ Error: Web panel API rejected connection.")
            return

        user_list = response.json().get("users", [])
        if not user_list:
            await message.reply("📐 No users found in the Web Panel database.")
            return

        inline_kb = InlineKeyboardMarkup(row_width=2)
        for ext in user_list:
            if str(ext) == "1001":
                inline_kb.insert(InlineKeyboardButton(text=f"🔒 User 1001", callback_data="locked_1001"))
            else:
                inline_kb.insert(InlineKeyboardButton(text=f"👤 User {ext}", callback_data=user_cb.new(ext=ext)))
            
        await message.reply("Select target user from the active Web Panel database:", reply_markup=inline_kb)
    except Exception as e:
        await message.reply(f"❌ Failed to reach Web Panel API: {e}")

@dp.callback_query_handler(lambda c: c.data == 'locked_1001', state="*")
async def handle_locked_admin(query: types.CallbackQuery):
    await query.answer("🔒 User 1001 is reserved. Use the Admin Control Center.", show_alert=True)

@dp.callback_query_handler(user_cb.filter(), state="*")
async def handle_user_selection(query: types.CallbackQuery, callback_data: dict, state: FSMContext):
    ext = callback_data['ext']
    await state.update_data(target_user=ext)
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT country FROM number_pool WHERE country IS NOT NULL")
    countries = cursor.fetchall()
    conn.close()

    if not countries:
        await bot.send_message(query.from_user.id, "❌ Vault is empty.")
        await state.finish()
        return

    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    for c in countries: markup.insert(KeyboardButton(c[0]))
    markup.row(KeyboardButton("❌ Cancel"))

    await bot.send_message(query.from_user.id, f"👤 User {ext} selected.\n\nSelect target Country:", reply_markup=markup)
    await AllocateState.waiting_for_country.set()
    await query.answer()

@dp.message_handler(state=AllocateState.waiting_for_country)
async def process_country(message: types.Message, state: FSMContext):
    if message.text == "❌ Cancel":
        await state.finish()
        await message.reply("Action cancelled.", reply_markup=get_master_menu(message.from_user.id))
        return

    await state.update_data(target_country=message.text)
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT range_format FROM number_pool WHERE country=?", (message.text,))
    ranges = cursor.fetchall()
    conn.close()

    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    for r in ranges: markup.insert(KeyboardButton(r[0]))
    markup.row(KeyboardButton("❌ Cancel"))

    await message.reply(f"{message.text} selected.\nChoose prefix range:", reply_markup=markup)
    await AllocateState.waiting_for_range.set()

@dp.message_handler(state=AllocateState.waiting_for_range)
async def process_range(message: types.Message, state: FSMContext):
    if message.text == "❌ Cancel":
        await state.finish()
        await message.reply("Action cancelled.", reply_markup=get_master_menu(message.from_user.id))
        return

    await state.update_data(target_range=message.text)
    user_data = await state.get_data()

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT count(*) FROM number_pool WHERE country=? AND range_format=?", (user_data.get("target_country"), message.text))
    available = cursor.fetchone()[0]
    conn.close()

    await message.reply(f"📐 Range {message.text} selected.\n✅ Available: {available}\n\nEnter count to allocate to User:", reply_markup=get_master_menu(message.from_user.id))
    await AllocateState.waiting_for_count.set()

@dp.message_handler(state=AllocateState.waiting_for_count)
async def process_allocation_count(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await state.finish()
        await message.reply("❌ Cancelled: Invalid number.", reply_markup=get_master_menu(message.from_user.id))
        return

    requested_count = int(message.text)
    user_data = await state.get_data()
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, phone_number FROM number_pool WHERE country=? AND range_format=? ORDER BY RANDOM() LIMIT ?", (user_data.get("target_country"), user_data.get("target_range"), requested_count))
    rows = cursor.fetchall()

    if not rows:
        conn.close()
        await state.finish()
        await message.reply("❌ Allocation Failed: Vault is empty or lacking numbers.", reply_markup=get_master_menu(message.from_user.id))
        return

    db_ids = [str(row[0]) for row in rows]
    raw_numbers = [row[1] for row in rows]

    try:
        response = requests.post(f"{PANEL_API_URL}/bot_assign", json={"extension": user_data.get("target_user"), "numbers": raw_numbers}, headers={"X-API-KEY": API_SECRET_KEY})
        if response.status_code == 200:
            cursor.execute(f"DELETE FROM number_pool WHERE id IN ({','.join(db_ids)})")
            conn.commit()
            await message.reply(f"🚀 Success! Sent {len(raw_numbers)} numbers to User {user_data.get('target_user')}.")
            await bot.send_message(message.from_user.id, "\n".join([f"+{num}" for num in raw_numbers]))
        else:
            await message.reply(f"❌ API Rejected: {response.text}")
    except Exception as e:
        await message.reply(f"❌ API Transmission Failure: {e}")
    finally:
        conn.close()
        await state.finish()

@dp.message_handler(state=AdminRouteState.waiting_for_country)
async def admin_process_country(message: types.Message, state: FSMContext):
    if message.text == "❌ Cancel":
        await state.finish()
        await message.reply("Action cancelled.", reply_markup=get_master_menu(message.from_user.id))
        return

    await state.update_data(target_country=message.text)
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT range_format FROM number_pool WHERE country=?", (message.text,))
    ranges = cursor.fetchall()
    conn.close()

    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    for r in ranges: markup.insert(KeyboardButton(r[0]))
    markup.row(KeyboardButton("❌ Cancel"))

    await message.reply(f"{message.text} selected.\nChoose prefix range:", reply_markup=markup)
    await AdminRouteState.waiting_for_range.set()

@dp.message_handler(state=AdminRouteState.waiting_for_range)
async def admin_process_range(message: types.Message, state: FSMContext):
    if message.text == "❌ Cancel":
        await state.finish()
        await message.reply("Action cancelled.", reply_markup=get_master_menu(message.from_user.id))
        return

    await state.update_data(target_range=message.text)
    user_data = await state.get_data()

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT count(*) FROM number_pool WHERE country=? AND range_format=?", (user_data.get("target_country"), message.text))
    available = cursor.fetchone()[0]
    conn.close()

    await message.reply(f"📐 Range {message.text} selected.\n✅ Available: {available}\n\nEnter count to deduct for Zoiper 1001:", reply_markup=get_master_menu(message.from_user.id))
    await AdminRouteState.waiting_for_count.set()

@dp.message_handler(state=AdminRouteState.waiting_for_count)
async def admin_process_allocation_count(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await state.finish()
        await message.reply("❌ Cancelled: Invalid number.", reply_markup=get_master_menu(message.from_user.id))
        return

    requested_count = int(message.text)
    user_data = await state.get_data()
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, phone_number FROM number_pool WHERE country=? AND range_format=? ORDER BY RANDOM() LIMIT ?", (user_data.get("target_country"), user_data.get("target_range"), requested_count))
    rows = cursor.fetchall()

    if not rows:
        conn.close()
        await state.finish()
        await message.reply("❌ Routing Failed: Vault is empty or lacking numbers.", reply_markup=get_master_menu(message.from_user.id))
        return

    db_ids = [str(row[0]) for row in rows]
    raw_numbers = [row[1] for row in rows]

    cursor.execute(f"DELETE FROM number_pool WHERE id IN ({','.join(db_ids)})")
    conn.commit()
    conn.close()
    await state.finish()
    
    await message.reply(f"👑 Implicit Route Success! {len(raw_numbers)} numbers deducted from vault.")
    await bot.send_message(message.from_user.id, "\n".join([f"+{num}" for num in raw_numbers]))

async def on_startup(dp):
    asyncio.create_task(vault_auto_cleaner())
    print("Bot running with Dynamic Environment Variables...")

if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
