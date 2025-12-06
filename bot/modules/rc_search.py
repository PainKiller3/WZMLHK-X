import subprocess
import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urljoin, quote
from datetime import datetime, timedelta
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from rapidfuzz import fuzz

from .. import LOGGER
from ..helper.ext_utils.bot_utils import new_task
from ..helper.telegram_helper.message_utils import send_message, edit_message
from ..core.config_manager import Config

# Get rclone configuration from Config
RCLONE_REMOTE = getattr(Config, "RCLONE_REMOTE", "")
RCLONE_SERVE_URL = getattr(Config, "RCLONE_SERVE_URL", "")
REMOTE_BASE_PATH = getattr(Config, "REMOTE_BASE_PATH", "")
RESULTS_PER_PAGE = getattr(Config, "RESULTS_PER_PAGE", 4)

# Store search results temporarily
search_cache = {}

# Store last 10 searches per user
search_history = {}

# ---------------- Auto Index Refresh Cache ---------------- #
global_file_index = []
global_index_timestamp = None
INDEX_TTL = 600  # Refresh every 10 minutes


# ---------------- Helper Functions ---------------- #
def run_rclone_command(cmd, description="rclone command"):
    """Run rclone command with logging and error handling."""
    LOGGER.info(f"Running: {' '.join(cmd)}")
    try:
        result = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode()
        LOGGER.info(f"{description} succeeded.")

        return result

    except subprocess.CalledProcessError as e:
        LOGGER.error(
            f"{description} failed with code {e.returncode}: {e.output.decode()}"
        )

        return None

    except Exception as e:
        LOGGER.error(f"{description} unexpected error: {e}")

        return None


def refresh_global_index(force=False):
    """
    Refresh global index every INDEX_TTL seconds.
    If force=True → force-refresh the index.
    """
    global global_file_index, global_index_timestamp

    now = datetime.now()

    # Use cached index if still valid
    if (
        not force
        and global_index_timestamp
        and (now - global_index_timestamp).total_seconds() < INDEX_TTL
    ):
        return global_file_index

    try:
        result = run_rclone_command(
            [
                "rclone",
                "--config",
                "rclone.conf",  # Use local rclone.conf
                "lsjson",
                RCLONE_REMOTE,
                "--recursive",
            ],
            description="Refreshing global index",
        )

        if result:
            try:
                global_file_index = json.loads(result)
                global_index_timestamp = now
                LOGGER.info(
                    f"Global index refreshed with {len(global_file_index)} files."
                )
                return global_file_index
            except Exception as e:
                LOGGER.error(f"Failed to parse global index JSON: {e}")

    except Exception as e:
        LOGGER.error(f"Error refreshing global index: {e}")

    return None


def search_files():
    """
    Return file list from global cache.
    Auto refreshes every INDEX_TTL seconds.
    """
    return refresh_global_index()


def parse_size(size_str):
    """Convert size string like '1GB', '500MB' to bytes."""
    size_str = size_str.upper().strip()

    units = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}

    # Match number and unit
    match = re.match(r"^([\d.]+)\s*(B|KB|MB|GB|TB)$", size_str)
    if not match:
        return None

    value = float(match.group(1))
    unit = match.group(2)

    return int(value * units[unit])


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


def highlight_match(text, query):
    """Highlight matched words in text using markdown bold."""
    query_words = normalize(query).split()
    result = text

    for word in query_words:
        if len(word) < 2:  # Skip very short words
            continue
        # Case-insensitive replacement with bold markdown
        pattern = re.compile(re.escape(word), re.IGNORECASE)
        result = pattern.sub(lambda m: f"**{m.group(0)}**", result)

    return result


