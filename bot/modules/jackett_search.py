import asyncio
import re
import xml.etree.ElementTree as ET

from niquests import AsyncSession

from .. import LOGGER, bot_loop
from ..core.config_manager import Config
from ..helper.ext_utils.bot_utils import new_task
from ..helper.ext_utils.status_utils import get_readable_file_size
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_message,
)
from .mirror_leech import Mirror

# In-memory storage for active search sessions: {search_id: {"results": [...], "user_id": int, "query": str}}
SEARCH_CACHE = {}

# Active series queue runner locks to prevent overlapping batches per user
SERIES_QUEUE_LOCK = asyncio.Lock()


def parse_season_info(title: str):
    """
    Detect if release is a TV Season pack or Episode.
    Returns:
      is_series: bool
      season_num: int or None
      is_complete: bool
    """
    clean_title = re.sub(r"[._\-]", " ", title)

    # Check for complete multi-season series pack
    if re.search(
        r"\b(complete series|s01\s*[-–]\s*s\d+|seasons?\s*1\s*[-–]\s*\d+)\b",
        clean_title,
        re.IGNORECASE,
    ):
        return True, None, True

    # Check for single season pack: e.g. S01, Season 1
    match = re.search(r"\b(?:s|season\s*)(\d{1,2})\b", clean_title, re.IGNORECASE)
    if match:
        season_num = int(match.group(1))
        # Exclude if it looks like a single episode (e.g., S01E05)
        if re.search(r"\b(?:s\d{1,2})?e\d{1,3}\b", clean_title, re.IGNORECASE):
            return True, season_num, False
        return True, season_num, True

    return False, None, False


async def query_jackett(query: str, category: str = None, limit: int = None):
    """
    Query Jackett Torznab API and return parsed list of torrent/magnet results.
    """
    if not Config.JACKETT_URL or not Config.JACKETT_API_KEY:
        return []

    base_url = Config.JACKETT_URL.rstrip("/")
    api_key = Config.JACKETT_API_KEY
    limit = limit or Config.JACKETT_SEARCH_LIMIT or 15

    # Torznab endpoint
    params = {
        "apikey": api_key,
        "t": "search",
        "q": query,
    }
    if category:
        params["cat"] = category

    url = f"{base_url}/api/v2.0/indexers/all/results/torznab/api"

    results = []
    try:
        async with AsyncSession() as session:
            resp = await session.get(url, params=params, timeout=20)
            if resp.status_code != 200:
                LOGGER.error(f"Jackett API returned status {resp.status_code}")
                return []

            root = ET.fromstring(resp.text)
            channel = root.find("channel")
            if channel is None:
                return []

            for item in channel.findall("item"):
                title_elem = item.find("title")
                title = title_elem.text if title_elem is not None else "Unknown"

                size_elem = item.find("size")
                size = (
                    int(size_elem.text)
                    if size_elem is not None and size_elem.text
                    else 0
                )

                link_elem = item.find("link")
                torrent_link = link_elem.text if link_elem is not None else ""

                seeders = 0
                magnet_url = ""
                # Parse torznab:attr elements
                for attr in item.findall("{http://torznab.com/schemas/2015/feed}attr"):
                    attr_name = attr.get("name")
                    attr_value = attr.get("value")
                    if attr_name == "seeders" and attr_value:
                        try:
                            seeders = int(attr_value)
                        except ValueError:
                            pass
                    elif attr_name == "magneturl" and attr_value:
                        magnet_url = attr_value

                # If no direct magnet in torznab attributes, use link if it's magnet
                download_url = magnet_url or torrent_link

                if download_url:
                    is_series, season_num, is_pack = parse_season_info(title)
                    results.append(
                        {
                            "title": title,
                            "size": size,
                            "readable_size": get_readable_file_size(size),
                            "seeders": seeders,
                            "url": download_url,
                            "is_series": is_series,
                            "season": season_num,
                            "is_pack": is_pack,
                        }
                    )

                if len(results) >= limit:
                    break

    except Exception as e:
        LOGGER.error(f"Error querying Jackett: {e}")

    # Sort results by seeders descending
    results.sort(key=lambda x: x["seeders"], reverse=True)
    return results


