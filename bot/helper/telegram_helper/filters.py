from pyrogram.filters import create
from pyrogram.enums import ChatType

from ... import auth_chats, sudo_users, stremio_users, stremio_chats, user_data
from ...core.config_manager import Config
from .tg_utils import chat_info


class CustomFilters:
    async def owner_filter(self, _, update):
        user = update.from_user or update.sender_chat
        return user.id == Config.OWNER_ID

    owner = create(owner_filter)

    async def authorized_user(self, _, update):
        uid = (update.from_user or update.sender_chat).id
        chat_id = update.chat.id
        thread_id = update.message_thread_id if update.is_topic_message else None
        return bool(
            uid == Config.OWNER_ID
            or (
                uid in user_data
                and (
                    user_data[uid].get("AUTH", False)
                    or user_data[uid].get("SUDO", False)
                )
            )
            or (
                chat_id in user_data
                and user_data[chat_id].get("AUTH", False)
                and (
                    thread_id is None
                    or thread_id in user_data[chat_id].get("thread_ids", [])
                )
            )
            or uid in sudo_users
            or uid in auth_chats
            or chat_id in auth_chats
            and (
                auth_chats[chat_id]
                and thread_id
                and thread_id in auth_chats[chat_id]
                or not auth_chats[chat_id]
            )
        )

    authorized = create(authorized_user)

    async def authorized_usetting(self, _, update):
        uid = (update.from_user or update.sender_chat).id
        is_exists = False
        if await CustomFilters.authorized("", update):
            is_exists = True
        elif update.chat.type == ChatType.PRIVATE:
            for channel_id in user_data:
                if not (
                    user_data[channel_id].get("is_auth")
                    and str(channel_id).startswith("-100")
                ):
                    continue
                try:
                    if await (await chat_info(str(channel_id))).get_member(uid):
                        is_exists = True
                        break
                except Exception:
                    continue
        return is_exists

    authorized_uset = create(authorized_usetting)

    async def sudo_user(self, _, update):
        user = update.from_user or update.sender_chat
        uid = user.id
        return bool(
            uid == Config.OWNER_ID
            or uid in user_data
            and user_data[uid].get("SUDO")
            or uid in sudo_users
        )

    sudo = create(sudo_user)

    async def stremio_user(self, _, update):
        user = update.from_user or update.sender_chat
        uid = user.id if user else 0
        chat = update.chat or (
            update.message.chat
            if hasattr(update, "message") and update.message
            else None
        )
        chat_id = chat.id if chat else 0

        # Owner and Sudo users always bypass
        if (
            uid == Config.OWNER_ID
            or (uid in user_data and user_data[uid].get("SUDO"))
            or uid in sudo_users
        ):
            return True

        has_user_filter = bool(Config.STREMIO_USERS or stremio_users)
        has_chat_filter = bool(Config.STREMIO_AUTHORIZED_CHATS or stremio_chats)

        if not has_user_filter and not has_chat_filter:
            return await CustomFilters.authorized("", update)

        user_allowed = not has_user_filter or (uid in stremio_users)
        chat_allowed = (
            (chat_id in stremio_chats)
            if has_chat_filter
            else await CustomFilters.authorized("", update)
        )

        return bool(user_allowed and chat_allowed)

    stremio = create(stremio_user)
