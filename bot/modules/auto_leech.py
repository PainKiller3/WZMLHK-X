import copy

from .. import user_data
from ..core.config_manager import Config
from ..helper.ext_utils.auto_leech_helper import extract_media_links
from ..helper.telegram_helper.bot_commands import BotCommands
from ..helper.telegram_helper.filters import CustomFilters
from .mirror_leech import leech


async def auto_leech_listener(client, message):
    if not message.text or message.text.startswith("/"):
        return

    user_id = (
        message.from_user.id
        if message.from_user
        else (message.sender_chat.id if message.sender_chat else 0)
    )
    user_dict = user_data.get(user_id, {})
    is_auto_leech_enabled = user_dict.get("USER_AUTO_LEECH", False)
    if not is_auto_leech_enabled:
        return

    if Config.AUTO_LEECH_CHATS:
        allowed_chats = []
        for chat_str in Config.AUTO_LEECH_CHATS.split():
            try:
                allowed_chats.append(int(chat_str.strip()))
            except ValueError:
                pass
        if allowed_chats and message.chat.id not in allowed_chats:
            return
    else:
        # Default to authorized chats if AUTO_LEECH_CHATS is empty
        if not await CustomFilters.authorized(client, message):
            return

    max_links = user_dict.get("AUTO_LEECH_MAX_LINKS", Config.AUTO_LEECH_MAX_LINKS)
    user_ext = user_dict.get("AUTO_LEECH_EXT", [])
    media_links = extract_media_links(
        message.text, max_links, custom_patterns=tuple(user_ext)
    )
    if not media_links:
        return

    leech_cmd = (
        BotCommands.LeechCommand[0]
        if isinstance(BotCommands.LeechCommand, list)
        else BotCommands.LeechCommand
    )

    for idx, link in enumerate(media_links, start=1):
        msg_copy = copy.copy(message)
        msg_copy._client = getattr(message, "_client", client)
        msg_copy.task_id = int(f"{message.id}{idx}")
        msg_copy.text = f"/{leech_cmd} {link}"
        await leech(client, msg_copy)
