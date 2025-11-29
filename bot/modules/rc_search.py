import subprocess
import json
import re
from urllib.parse import urljoin, quote, unquote
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from rapidfuzz import fuzz

from .. import LOGGER
from ..helper.ext_utils.bot_utils import new_task
from ..helper.telegram_helper.message_utils import send_message, edit_message
from ..core.config_manager import Config
from ..core.tg_client import TgClient

# Get rclone configuration from Config
RCLONE_REMOTE = getattr(Config, "RCLONE_REMOTE", "")
RCLONE_SERVE_URL = getattr(Config, "RCLONE_SERVE_URL", "")
REMOTE_BASE_PATH = getattr(Config, "REMOTE_BASE_PATH", "")
RESULTS_PER_PAGE = getattr(Config, "RESULTS_PER_PAGE", 4)

# Store search results temporarily
search_cache = {}


# ---------------- Helper Functions ---------------- #
def search_files():
    """Return all files from remote using rclone."""
    try:
        result = subprocess.check_output(
            [
                "rclone",
                "--config",
                "rclone.conf",
                "lsjson",
                RCLONE_REMOTE,
                "--recursive",
            ],
            stderr=subprocess.STDOUT,
        ).decode()
        return json.loads(result)
    except Exception as e:
        LOGGER.error(f"rclone search failed: {e}")
        return None


def format_size(size_bytes):
    """Convert size to human-readable format."""
    if size_bytes <= 0:
        return "-1.00B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.2f}{unit}"
        size_bytes /= 1024
    return f"{size_bytes:.2f}PB"


def normalize(text):
    """Normalize filenames and queries."""
    return re.sub(r"[._-]", " ", text).lower()


def match_file(query, filename, threshold=85):
    """
    Strict matching:
    1. Substring match (full query in filename)
    2. OR strong fuzzy match using token_sort_ratio
    """
    query_norm = normalize(query)
    filename_norm = normalize(filename)

    # Exact substring match
    if query_norm in filename_norm:
        return True

    # Fuzzy match (strict)
    query_words = query_norm.split()
    for word in query_words:
        score = fuzz.token_sort_ratio(word, filename_norm)
        if score < threshold:
            return False
    return True


def is_valid_query(query):
    """
    Validate the search query.
    Returns True if valid, False otherwise.
    """
    # Remove leading/trailing whitespace
    query = query.strip()

    # Check if query is empty
    if not query:
        return False

    # Check if query is just special characters or wildcards
    # Only alphanumeric characters are considered valid
    if not any(c.isalnum() for c in query):
        return False

    # Check for single character wildcards
    if query in ["*", "?", ".", ".."]:
        return False

    return True


def escape_markdown(text):
    """Escape special characters for Markdown."""
    special_chars = ["_", "*", "`"]
    for char in special_chars:
        text = text.replace(char, f"\\{char}")
    return text


def create_result_text(matched_files, page, query):
    """Create text for a specific page of results."""
    total_results = len(matched_files)
    total_pages = (total_results + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE

    start_idx = page * RESULTS_PER_PAGE
    end_idx = min(start_idx + RESULTS_PER_PAGE, total_results)

    header = f"🔎 **Search Results for:** {query}\n"
    header += f"📊 **Total:** {total_results} | **Page:** {page + 1}/{total_pages}\n\n"

    reply = header

    for i in range(start_idx, end_idx):
        f = matched_files[i]
        name = f["Name"]
        if f.get("IsDir", False):
            size = "-dir-"
        else:
            size = format_size(f.get("Size", -1))

        path = f["Path"]

        # Remove duplicate folder from path
        if path.startswith(f"{REMOTE_BASE_PATH}/"):
            path = path[len(REMOTE_BASE_PATH) + 1 :]

        # URL encode the path for proper link generation
        encoded_path = quote(f"{REMOTE_BASE_PATH}/{path}")
        public_link = urljoin(RCLONE_SERVE_URL + "/", encoded_path)

        # Escape special characters for Markdown
        escaped_name = escape_markdown(name)

        reply += (
            f"**{i + 1}.** 📄 **{escaped_name}**\n"
            f"💾 **Size:** {size}\n"
            f"[🔗 Open Link]({public_link})\n\n"
        )

    return reply, total_pages


def create_pagination_buttons(page, total_pages, user_id, query):
    """Create pagination buttons."""
    encoded_query = quote(query)
    buttons = []
    nav_buttons = []

    # Previous button
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton(
                "⬅️ Previous",
                callback_data=f"page:{user_id}:{page - 1}:{encoded_query}",
            )
        )

    # Page indicator
    nav_buttons.append(
        InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="page:noop")
    )

    # Next button
    if page < total_pages - 1:
        nav_buttons.append(
            InlineKeyboardButton(
                "Next ➡️", callback_data=f"page:{user_id}:{page + 1}:{encoded_query}"
            )
        )

    if nav_buttons:
        buttons.append(nav_buttons)

    # Close button
    buttons.append([InlineKeyboardButton("❌ Close", callback_data=f"close:{user_id}")])

    return InlineKeyboardMarkup(buttons)


