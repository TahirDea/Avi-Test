# FileStream/plugins/stream.py

import asyncio
from typing import List, Optional, Dict, Union
from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message

from FileStream.bot import FileStream, multi_clients
from FileStream.utils.bot_utils import (
    is_user_banned, is_user_exist, is_user_joined,
    gen_link, is_channel_banned, is_channel_exist,
    is_user_authorized
)
from FileStream.utils.database import Database
from FileStream.utils.file_properties import get_file_ids, get_file_info
from FileStream.config import Telegram
from FileStream.utils.logger import logger  # Import custom logger

# Common error handler
async def handle_exception(client, message, error, user_id, log_message):
    logger.error(f"Error in {message.command or 'handler'}: {error}", exc_info=True)
    await client.send_message(
        chat_id=Telegram.ULOG_CHANNEL,
        text=f"**#ErrorTrackback:** `{error}`",
        disable_web_page_preview=True
    )
    logger.error(log_message.format(user_id=user_id, error=error))

# Private Receive Handler
@FileStream.on_message(
    filters.private & (
        filters.document | filters.video | filters.video_note |
        filters.audio | filters.voice | filters.animation | filters.photo
    ),
    group=4
)
async def private_receive_handler(bot: Client, message: Message):
    if not await is_user_authorized(message):
        logger.info(f"Unauthorized user {message.from_user.id} attempted to send a file.")
        return

    if await is_user_banned(message):
        logger.info(f"Banned user {message.from_user.id} attempted to send a file.")
        return

    await is_user_exist(bot, message)

    if Telegram.FORCE_SUB and not await is_user_joined(bot, message):
        logger.info(f"User {message.from_user.id} has not joined the required channel.")
        return

    try:
        inserted_id = await db.add_file(get_file_info(message))
        await get_file_ids(False, inserted_id, multi_clients, message)
        reply_markup, stream_text = await gen_link(_id=inserted_id)
        await message.reply_text(
            text=stream_text,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
            quote=True
        )
        logger.info(f"Processed private message from user {message.from_user.id} with file ID {inserted_id}.")
    except FloodWait as e:
        await asyncio.sleep(e.value)
        await bot.send_message(
            chat_id=Telegram.ULOG_CHANNEL,
            text=f"Got FloodWait of {e.value}s from {message.from_user.first_name} (ID: {message.from_user.id})",
            disable_web_page_preview=True,
            parse_mode=enums.ParseMode.MARKDOWN
        )
    except Exception as e:
        await handle_exception(bot, message, e, message.from_user.id, "Error processing private message for user {}")

# Channel Receive Handler
@FileStream.on_message(
    filters.channel & ~filters.forwarded & ~filters.media_group & (
        filters.document | filters.video | filters.video_note |
        filters.audio | filters.voice | filters.photo
    )
)
async def channel_receive_handler(bot: Client, message: Message):
    if await is_channel_banned(bot, message):
        logger.info(f"Banned channel {message.chat.id} sent a file.")
        return

    await is_channel_exist(bot, message)

    try:
        inserted_id = await db.add_file(get_file_info(message))
        await get_file_ids(False, inserted_id, multi_clients, message)
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("Download link 📥", url=f"https://t.me/{FileStream.username}?start=stream_{inserted_id}")
        ]])
        await bot.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=message.id,
            reply_markup=reply_markup
        )
        logger.info(f"Processed channel message from chat {message.chat.id} with file ID {inserted_id}.")
    except FloodWait as w:
        await asyncio.sleep(w.x)
        await bot.send_message(
            chat_id=Telegram.ULOG_CHANNEL,
            text=f"Got FloodWait of {w.x}s from {message.chat.title} (ID: {message.chat.id})",
            disable_web_page_preview=True
        )
    except Exception as e:
        await handle_exception(bot, message, e, message.chat.id, "Error processing channel message for chat {}")

