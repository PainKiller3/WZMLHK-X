from json import dumps

from niquests import AsyncSession

from .. import LOGGER
from .config_manager import Config

TOKEN_URL = "https://www.seedr.cc/oauth_test/token.php"
RESOURCE_URL = "https://www.seedr.cc/oauth_test/resource.php"
CLIENT_ID = "seedr_chrome"


class SeedrClient:
    def __init__(self):
        self._access_token = ""
        self._refresh_token = ""
        self.is_connected = False
        self.error = "Seedr Credentials not provided!"

    async def login(self):
        if not Config.SEEDR_EMAIL or not Config.SEEDR_PASSWORD:
            self.is_connected = False
            self.error = "Seedr Credentials not provided!"
            raise ValueError(self.error)
        self.error = ""
        result = await self._token_request(
            {
                "username": Config.SEEDR_EMAIL,
                "password": Config.SEEDR_PASSWORD,
                "grant_type": "password",
                "client_id": CLIENT_ID,
                "type": "login",
            }
        )
        if "access_token" not in result:
            self.error = f"Seedr Login Failed: {result}"
            raise ValueError(self.error)
        self._access_token = result["access_token"]
        self._refresh_token = result.get("refresh_token", "")
        self.is_connected = True
        return result

    async def _token_request(self, payload):
        async with AsyncSession(timeout=30) as client:
            resp = await client.post(TOKEN_URL, data=payload)
            return resp.json()

    async def _refresh(self):
        if not self._refresh_token:
            return False
        try:
            result = await self._token_request(
                {
                    "refresh_token": self._refresh_token,
                    "grant_type": "refresh_token",
                    "client_id": CLIENT_ID,
                }
            )
        except Exception as e:
            LOGGER.error(f"Seedr token refresh failed: {e}")
            return False
        if "access_token" not in result:
            LOGGER.error(f"Seedr token refresh failed: {result}")
            return False
        self._access_token = result["access_token"]
        self._refresh_token = result.get("refresh_token", self._refresh_token)
        return True

    async def _api(self, func, payload):
        async with AsyncSession(timeout=30) as client:
            resp = await client.post(
                RESOURCE_URL,
                params={"access_token": self._access_token, "func": func},
                data=payload,
            )
            result = resp.json()
        if result.get("error") == "expired_token" and await self._refresh():
            async with AsyncSession(timeout=30) as client:
                resp = await client.post(
                    RESOURCE_URL,
                    params={"access_token": self._access_token, "func": func},
                    data=payload,
                )
                result = resp.json()
        return result

    async def add_torrent(self, magnet):
        result = await self._api(
            "add_torrent", {"torrent_magnet": magnet, "folder_id": "0"}
        )
        if (
            result.get("error")
            or result.get("result") is False
            or result.get("status_code")
        ):
            raise ValueError(f"Seedr add_torrent failed: {result}")
        return result

    async def list_contents(self, content_id="0"):
        return await self._api(
            "list_contents", {"content_type": "folder", "content_id": str(content_id)}
        )

    async def fetch_file(self, folder_file_id):
        result = await self._api("fetch_file", {"folder_file_id": str(folder_file_id)})
        if isinstance(result, str):
            return result
        if (
            result.get("error")
            or result.get("result") is False
            or not result.get("url")
        ):
            raise ValueError(f"Seedr fetch_file failed: {result}")
        return result["url"]

    async def delete(self, item_type, item_id):
        return await self._api(
            "delete", {"delete_arr": dumps([{"type": item_type, "id": item_id}])}
        )


seedr = SeedrClient()