# ---------------- Bot Handler ---------------- #
@new_task
async def rclone_list(_, message):
    """Handler for /rclist command - searches rclone remote."""
    if len(message.text.split()) == 1:
        return await send_message(
            message, "<i>❌ **Invalid command**\n\nUsage: `/rclist <query>`</i>"
        )

    query = message.text.split(maxsplit=1)[1].strip()

    # Validate query
    if not is_valid_query(query):
        return await send_message(
            message,
            "<i>❌ **Invalid command**\n\nUsage: `/rclist <query>`\n\nPlease provide a valid search query with alphanumeric characters.</i>",
        )

    user_id = message.from_user.id

    # "Searching for..." indicator
    search_msg = await send_message(
        message, f"<b>Searching for: <i>{query}</i> ...</b>"
    )

    files = search_files()
    if not files:
        return await edit_message(
            search_msg, f"❌ No results found for: <i>{query}</i>"
        )

    # Filter using strict fuzzy logic
    matched_files = [f for f in files if match_file(query, f["Name"])]

    if not matched_files:
        return await edit_message(
            search_msg, f"❌ No results found for: <i>{query}</i>"
        )

    # Store results in cache
    cache_key = f"{user_id}:{query}"
    search_cache[cache_key] = matched_files

    # Create first page
    reply, total_pages = create_result_text(matched_files, 0, query)
    buttons = create_pagination_buttons(0, total_pages, user_id, query)

    # Delete searching message
    try:
        await search_msg.delete()
    except Exception:
        pass

    # Send first page with buttons
    await message.reply_text(
        reply,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=buttons,
        disable_web_page_preview=True,
    )


def pagination_filter(_, __, query: CallbackQuery):
    data = query.data or ""
    return data.startswith("page:") or data.startswith("close:")


@TgClient.bot.on_callback_query(filters.create(pagination_filter))
async def handle_pagination(client, callback_query: CallbackQuery):
    data = callback_query.data

    # Ignore only your own noop button
    if data == "page:noop":
        return await callback_query.answer()

    # Close button
    if data.startswith("close:"):
        _, user_id = data.split(":")
        if callback_query.from_user.id != int(user_id):
            return await callback_query.answer(
                "❌ This is not your search!", show_alert=True
            )
        await callback_query.message.delete()
        return await callback_query.answer("✅ Closed")

    # Page navigation
    if data.startswith("page:"):
        _, user_id, page, encoded_query = data.split(":", 3)

        if callback_query.from_user.id != int(user_id):
            return await callback_query.answer(
                "❌ This is not your search!", show_alert=True
            )

        query = unquote(encoded_query)
        cache_key = f"{user_id}:{query}"

        if cache_key not in search_cache:
            return await callback_query.answer(
                "❌ Search expired. Please search again.", show_alert=True
            )

        matched_files = search_cache[cache_key]

        reply, total_pages = create_result_text(matched_files, int(page), query)
        buttons = create_pagination_buttons(int(page), total_pages, user_id, query)

        await callback_query.message.edit_text(
            reply,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=buttons,
            disable_web_page_preview=True,
        )

        return await callback_query.answer(f"📄 Page {int(page) + 1}/{total_pages}")
