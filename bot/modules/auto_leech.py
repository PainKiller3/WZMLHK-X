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
    is_auto_leech_enabled = Config.AUTO_LEECH or user_dict.get("USER_AUTO_LEECH", False)
    is_social_auto_dl_enabled = Config.SOCIAL_AUTO_DOWNLOAD or user_dict.get(
        "SOCIAL_AUTO_DOWNLOAD", False
    )

    if not is_auto_leech_enabled and not is_social_auto_dl_enabled:
        return

    # Check authorized/allowed chats
    social_chats = Config.SOCIAL_AUTO_DOWNLOAD_CHATS or Config.AUTO_LEECH_CHATS
    chats_to_check = (
        social_chats if is_social_auto_dl_enabled else Config.AUTO_LEECH_CHATS
    )

    if chats_to_check:
        allowed_chats = []
        for chat_str in chats_to_check.split():
            try:
                allowed_chats.append(int(chat_str.strip()))
            except ValueError:
                pass
        if allowed_chats and message.chat.id not in allowed_chats:
            return
    else:
        # Default to authorized chats if empty
        if not await CustomFilters.authorized(client, message):
            return

    max_links = user_dict.get("AUTO_LEECH_MAX_LINKS", Config.AUTO_LEECH_MAX_LINKS)
    user_ext = user_dict.get("AUTO_LEECH_EXT", [])

    # Custom patterns: append social domains if SOCIAL_AUTO_DOWNLOAD is enabled
    custom_patterns = list(user_ext)
    if is_social_auto_dl_enabled:
        custom_patterns.extend(["instagram.com", "twitter.com", "x.com"])

    media_links = extract_media_links(
        message.text, max_links, custom_patterns=tuple(custom_patterns)
    )

    # Filter out direct media urls if USER_AUTO_LEECH is disabled (keep social urls if SOCIAL_AUTO_DOWNLOAD is enabled)
    if not is_auto_leech_enabled:
        media_links = [
            link
            for link in media_links
            if any(
                domain in link.lower()
                for domain in ["instagram.com", "twitter.com", "x.com"]
            )
        ]

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
