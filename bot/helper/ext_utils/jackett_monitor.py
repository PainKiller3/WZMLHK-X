import asyncio
import io
import json
import zipfile
from os import environ
from pathlib import Path

from aiofiles import open as aiopen
from aiofiles.os import path as aiopath
from niquests import AsyncSession

from ... import LOGGER
from ...core.config_manager import Config
from .db_handler import database

JACKETT_CONFIG_PATH = environ.get("JACKETT_CONFIG_PATH", "/config/jackett")
JACKETT_TUNNEL_FILE = environ.get("JACKETT_TUNNEL_FILE", "/data/jackett_tunnel_url.txt")

# Known config roots
CANDIDATE_PATHS = [
    Path(JACKETT_CONFIG_PATH),
    Path(JACKETT_CONFIG_PATH) / "Jackett",
    Path("/jackett_config"),
    Path("/jackett_config/Jackett"),
    Path.home() / ".config" / "Jackett",
]


def _find_active_config_dir() -> Path:
    """Finds the first existing directory where Jackett stores configuration."""
    for p in CANDIDATE_PATHS:
        if p.exists() and p.is_dir():
            return p
    # Default to the first configured path
    return Path(JACKETT_CONFIG_PATH)


async def restore_jackett_from_db():
    """
    Restores Jackett indexers and configuration from MongoDB if the local
    config folder is empty/missing on a fresh VPS or deployment.
    """
    if Config.DISABLE_JACKETT or not Config.DATABASE_URL:
        return

    target_dir = _find_active_config_dir()
    indexers_dir = target_dir / "Indexers"

    # If Indexers directory already has configurations, skip restoring
    if indexers_dir.exists() and any(indexers_dir.glob("*.json")):
        LOGGER.info(
            f"jackett_monitor: Local Jackett config exists at {target_dir}. Skipping DB restore."
        )
        return

    try:
        zip_bytes = await database.restore_jackett_config()
        if not zip_bytes:
            LOGGER.info("jackett_monitor: No saved Jackett config found in MongoDB.")
            return

        LOGGER.info("jackett_monitor: Restoring Jackett configuration from MongoDB...")
        target_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            zf.extractall(target_dir)

        LOGGER.info(
            f"jackett_monitor: Successfully restored Jackett config into {target_dir} from MongoDB!"
        )
    except Exception as e:
        LOGGER.error(f"jackett_monitor: Failed to restore Jackett config from DB: {e}")


async def backup_jackett_to_db():
    """
    Zips the Jackett configuration (Indexers, ServerConfig.json)
    and saves it to MongoDB so migrating to a new VPS requires zero setup.
    """
    if Config.DISABLE_JACKETT or not Config.DATABASE_URL:
        return

    target_dir = _find_active_config_dir()
    if not target_dir.exists():
        return

    try:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in target_dir.rglob("*"):
                # Avoid caching massive logs/temp files
                if file_path.is_file() and not file_path.name.endswith(".log"):
                    arcname = file_path.relative_to(target_dir)
                    zf.write(file_path, arcname)

        zip_data = buf.getvalue()
        if zip_data:
            await database.backup_jackett_config(zip_data)
            LOGGER.info(
                f"jackett_monitor: Backed up Jackett configuration ({len(zip_data)} bytes) to MongoDB."
            )
    except Exception as e:
        LOGGER.error(f"jackett_monitor: Failed to backup Jackett config to DB: {e}")


async def _check_jackett_tunnel():
    """Reads and logs the Cloudflare tunnel URL for Jackett dashboard if present."""
    for _ in range(12):  # Poll up to 60s
        try:
            if await aiopath.isfile(JACKETT_TUNNEL_FILE):
                async with aiopen(JACKETT_TUNNEL_FILE, "r") as f:
                    url = (await f.read()).strip()
                if url:
                    LOGGER.info(
                        f"jackett_monitor: JACKETT_DASHBOARD = {url} [Cloudflare Tunnel]"
                    )
                    return url
        except Exception:
            pass
        await asyncio.sleep(5)


async def auto_configure_jackett():
    """
    Auto-detects and configures JACKETT_URL and JACKETT_API_KEY.
    Supports:
      1. MongoDB restore on fresh VPS / clean deployment
      2. Docker Compose networking (service name 'jackett')
      3. Normal Docker / Bare-metal host (localhost / 127.0.0.1)
      4. Auto-discovery of API Key from ServerConfig.json
      5. Auto-sync backup of Jackett configuration to MongoDB
    """
    if Config.DISABLE_JACKETT:
        LOGGER.info(
            "jackett_monitor: Jackett is disabled (DISABLE_JACKETT = True). Skipping auto-configuration."
        )
        return

    # 1. Restore from MongoDB on fresh VPS if needed
    await restore_jackett_from_db()

    configured_url = Config.JACKETT_URL
    configured_key = Config.JACKETT_API_KEY

    # 2. Auto-discover API Key from config if key not already set
    if not configured_key:
        cfg_paths = [p / "ServerConfig.json" for p in CANDIDATE_PATHS]
        for cfg_file in cfg_paths:
            try:
                if await aiopath.isfile(str(cfg_file)):
                    async with aiopen(str(cfg_file), "r") as f:
                        data = json.loads(await f.read())
                        api_key = data.get("APIKey")
                        if api_key:
                            Config.JACKETT_API_KEY = api_key
                            configured_key = api_key
                            LOGGER.info(
                                f"jackett_monitor: Auto-discovered API Key from {cfg_file}"
                            )
                            break
            except Exception as e:
                LOGGER.debug(f"jackett_monitor: error reading {cfg_file}: {e}")

    # 3. Check candidate URLs if JACKETT_URL is empty
    candidate_urls = []
    if configured_url:
        candidate_urls.append((configured_url.rstrip("/"), "configured"))
    else:
        candidate_urls.append(("http://jackett:9117", "docker-compose service"))
        candidate_urls.append(("http://127.0.0.1:9117", "normal-docker / localhost"))
        candidate_urls.append(("http://localhost:9117", "localhost fallback"))

    discovered_url = None
    async with AsyncSession() as session:
        for url, mode in candidate_urls:
            try:
                resp = await session.get(
                    f"{url}/api/v2.0/indexers/all/results/torznab/api", timeout=3
                )
                if resp.status_code in (200, 401, 403):
                    discovered_url = url
                    Config.JACKETT_URL = url
                    LOGGER.info(
                        f"jackett_monitor: JACKETT_URL = {url} [Detected via {mode}]"
                    )
                    break
            except Exception:
                continue

    if not discovered_url and configured_url:
        LOGGER.info(f"jackett_monitor: JACKETT_URL = {configured_url} [Configured]")
    elif not discovered_url:
        LOGGER.info(
            "jackett_monitor: JACKETT_URL not set (Neither docker-compose 'http://jackett:9117' nor 'http://127.0.0.1:9117' responded)"
        )

    # 4. If Jackett is reachable, backup current config state to MongoDB
    if discovered_url or configured_url:
        asyncio.create_task(backup_jackett_to_db())

    # 5. Check and log Cloudflare tunnel URL for Jackett dashboard if running
    asyncio.create_task(_check_jackett_tunnel())