@new_task
async def jackett_search(_, message):
    if Config.DISABLE_TORRENTS or Config.DISABLE_SEARCH:
        await send_message(
            message, "Torrent search is currently disabled by the Bot Owner."
        )
        return

    if not Config.JACKETT_URL or not Config.JACKETT_API_KEY:
        await send_message(
            message,
            "⚠️ <b>Jackett is not configured!</b>\nPlease set <code>JACKETT_URL</code> and <code>JACKETT_API_KEY</code> in bot settings / config.",
        )
        return

    text_parts = message.text.split(maxsplit=1)
    if len(text_parts) == 1:
        await send_message(
            message,
            "<b>Usage:</b> <code>/jsearch &lt;movie or tv show name&gt;</code>\n"
            "<i>Example:</i> <code>/jsearch Breaking Bad</code> or <code>/jsearch Inception</code>",
        )
        return

    query = text_parts[1].strip()
    status_msg = await send_message(
        message, f"🔍 <i>Searching Jackett for:</i> <b>{query}</b>..."
    )

    results = await query_jackett(query)
    if not results:
        await edit_message(
            status_msg,
            f"❌ No torrent results found for: <b>{query}</b> on enabled Jackett indexers.",
        )
        return

    search_id = f"{message.from_user.id}_{message.id}"
    SEARCH_CACHE[search_id] = {
        "results": results,
        "user_id": message.from_user.id,
        "query": query,
    }

    buttons = ButtonMaker()
    # List top results as selection buttons
    for idx, item in enumerate(results[:8]):
        badge = f"[S{item['season']:02d}] " if item.get("season") else ""
        btn_text = f"[{idx + 1}] {badge}{item['readable_size']} | ⬆️ {item['seeders']}s"
        buttons.data_button(btn_text, f"jck {search_id} pick {idx}")

    # If series releases are detected, offer a dedicated Season Filter view
    has_seasons = any(r.get("season") for r in results)
    if has_seasons:
        buttons.data_button("📺 Filter by Season", f"jck {search_id} season_view")

    buttons.data_button("❌ Cancel", f"jck {search_id} cancel")
    menu = buttons.build_menu(2)

    caption = f"<b>🍿 Jackett Results for:</b> <code>{query}</code>\n\n"
    for idx, item in enumerate(results[:8]):
        caption += (
            f"<b>{idx + 1}.</b> <code>{item['title']}</code>\n"
            f"   📦 <b>Size:</b> {item['readable_size']} | ⬆️ <b>Seeds:</b> {item['seeders']}\n"
        )
    caption += "\n<i>Select an item to Mirror, Leech, or Sleech:</i>"

    await edit_message(status_msg, caption, menu)


async def execute_task(client, message, torrent_url: str, mode: str):
    """
    Triggers qBittorrent task for the given magnet/torrent link.
    mode can be: 'mirror', 'leech', or 'sleech'
    """
    # Clone incoming message structure with new link
    dummy_message = message.reply_to_message or message
    dummy_message.text = f"dummy {torrent_url}"

    if mode == "mirror":
        bot_loop.create_task(
            Mirror(client, dummy_message, is_qbit=True, is_leech=False).new_event()
        )
    elif mode == "leech":
        bot_loop.create_task(
            Mirror(client, dummy_message, is_qbit=True, is_leech=True).new_event()
        )
    elif mode == "sleech":
        bot_loop.create_task(
            Mirror(
                client,
                dummy_message,
                is_qbit=True,
                is_leech=True,
                is_stremio=True,
            ).new_event()
        )


async def process_series_queue(client, message, season_tasks: list, mode: str):
    """
    Safely download multi-season series one season at a time with cooldown rest period.
    """
    async with SERIES_QUEUE_LOCK:
        cooldown = Config.JACKETT_SERIES_COOLDOWN or 180
        total = len(season_tasks)
        for idx, task in enumerate(season_tasks, start=1):
            await send_message(
                message,
                f"🚀 <b>Starting Season [{idx}/{total}]:</b>\n"
                f"<code>{task['title']}</code>\n"
                f"Mode: <b>{mode.upper()}</b>",
            )
            await execute_task(client, message, task["url"], mode)

            if idx < total:
                wait_note = await send_message(
                    message,
                    f"⏸ <i>Waiting {cooldown} seconds cooldown before queuing next season to prevent flood limits...</i>",
                )
                await asyncio.sleep(cooldown)
                await delete_message(wait_note)