def match_file(query, filename, threshold=85):
    """
    Strict matching:
    1. Substring match (full query in filename)
    2. OR strong fuzzy match using token_sort_ratio
    Prevents partial word false matches (e.g., "forza" won't match "formant").
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


def parse_date_filter(date_str):
    """Parse date string like '7d', '30d', '1y' and return datetime."""
    date_str = date_str.lower().strip()

    match = re.match(r"^(\d+)(d|w|m|y)$", date_str)
    if not match:
        return None

    value = int(match.group(1))
    unit = match.group(2)

    now = datetime.now()

    if unit == "d":
        return now - timedelta(days=value)
    elif unit == "w":
        return now - timedelta(weeks=value)
    elif unit == "m":
        return now - timedelta(days=value * 30)  # Approximate
    elif unit == "y":
        return now - timedelta(days=value * 365)  # Approximate

    return None


def parse_search_args(args):
    """
    Parse search arguments and extract filters.
    Returns: (query, file_type, min_size, max_size, date_filter)

    Examples:
        /rclist software --type zip
        /rclist movie --type mkv --min 1GB --max 10GB
        /rclist document --date 7d
    """
    query_parts = []
    file_type = None
    min_size = None
    max_size = None
    date_filter = None

    i = 0
    while i < len(args):
        arg = args[i]

        if arg == "--type" and i + 1 < len(args):
            file_type = args[i + 1].lower()
            i += 2
        elif arg == "--min" and i + 1 < len(args):
            min_size = parse_size(args[i + 1])
            i += 2
        elif arg == "--max" and i + 1 < len(args):
            max_size = parse_size(args[i + 1])
            i += 2
        elif arg == "--date" and i + 1 < len(args):
            date_filter = parse_date_filter(args[i + 1])
            i += 2
        else:
            query_parts.append(arg)
            i += 1

    query = " ".join(query_parts)
    return query, file_type, min_size, max_size, date_filter


def apply_filters(
    files, query, file_type=None, min_size=None, max_size=None, date_filter=None
):
    """Apply all filters to file list."""
    matched_files = []

    for f in files:
        # Skip directories for size and type filters
        is_dir = f.get("IsDir", False)

        # Match query
        if query and not match_file(query, f["Name"]):
            continue

        # Filter by file type
        if file_type and not is_dir:
            file_ext = Path(f["Name"]).suffix.lower().lstrip(".")
            if file_ext != file_type:
                continue

        # Filter by size
        if not is_dir:
            file_size = f.get("Size", 0)

            if min_size is not None and file_size < min_size:
                continue

            if max_size is not None and file_size > max_size:
                continue

        # Filter by date
        if date_filter:
            mod_time_str = f.get("ModTime", "")
            if mod_time_str:
                try:
                    # Parse ISO format timestamp
                    mod_time = datetime.fromisoformat(
                        mod_time_str.replace("Z", "+00:00")
                    )
                    if mod_time < date_filter:
                        continue
                except:
                    pass

        matched_files.append(f)

    return matched_files


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


@lru_cache(maxsize=500)
def get_folder_size(path):
    """
    Returns (bytes, file_count) for a folder using `rclone size`.
    Cached via LRU to avoid recomputing slow recursive scans.
    """
    try:
        cmd = [
            "rclone",
            "--config",
            "rclone.conf",
            "size",
            f"{RCLONE_REMOTE}{REMOTE_BASE_PATH}/{path}",
            "--json",
        ]

        result = run_rclone_command(cmd, description=f"Getting folder size for {path}")
        if result:
            data = json.loads(result)
            return data.get("bytes", 0), data.get("count", 0)

    except Exception as e:
        LOGGER.error(f"Error parsing folder size for {path}: {e}")
        return 0, 0


def create_result_text(matched_files, page, query, filters_applied):
    """Create text for a specific page of results."""
    total_results = len(matched_files)
    total_pages = (total_results + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE

    start_idx = page * RESULTS_PER_PAGE
    end_idx = min(start_idx + RESULTS_PER_PAGE, total_results)

    header = f"🔎 **Search Results for:** {query}\n"

    # Show active filters
    if filters_applied:
        header += "🔧 **Filters:** " + ", ".join(filters_applied) + "\n"

    header += f"📊 **Total:** {total_results} | **Page:** {page + 1}/{total_pages}\n\n"

    reply = header

    for i in range(start_idx, end_idx):
        f = matched_files[i]
        name = f["Name"]

        # Highlight matched words in filename
        if query:
            highlighted_name = highlight_match(name, query)
        else:
            highlighted_name = name

        if f.get("IsDir", False):
            folder_path = f["Path"]

            # Clean path for rclone size
            if folder_path.startswith(f"{REMOTE_BASE_PATH}/"):
                folder_path = folder_path[len(REMOTE_BASE_PATH) + 1 :]

            bytes_size, file_count = get_folder_size(folder_path)
            size = f"{file_count} files | {format_size(bytes_size)}"
        else:
            size = format_size(f.get("Size", -1))

        path = f["Path"]

        # Remove duplicate folder from path
        if path.startswith(f"{REMOTE_BASE_PATH}/"):
            path = path[len(REMOTE_BASE_PATH) + 1 :]

        # URL encode the path for proper link generation
        encoded_path = quote(f"{REMOTE_BASE_PATH}/{path}")
        public_link = urljoin(RCLONE_SERVE_URL + "/", encoded_path)

        # Escape special characters for Markdown (but keep our bold highlights)
        escaped_name = highlighted_name

        icon = "📁" if f.get("IsDir", False) else "📄"

        reply += (
            f"**{i + 1}.** {icon} {escaped_name}\n"
            f"💾 **Size:** {size}\n"
            f"[🔗 Open Link]({public_link})\n\n"
        )

    return reply, total_pages


def create_pagination_buttons(page, total_pages, user_id, query):
    """Create pagination buttons."""
    buttons = []
    nav_buttons = []

    # Previous button
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton(
                "⬅️ Previous", callback_data=f"page:{user_id}:{page - 1}:{query}"
            )
        )

    # Next button
    if page < total_pages - 1:
        nav_buttons.append(
            InlineKeyboardButton(
                "Next ➡️", callback_data=f"page:{user_id}:{page + 1}:{query}"
            )
        )

    if nav_buttons:
        buttons.append(nav_buttons)

    # Close button
    buttons.append(
        [
            InlineKeyboardButton(
                "❌ Close", callback_data=f"page:{user_id}:close:{query}"
            )
        ]
    )

    return InlineKeyboardMarkup(buttons)


async def rclist_command(client: Client, message: Message):
    # Extract command and arguments
    command_message_id = message.id
    chat_id = message.chat.id
    user_id = message.from_user.id

    # If user typed only "/rclist" show suggestions (autocomplete-style)
    if len(message.command) < 2:
        history = search_history.get(user_id, [])

        text = "🤖 **Search Suggestions**\n\n"

        if history:
            text += "🕘 **Recent Searches:**\n"
            for q in history[:5]:
                text += f"• `/rclist {q}`\n"
            text += "\n"

        text += (
            "🧩 **Common Filters:**\n"
            "`--type zip`   `--type mkv`\n"
            "`--min 1GB`    `--max 10GB`\n"
            "`--date 7d`\n\n"
            "📌 **Examples:**\n"
            "`/rclist software --type zip`\n"
            "`/rclist movie --min 1GB --max 5GB`\n"
        )

        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return

    # Parse arguments
    args = message.command[1:]
    query, file_type, min_size, max_size, date_filter = parse_search_args(args)

    # Validate query (can be empty if filters are provided)
    if query and not is_valid_query(query):
        await message.reply_text(
            "❌ **Invalid command**\n\nUsage: `/rclist <query> [options]`\n\n"
            "Please provide a valid search query with alphanumeric characters.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # At least one filter or query must be provided
    if (
        not query
        and not file_type
        and not min_size
        and not max_size
        and not date_filter
    ):
        await message.reply_text(
            "❌ **Invalid command**\n\nUsage: `/rclist <query> [options]`\n\n"
            "Please provide at least a search query or filter.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    user_id = message.from_user.id

    # Build filter description
    filters_applied = []
    if file_type:
        filters_applied.append(f"Type: {file_type}")
    if min_size:
        filters_applied.append(f"Min: {format_size(min_size)}")
    if max_size:
        filters_applied.append(f"Max: {format_size(max_size)}")
    if date_filter:
        filters_applied.append(f"Modified after: {date_filter.strftime('%Y-%m-%d')}")

    # "Searching for..." indicator
    search_text = f"🔍 Searching for: **{query or 'all files'}**"
    if filters_applied:
        search_text += f"\n🔧 Filters: {', '.join(filters_applied)}"
    search_text += " ..."

    search_msg = await message.reply_text(search_text, parse_mode=ParseMode.MARKDOWN)

    files = search_files()

    if not files:
        await search_msg.edit_text(
            f"❌ No results found for: **{query or 'your search'}**",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Apply all filters
    matched_files = apply_filters(
        files, query, file_type, min_size, max_size, date_filter
    )

    if not matched_files:
        await search_msg.edit_text(
            f"❌ No results found for: **{query or 'your search'}**\n\n"
            f"Try adjusting your filters or search query.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Save search query to history (dedupe, newest first, max 10)
    if query:
        old = search_history.get(user_id, [])
        new_list = [query] + [q for q in old if q != query]
        search_history[user_id] = new_list[:10]

    # Store results in cache with unique key including filters
    cache_key = f"{user_id}:{query}:{file_type}:{min_size}:{max_size}:{date_filter}"
    search_cache[cache_key] = {
        "files": matched_files,
        "query": query or "all files",
        "filters": filters_applied,
        "cmd_msg_id": command_message_id,
        "chat_id": chat_id,
    }

    # Create first page
    reply, total_pages = create_result_text(
        matched_files, 0, query or "all files", filters_applied
    )
    buttons = create_pagination_buttons(0, total_pages, user_id, cache_key)

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


async def recent_searches(client: Client, message: Message):
    user_id = message.from_user.id
    history = search_history.get(user_id, [])

    if not history:
        return await message.reply_text(
            "📭 **No recent searches yet!**\nStart searching using `/rclist <keyword>`.",
            parse_mode=ParseMode.MARKDOWN,
        )

    formatted = "\n".join([f"**{i+1}.** `{q}`" for i, q in enumerate(history)])

    await message.reply_text(
        f"🕘 **Your Recent Searches (last {len(history)}):**\n\n{formatted}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def latest_uploads(client: Client, message: Message):
    """Show latest uploaded/modified files with pagination and highlights."""
    user_id = message.from_user.id

    # Number of results per page (same as RESULTS_PER_PAGE)
    per_page = RESULTS_PER_PAGE

    # "Fetching latest uploads..." indicator
    fetching_msg = await message.reply_text(
        f"⏳ Fetching latest uploads...", parse_mode=ParseMode.MARKDOWN
    )

    # Get all files from remote
    files = search_files()
    if not files:
        await fetching_msg.edit_text(
            "❌ Unable to fetch files from remote.", parse_mode=ParseMode.MARKDOWN
        )
        return

    # Filter out directories, only files
    file_list = [f for f in files if not f.get("IsDir", False)]

    # Sort files by ModTime descending
    def get_mod_time(f):
        try:
            return datetime.fromisoformat(f.get("ModTime", "").replace("Z", "+00:00"))
        except:
            return datetime.min

    file_list.sort(key=get_mod_time, reverse=True)

    if not file_list:
        await fetching_msg.edit_text(
            "❌ No files found in remote.", parse_mode=ParseMode.MARKDOWN
        )
        return

    # Highlight filenames (optional, here we just keep names as is)
    for f in file_list:
        f["Name"] = highlight_match(
            f["Name"], ""
        )  # empty query, keeps original but can apply styles

    # Cache key for this user
    cache_key = f"latest:{user_id}"
    search_cache[cache_key] = {
        "files": file_list,
        "query": "Latest Uploads",
        "filters": [],
    }

    # Create first page
    reply, total_pages = create_result_text(file_list, 0, "Latest Uploads", [])
    buttons = create_pagination_buttons(0, total_pages, user_id, cache_key)

    # Edit fetching message with first page
    await fetching_msg.edit_text(
        reply,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=buttons,
        disable_web_page_preview=True,
    )


async def handle_pagination(client: Client, callback_query: CallbackQuery):
    data = callback_query.data.split(":", 3)  # Split into max 4 parts

    user_id = int(data[1])

    # Check if user is authorized
    if callback_query.from_user.id != user_id:
        return await callback_query.answer(
            "❌ This is not your search!", show_alert=True
        )

    # Handle close button
    if data[2] == "close":
        await callback_query.answer("✅ Closed")

        cache_key = data[3] if len(data) > 3 else None

        # Delete bot result message
        try:
            await callback_query.message.delete()
        except Exception as e:
            LOGGER.error(f"Failed to delete bot message: {e}")

        # Delete original /rclist command message
        if cache_key and cache_key in search_cache:
            data = search_cache.get(cache_key)
            try:
                await client.delete_messages(
                    chat_id=data["chat_id"], message_ids=data["cmd_msg_id"]
                )
            except Exception as e:
                LOGGER.error(f"Failed to delete command message: {e}")

            # Cleanup cache
            search_cache.pop(cache_key, None)

        return

    # 📄 PAGINATION
    if len(data) < 4:
        return await callback_query.answer(
            "❌ Invalid pagination request", show_alert=True
        )

    page = int(data[2])
    cache_key = data[3]  # Full cache key with filters

    # Get cached results
    if cache_key not in search_cache:
        return await callback_query.answer(
            "❌ Search expired. Please search again.", show_alert=True
        )

    cache_data = search_cache[cache_key]
    matched_files = cache_data["files"]
    query = cache_data["query"]
    filters_applied = cache_data["filters"]

    # Create page text and buttons
    reply, total_pages = create_result_text(matched_files, page, query, filters_applied)
    buttons = create_pagination_buttons(page, total_pages, user_id, cache_key)

    # Update message
    await callback_query.message.edit_text(
        reply,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=buttons,
        disable_web_page_preview=True,
    )

    await callback_query.answer(f"📄 Page {page + 1}/{total_pages}")