# /link Command Handler
@FileStream.on_message(filters.command("link") & ~filters.private)
async def link_handler(client: Client, message: Message):
    user_id = message.from_user.id

    if not await is_user_authorized(message):
        logger.info(f"Unauthorized user {user_id} attempted to use /link command.")
        return

    if await is_user_banned(message):
        logger.info(f"Banned user {user_id} attempted to use /link command.")
        return

    await is_user_exist(client, message)

    if Telegram.FORCE_SUB and not await is_user_joined(client, message):
        logger.info(f"User {user_id} has not joined the required channel.")
        return

    if message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        is_admin = await check_admin_privileges(client, message.chat.id)
        if not is_admin:
            await message.reply_text(
                "🔒 The bot needs to be an admin in this group to function properly.\nPlease promote the bot to admin and try again.",
                quote=True
            )
            logger.warning(f"Bot lacks admin privileges in chat {message.chat.id}")
            return

    if not message.reply_to_message:
        await message.reply_text("⚠️ Please use the /link command in reply to a file.", quote=True)
        logger.info(f"User {user_id} used /link without replying to a message.")
        return

    reply_msg = message.reply_to_message
    if not reply_msg.media:
        await message.reply_text("⚠️ The message you're replying to does not contain any file.", quote=True)
        logger.info(f"User {user_id} replied to a non-media message with /link.")
        return

    command_parts = message.text.strip().split()
    num_files = 1
    if len(command_parts) > 1:
        try:
            num_files = int(command_parts[1])
            if num_files < 1 or num_files > 25:
                await message.reply_text("⚠️ Please specify a number between 1 and 25.", quote=True)
                logger.warning(f"User {user_id} specified invalid number of files: {num_files}")
                return
        except ValueError:
            await message.reply_text("⚠️ Invalid number specified.", quote=True)
            logger.warning(f"User {user_id} provided non-integer value for number of files.")
            return

    if num_files == 1:
        await process_single_file(client, message, reply_msg)
    else:
        await process_multiple_files(client, message, reply_msg, num_files)

# Helper Functions
async def process_single_file(client, message, reply_msg):
    user_id = message.from_user.id
    try:
        inserted_id = await db.add_file(get_file_info(reply_msg))
        await get_file_ids(False, inserted_id, multi_clients, reply_msg)
        reply_markup, stream_text = await gen_link(_id=inserted_id)
        await message.reply_text(
            text=stream_text,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
            quote=True
        )
        logger.info(f"Generated link for file ID {inserted_id} for user {user_id}.")
    except FloodWait as e:
        await asyncio.sleep(e.value)
        await client.send_message(
            chat_id=Telegram.ULOG_CHANNEL,
            text=f"Got FloodWait of {e.value}s from {message.from_user.first_name} (ID: {user_id})",
            disable_web_page_preview=True,
            parse_mode=enums.ParseMode.MARKDOWN
        )
    except Exception as e:
        await handle_exception(client, message, e, user_id, "Error processing single file for user {}")

async def process_multiple_files(client, message, reply_msg, num_files):
    user_id = message.from_user.id
    chat_id = message.chat.id
    start_message_id = reply_msg.id
    end_message_id = start_message_id + num_files - 1
    message_ids = list(range(start_message_id, end_message_id + 1))

    try:
        messages = await client.get_messages(chat_id=chat_id, message_ids=message_ids)
        logger.info(f"Fetched {len(messages)} messages from chat {chat_id} for processing.")
    except RPCError as e:
        logger.error(f"Failed to fetch messages in chat {chat_id}: {e}", exc_info=True)
        await message.reply_text(f"❌ Failed to fetch messages: {e}", quote=True)
        return

    processed_count = 0
    for msg in messages:
        if msg and msg.media:
            try:
                inserted_id = await db.add_file(get_file_info(msg))
                await get_file_ids(False, inserted_id, multi_clients, msg)
                reply_markup, stream_text = await gen_link(_id=inserted_id)
                await message.reply_text(
                    text=stream_text,
                    parse_mode=enums.ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=reply_markup,
                    quote=True
                )
                processed_count += 1
                logger.info(f"Generated link for file ID {inserted_id} for user {user_id}.")
            except FloodWait as e:
                await asyncio.sleep(e.value)
                await client.send_message(
                    chat_id=Telegram.ULOG_CHANNEL,
                    text=f"Got FloodWait of {e.value}s from {message.from_user.first_name} (ID: {user_id})",
                    disable_web_page_preview=True,
                    parse_mode=enums.ParseMode.MARKDOWN
                )
            except Exception as e:
                await handle_exception(client, message, e, user_id, "Error processing file ID {} for user {}")
    await message.reply_text(f"✅ Processed {processed_count} file(s).", quote=True)
    logger.info(f"User {user_id} processed {processed_count} file(s) with /link command.")

async def check_admin_privileges(client, chat_id):
    try:
        member = await client.get_chat_member(chat_id, client.me.id)
        is_admin = member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]
        logger.info(f"Bot is {'admin' if is_admin else 'not admin'} in chat {chat_id}.")
        return is_admin
    except Exception as e:
        logger.error(f"Error checking admin privileges in chat {chat_id}: {e}", exc_info=True)
        return False
