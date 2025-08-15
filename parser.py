from telethon import TelegramClient, events, sync
import asyncio

# Введи сюда свои данные от Telegram API
api_id = 241421
api_hash = 'YOUR_API_HASH'
session_file = '@fuckpunch.session'  # Имя файла сессии

async def main():
    client = TelegramClient(session_file, api_id, api_hash)
    await client.start()

    print("Получаем список диалогов...")
    dialogs = await client.get_dialogs()

    # Выведем список чатов с номерами
    for i, dialog in enumerate(dialogs):
        name = dialog.name
        print(f"{i}: {name}")

    # Выбор чата
    chat_index = int(input("Выбери номер чата: "))
    selected_dialog = dialogs[chat_index]
    print(f"Выбран чат: {selected_dialog.name}")

    # Получаем последние 100 сообщений чата
    messages = await client.get_messages(selected_dialog, limit=100)

    users = set()
    for msg in messages:
        if msg.sender:
            # Добавим юзернейм или имя если нет юзернейма
            username = msg.sender.username if msg.sender.username else msg.sender.first_name
            users.add(username)

    print("Пользователи, писавшие в чате:")
    for user in users:
        print(user)

    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
