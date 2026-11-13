import json
from os import environ
from pathlib import Path

from aiofiles import open as aiopen
from aiofiles.os import path as aiopath
from niquests import AsyncSession

from ... import LOGGER
from ...core.config_manager import Config

JACKETT_CONFIG_PATH = environ.get("JACKETT_CONFIG_PATH", "/config/jackett")


async def auto_configure_jackett():
    """
    Auto-detects and configures JACKETT_URL and JACKETT_API_KEY.
    Supports:
      1. Docker Compose networking (service name 'jackett')
      2. Normal Docker / Bare-metal host (localhost / 127.0.0.1)
      3. Shared volume auto-discovery of API Key from Jackett's ServerConfig.json
    """
    if Config.DISABLE_JACKETT:
        LOGGER.info(
            "jackett_monitor: Jackett is disabled (DISABLE_JACKETT = True). Skipping auto-configuration."
        )
        return
    configured_url = Config.JACKETT_URL
    configured_key = Config.JACKETT_API_KEY

    # 1. Auto-discover API Key from mounted config if key not already set
    if not configured_key:
        cfg_paths = [
            Path(JACKETT_CONFIG_PATH) / "ServerConfig.json",
            Path(JACKETT_CONFIG_PATH) / "Jackett" / "ServerConfig.json",
            Path("/jackett_config/ServerConfig.json"),
            Path("/jackett_config/Jackett/ServerConfig.json"),
            Path.home() / ".config" / "Jackett" / "ServerConfig.json",
        ]
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

    # 2. Check candidate URLs if JACKETT_URL is empty or default
    candidate_urls = []
    if configured_url:
        candidate_urls.append((configured_url.rstrip("/"), "configured"))
    else:
        # Docker Compose service name candidate
        candidate_urls.append(("http://jackett:9117", "docker-compose service"))
        # Normal Docker host / local candidate
        candidate_urls.append(("http://127.0.0.1:9117", "normal-docker / localhost"))
        candidate_urls.append(("http://localhost:9117", "localhost fallback"))

    discovered_url = None
    async with AsyncSession() as session:
        for url, mode in candidate_urls:
            try:
                resp = await session.get(
                    f"{url}/api/v2.0/indexers/all/results/torznab/api", timeout=3
                )
                # Jackett returns 401 Unauthorized or 200/XML when up, confirming reachability
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