@new_task
async def jackett_callback(client, query):
    user_id = query.from_user.id
    data = query.data.split()
    # Format: jck <search_id> <action> [args...]
    if len(data) < 3:
        await query.answer("Invalid request!", show_alert=True)
        return

    search_id = data[1]
    action = data[2]

    session = SEARCH_CACHE.get(search_id)
    if not session and action != "cancel":
        await query.answer(
            "Search session expired! Please search again.", show_alert=True
        )
        return

    if session and session["user_id"] != user_id:
        await query.answer("This search menu is not yours!", show_alert=True)
        return

    message = query.message

    if action == "cancel":
        SEARCH_CACHE.pop(search_id, None)
        await query.answer("Search canceled.")
        await edit_message(message, "❌ <i>Search session canceled.</i>")
        return

    results = session["results"]

    if action == "season_view":
        # Group available results by Season
        seasons = {}
        for r in results:
            s_num = r.get("season")
            if s_num is not None and s_num not in seasons:
                seasons[s_num] = r

        buttons = ButtonMaker()
        for s_num in sorted(seasons.keys()):
            buttons.data_button(
                f"Season {s_num}", f"jck {search_id} pick_season {s_num}"
            )

        buttons.data_button(
            "📦 All Seasons (Safe Queue)", f"jck {search_id} pick_all_seasons"
        )
        buttons.data_button("⬅️ Back to List", f"jck {search_id} back_list")
        buttons.data_button("❌ Cancel", f"jck {search_id} cancel")

        await query.answer()
        await edit_message(
            message,
            f"<b>📺 TV Series Detected:</b> <code>{session['query']}</code>\n\n"
            f"Select an individual season or choose <b>All Seasons</b>.\n"
            f"<i>(All Seasons will download one season at a time with anti-flood delay)</i>",
            buttons.build_menu(2),
        )
        return

    elif action == "back_list":
        buttons = ButtonMaker()
        for idx, item in enumerate(results[:8]):
            badge = f"[S{item['season']:02d}] " if item.get("season") else ""
            btn_text = (
                f"[{idx + 1}] {badge}{item['readable_size']} | ⬆️ {item['seeders']}s"
            )
            buttons.data_button(btn_text, f"jck {search_id} pick {idx}")

        has_seasons = any(r.get("season") for r in results)
        if has_seasons:
            buttons.data_button("📺 Filter by Season", f"jck {search_id} season_view")

        buttons.data_button("❌ Cancel", f"jck {search_id} cancel")
        menu = buttons.build_menu(2)

        caption = f"<b>🍿 Jackett Results for:</b> <code>{session['query']}</code>\n\n"
        for idx, item in enumerate(results[:8]):
            caption += (
                f"<b>{idx + 1}.</b> <code>{item['title']}</code>\n"
                f"   📦 <b>Size:</b> {item['readable_size']} | ⬆️ <b>Seeds:</b> {item['seeders']}\n"
            )
        caption += "\n<i>Select an item to Mirror, Leech, or Sleech:</i>"

        await query.answer()
        await edit_message(message, caption, menu)
        return

    elif action == "pick":
        idx = int(data[3])
        item = results[idx]
        buttons = ButtonMaker()
        buttons.data_button("📥 qbMirror", f"jck {search_id} run mirror {idx}")
        buttons.data_button("📤 qbLeech", f"jck {search_id} run leech {idx}")
        buttons.data_button("📺 Sleech (Stremio)", f"jck {search_id} run sleech {idx}")
        buttons.data_button("⬅️ Back", f"jck {search_id} back_list")
        buttons.data_button("❌ Cancel", f"jck {search_id} cancel")

        details = (
            f"<b>Selected Release:</b>\n"
            f"🎬 <code>{item['title']}</code>\n"
            f"📦 <b>Size:</b> {item['readable_size']}\n"
            f"⬆️ <b>Seeders:</b> {item['seeders']}\n\n"
            f"<i>Choose action to start downloading:</i>"
        )
        await query.answer()
        await edit_message(message, details, buttons.build_menu(2))
        return

    elif action == "pick_season":
        s_num = int(data[3])
        # Find best seeded release for this season
        matched = [r for r in results if r.get("season") == s_num]
        if not matched:
            await query.answer("No release found for this season!", show_alert=True)
            return

        item = matched[0]
        item_idx = results.index(item)

        buttons = ButtonMaker()
        buttons.data_button("📥 qbMirror", f"jck {search_id} run mirror {item_idx}")
        buttons.data_button("📤 qbLeech", f"jck {search_id} run leech {item_idx}")
        buttons.data_button(
            "📺 Sleech (Stremio)", f"jck {search_id} run sleech {item_idx}"
        )
        buttons.data_button("⬅️ Back", f"jck {search_id} season_view")
        buttons.data_button("❌ Cancel", f"jck {search_id} cancel")

        details = (
            f"<b>Selected Season {s_num}:</b>\n"
            f"🎬 <code>{item['title']}</code>\n"
            f"📦 <b>Size:</b> {item['readable_size']}\n"
            f"⬆️ <b>Seeders:</b> {item['seeders']}\n\n"
            f"<i>Choose action for Season {s_num}:</i>"
        )
        await query.answer()
        await edit_message(message, details, buttons.build_menu(2))
        return

    elif action == "pick_all_seasons":
        buttons = ButtonMaker()
        buttons.data_button("📥 Mirror All", f"jck {search_id} run_all mirror")
        buttons.data_button("📤 Leech All", f"jck {search_id} run_all leech")
        buttons.data_button(
            "📺 Sleech All (Stremio)", f"jck {search_id} run_all sleech"
        )
        buttons.data_button("⬅️ Back", f"jck {search_id} season_view")
        buttons.data_button("❌ Cancel", f"jck {search_id} cancel")

        # Collect distinct season items
        seasons = {}
        for r in results:
            s_num = r.get("season")
            if s_num is not None and s_num not in seasons:
                seasons[s_num] = r

        summary_text = (
            f"<b>📦 Queue All Seasons ({len(seasons)} seasons found):</b>\n\n"
        )
        for s_num in sorted(seasons.keys()):
            summary_text += f"• Season {s_num}: {seasons[s_num]['readable_size']} (Seeds: {seasons[s_num]['seeders']})\n"

        summary_text += (
            f"\n⚠️ <i>Each season will be processed one by one with a {Config.JACKETT_SERIES_COOLDOWN or 180}s "
            f"rest period to prevent Telegram FloodWait.</i>\n\nChoose destination:"
        )

        await query.answer()
        await edit_message(message, summary_text, buttons.build_menu(2))
        return

    elif action == "run":
        mode = data[3]
        idx = int(data[4])
        item = results[idx]
        SEARCH_CACHE.pop(search_id, None)
        await query.answer(f"Starting {mode.upper()}...")
        await edit_message(
            message,
            f"✅ <b>Triggered {mode.upper()} via qBittorrent:</b>\n"
            f"<code>{item['title']}</code>\n"
            f"📦 <b>Size:</b> {item['readable_size']}",
        )
        await execute_task(client, message, item["url"], mode)

    elif action == "run_all":
        mode = data[3]
        seasons = {}
        for r in results:
            s_num = r.get("season")
            if s_num is not None and s_num not in seasons:
                seasons[s_num] = r

        if not seasons:
            await query.answer("No distinct seasons found!", show_alert=True)
            return

        sorted_tasks = [seasons[k] for k in sorted(seasons.keys())]
        SEARCH_CACHE.pop(search_id, None)
        await query.answer(f"Queued {len(sorted_tasks)} seasons for {mode.upper()}!")
        await edit_message(
            message,
            f"✅ <b>Queued {len(sorted_tasks)} seasons for {mode.upper()} via qBittorrent.</b>\n"
            f"Processing sequentially with safe cooldown...",
        )
        bot_loop.create_task(process_series_queue(client, message, sorted_tasks, mode))
