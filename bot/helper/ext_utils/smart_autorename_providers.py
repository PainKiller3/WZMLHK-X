from contextlib import suppress
from dataclasses import dataclass
from difflib import SequenceMatcher
from logging import getLogger
import re
from typing import Optional
from urllib.parse import quote
from httpx import AsyncClient

from bot.core.config_manager import Config
from .bot_utils import sync_to_async

LOGGER = getLogger(__name__)


def is_title_similar(query: str, res_title: str, min_ratio: float = 0.45) -> bool:
    if not query or not res_title:
        return False
    q_norm = re.sub(r"[^\w\s]", "", query.lower()).strip()
    r_norm = re.sub(r"[^\w\s]", "", res_title.lower()).strip()

    if not q_norm or not r_norm:
        return False

    q_words = set(q_norm.split())
    r_words = set(r_norm.split())

    if not q_words or not r_words:
        return False

    intersection = q_words & r_words
    overlap_q = len(intersection) / len(q_words)
    overlap_r = len(intersection) / len(r_words)

    seq_ratio = SequenceMatcher(None, q_norm, r_norm).ratio()

    if seq_ratio >= min_ratio or overlap_q >= 0.5 or overlap_r >= 0.5:
        return True

    return False


@dataclass
class CanonicalMetadata:
    title: Optional[str] = None
    year: Optional[str] = None
    series_title: Optional[str] = None
    episode_title: Optional[str] = None
    provider: Optional[str] = None
    provider_id: Optional[str] = None
    ott: Optional[str] = None


class KitsuProvider:
    KITSU_BASE = "https://kitsu.io/api/edge"

    async def _request(self, path: str, params: dict | None = None):
        try:
            async with AsyncClient(
                verify=False, follow_redirects=True, timeout=10
            ) as client:
                headers = {
                    "Accept": "application/vnd.api+json",
                    "Content-Type": "application/vnd.api+json",
                }
                resp = await client.get(
                    f"{self.KITSU_BASE}{path}", params=params, headers=headers
                )
                if resp.status_code != 200:
                    return None
                return resp.json()
        except Exception as exc:
            LOGGER.warning(f"Kitsu request failed: {exc}")
            return None

    async def find_title(
        self,
        title: str,
        year: Optional[str] = None,
        media_type: Optional[str] = None,
    ) -> Optional[CanonicalMetadata]:
        params = {"filter[text]": title, "page[limit]": "5"}
        data = await self._request("/anime", params)
        if not data or "data" not in data or not data["data"]:
            return None

        for item in data["data"]:
            attrs = item.get("attributes", {})
            titles = attrs.get("titles", {})
            canonical = (
                attrs.get("canonicalTitle")
                or titles.get("en")
                or titles.get("en_jp")
                or titles.get("ja_jp")
            )

            if not canonical:
                continue

            if is_title_similar(title, canonical):
                start_date = attrs.get("startDate") or ""
                disp_year = start_date[:4] if len(start_date) >= 4 else year
                item_id = str(item.get("id"))

                return CanonicalMetadata(
                    title=canonical,
                    year=disp_year,
                    series_title=canonical
                    if media_type in ("single_episode", "episode_range", "season_pack")
                    else None,
                    provider="kitsu",
                    provider_id=item_id,
                )

        return None

    async def find_episode(
        self,
        series_title: str,
        season: int,
        episode: int,
        year: Optional[str] = None,
    ) -> Optional[CanonicalMetadata]:
        anime_meta = await self.find_title(
            series_title, year=year, media_type="single_episode"
        )
        if not anime_meta or not anime_meta.provider_id:
            return None

        anime_id = anime_meta.provider_id
        ep_data = await self._request(
            f"/anime/{anime_id}/episodes",
            params={"filter[number]": str(episode)},
        )

        ep_title = None
        if ep_data and ep_data.get("data"):
            ep_attrs = ep_data["data"][0].get("attributes", {})
            ep_titles = ep_attrs.get("titles", {})
            candidate = (
                ep_attrs.get("canonicalTitle")
                or ep_titles.get("en_us")
                or ep_titles.get("en")
                or ep_titles.get("en_jp")
            )
            if candidate and not re.match(r"(?i)^episode\s*\d+$", candidate.strip()):
                ep_title = candidate

        return CanonicalMetadata(
            series_title=anime_meta.series_title,
            year=anime_meta.year,
            episode_title=ep_title,
            provider="kitsu",
            provider_id=anime_id,
        )


class CinemetaProvider:
    CINEMETA_BASE = "https://v3-cinemeta.strem.io"

    async def _request(self, path: str):
        try:
            async with AsyncClient(
                verify=False, follow_redirects=True, timeout=10
            ) as client:
                resp = await client.get(f"{self.CINEMETA_BASE}{path}")
                if resp.status_code != 200:
                    return None
                return resp.json()
        except Exception as exc:
            LOGGER.warning(f"Cinemeta request failed: {exc}")
            return None

    async def find_title(
        self,
        title: str,
        year: Optional[str] = None,
        media_type: Optional[str] = None,
    ) -> Optional[CanonicalMetadata]:
        stype = "movie" if media_type == "movie" else "series"
        q_encoded = quote(title.strip())
        data = await self._request(f"/catalog/{stype}/top/search={q_encoded}.json")
        if not data or "metas" not in data or not data["metas"]:
            return None

        for item in data["metas"]:
            item_name = item.get("name")
            if not item_name:
                continue

            if is_title_similar(title, item_name):
                item_year = item.get("releaseInfo") or item.get("year") or year
                if item_year:
                    item_year = str(item_year)[:4]

                return CanonicalMetadata(
                    title=item_name,
                    year=item_year,
                    series_title=item_name
                    if media_type in ("single_episode", "episode_range", "season_pack")
                    else None,
                    provider="cinemeta",
                    provider_id=item.get("id"),
                )

        return None

    async def find_episode(
        self,
        series_title: str,
        season: int,
        episode: int,
        year: Optional[str] = None,
    ) -> Optional[CanonicalMetadata]:
        meta = await self.find_title(
            series_title, year=year, media_type="single_episode"
        )
        if not meta or not meta.provider_id:
            return None

        detail = await self._request(f"/meta/series/{meta.provider_id}.json")
        ep_title = None
        if detail and "meta" in detail:
            videos = detail["meta"].get("videos", [])
            for vid in videos:
                if vid.get("season") == season and vid.get("episode") == episode:
                    candidate = vid.get("title") or vid.get("name")
                    if candidate and not re.match(
                        r"(?i)^episode\s*\d+$", candidate.strip()
                    ):
                        ep_title = candidate
                    break

        return CanonicalMetadata(
            series_title=meta.series_title,
            year=meta.year,
            episode_title=ep_title,
            provider="cinemeta",
            provider_id=meta.provider_id,
        )


class TVDBProvider:
    """Uses Cinemeta TVDB catalogue and lookup endpoints as TVDB proxy."""

    async def find_title(
        self,
        title: str,
        year: Optional[str] = None,
        media_type: Optional[str] = None,
    ) -> Optional[CanonicalMetadata]:
        cinemeta = CinemetaProvider()
        res = await cinemeta.find_title(title, year=year, media_type=media_type)
        if res:
            res.provider = "tvdb"
        return res

    async def find_episode(
        self,
        series_title: str,
        season: int,
        episode: int,
        year: Optional[str] = None,
    ) -> Optional[CanonicalMetadata]:
        cinemeta = CinemetaProvider()
        res = await cinemeta.find_episode(
            series_title, season=season, episode=episode, year=year
        )
        if res:
            res.provider = "tvdb"
        return res


class IMDbProvider:
    async def find_title(
        self,
        title: str,
        year: Optional[str] = None,
        media_type: Optional[str] = None,
    ) -> Optional[CanonicalMetadata]:
        def lookup():
            try:
                from imdbio import search_title, get_movie
            except ImportError:
                return None

            clean_q = title.strip().lower()
            if not clean_q:
                return None

            res = search_title(clean_q)
            results = getattr(res, "titles", []) or []
            if not results:
                return None

            if year:
                matched_year = [
                    item
                    for item in results
                    if str(getattr(item, "year", "") or "") == str(year)
                ]
                if matched_year:
                    results = matched_year

            if media_type == "movie":
                preferred = [
                    item for item in results if getattr(item, "kind", None) == "movie"
                ]
            elif media_type in ("single_episode", "episode_range", "season_pack"):
                preferred = [
                    item
                    for item in results
                    if getattr(item, "kind", None)
                    in ("tvSeries", "tvMiniSeries", "tvShow")
                ]
            else:
                preferred = [
                    item
                    for item in results
                    if getattr(item, "kind", None)
                    in ("movie", "tvSeries", "tvMiniSeries")
                ]

            targets = preferred or results
            target = None
            for item in targets:
                item_title = getattr(item, "title", None)
                if item_title and is_title_similar(title, item_title):
                    target = item
                    break

            if not target:
                return None

            item_id = getattr(target, "id", None)
            if not item_id:
                return None

            m = get_movie(item_id)
            if not m:
                return None

            disp_title = getattr(m, "title", None) or getattr(target, "title", None)
            if not disp_title or not is_title_similar(title, disp_title):
                return None

            disp_year = (
                str(getattr(m, "year", None) or getattr(target, "year", None) or "")
                or None
            )

            return CanonicalMetadata(
                title=disp_title,
                year=disp_year,
                series_title=disp_title
                if media_type in ("single_episode", "episode_range", "season_pack")
                else None,
                provider="imdb",
                provider_id=f"tt{item_id}",
            )

        try:
            return await sync_to_async(lookup)
        except Exception as exc:
            LOGGER.warning(f"IMDb Smart Rename lookup failed: {exc}")
            return None

    async def find_episode(
        self,
        series_title: str,
        season: int,
        episode: int,
        year: Optional[str] = None,
    ) -> Optional[CanonicalMetadata]:
        def lookup():
            try:
                from imdbio import search_title, get_movie
            except ImportError:
                return None

            clean_q = series_title.strip().lower()
            if not clean_q:
                return None

            res = search_title(clean_q)
            results = getattr(res, "titles", []) or []
            if not results:
                return None

            if year:
                matched_year = [
                    item
                    for item in results
                    if str(getattr(item, "year", "") or "") == str(year)
                ]
                if matched_year:
                    results = matched_year

            series_matches = [
                item
                for item in results
                if getattr(item, "kind", None) in ("tvSeries", "tvMiniSeries", "tvShow")
            ] or results

            target = None
            for item in series_matches:
                item_title = getattr(item, "title", None)
                if item_title and is_title_similar(series_title, item_title):
                    target = item
                    break

            if not target:
                return None

            series_id = getattr(target, "id", None)
            if not series_id:
                return None

            s_obj = get_movie(series_id)
            series_disp = (
                getattr(s_obj, "title", None)
                if s_obj
                else getattr(target, "title", None)
            )

            if not series_disp or not is_title_similar(series_title, series_disp):
                return None

            ep_title = None
            if s_obj and hasattr(s_obj, "episodes"):
                with suppress(Exception):
                    episodes_data = getattr(s_obj, "episodes", None)
                    if episodes_data:
                        se_eps = episodes_data.get(season, {})
                        ep_obj = se_eps.get(episode)
                        if ep_obj:
                            ep_title = getattr(ep_obj, "title", None)

            return CanonicalMetadata(
                series_title=series_disp,
                episode_title=ep_title,
                provider="imdb",
                provider_id=f"tt{series_id}",
            )

        try:
            return await sync_to_async(lookup)
        except Exception as exc:
            LOGGER.warning(f"IMDb episode lookup failed: {exc}")
            return None


TMDB_API_BASE = "https://api.themoviedb.org/3"


class TMDbProvider:
    async def _request(self, path: str, params: dict | None = None):
        token = str(getattr(Config, "TMDB_ACCESS_TOKEN", "") or "").strip()
        if not token:
            return None

        params = dict(params or {})
        headers = {"accept": "application/json"}

        if len(token) < 50:
            params["api_key"] = token
        else:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with AsyncClient(
                verify=False, follow_redirects=True, timeout=15
            ) as client:
                resp = await client.get(
                    f"{TMDB_API_BASE}{path}",
                    params=params,
                    headers=headers,
                )
                if resp.status_code != 200:
                    return None
                return resp.json()
        except Exception as exc:
            LOGGER.warning(f"TMDb Smart Rename request failed: {exc}")
            return None

    async def find_title(
        self,
        title: str,
        year: Optional[str] = None,
        media_type: Optional[str] = None,
    ) -> Optional[CanonicalMetadata]:
        if not str(getattr(Config, "TMDB_ACCESS_TOKEN", "") or "").strip():
            return None

        endpoint = (
            "/search/movie"
            if media_type == "movie"
            else "/search/tv"
            if media_type in ("single_episode", "episode_range", "season_pack")
            else "/search/multi"
        )
        params = {"query": title, "include_adult": "false", "language": "en-US"}
        if year and media_type == "movie":
            params["year"] = str(year)
        elif year and media_type != "movie":
            params["first_air_date_year"] = str(year)

        data = await self._request(endpoint, params)
        if not data:
            return None

        results = [
            r for r in data.get("results", []) if r.get("media_type") != "person"
        ]
        if not results:
            return None

        item = None
        for r in results:
            disp = (
                r.get("title")
                or r.get("name")
                or r.get("original_title")
                or r.get("original_name")
            )
            if disp and is_title_similar(title, disp):
                item = r
                break

        if not item:
            return None

        disp_title = (
            item.get("title")
            or item.get("name")
            or item.get("original_title")
            or item.get("original_name")
        )
        rel_date = item.get("release_date") or item.get("first_air_date") or ""
        disp_year = rel_date[:4] if len(rel_date) >= 4 else None

        networks = None
        if item.get("media_type") == "tv" or media_type in (
            "single_episode",
            "episode_range",
            "season_pack",
        ):
            tv_details = await self._request(f"/tv/{item['id']}")
            if tv_details and tv_details.get("networks"):
                nets = [
                    n.get("name")
                    for n in tv_details.get("networks", [])
                    if n.get("name")
                ]
                if nets:
                    networks = nets[0]

        return CanonicalMetadata(
            title=disp_title,
            year=disp_year,
            series_title=disp_title
            if media_type in ("single_episode", "episode_range", "season_pack")
            else None,
            provider="tmdb",
            provider_id=str(item.get("id")),
            ott=networks,
        )

    async def find_episode(
        self,
        series_title: str,
        season: int,
        episode: int,
        year: Optional[str] = None,
    ) -> Optional[CanonicalMetadata]:
        if not str(getattr(Config, "TMDB_ACCESS_TOKEN", "") or "").strip():
            return None

        params = {"query": series_title, "include_adult": "false", "language": "en-US"}
        if year:
            params["first_air_date_year"] = str(year)

        search_data = await self._request("/search/tv", params)
        if not search_data or not search_data.get("results"):
            return None

        tv_item = None
        for r in search_data["results"]:
            disp = r.get("name") or r.get("original_name")
            if disp and is_title_similar(series_title, disp):
                tv_item = r
                break

        if not tv_item:
            return None

        tv_id = tv_item["id"]
        series_disp = tv_item.get("name") or tv_item.get("original_name")

        ep_data = await self._request(f"/tv/{tv_id}/season/{season}/episode/{episode}")
        ep_title = None
        if ep_data and ep_data.get("name"):
            ep_title = ep_data.get("name")

        return CanonicalMetadata(
            series_title=series_disp,
            episode_title=ep_title,
            provider="tmdb",
            provider_id=str(tv_id),
        )


class CanonicalMetadataResolver:
    def __init__(self):
        self.kitsu = KitsuProvider()
        self.tvdb = TVDBProvider()
        self.tmdb = TMDbProvider()
        self.imdb = IMDbProvider()
        self.cinemeta = CinemetaProvider()
        self.cache: dict[tuple, CanonicalMetadata] = {}

    def _get_provider_chain(
        self, media_type: Optional[str], is_anime: bool = False
    ) -> list:
        has_tmdb = bool(str(getattr(Config, "TMDB_ACCESS_TOKEN", "") or "").strip())

        if is_anime:
            # 🇯🇵 Anime: Kitsu > TVDB > TMDB (if token) > IMDb > Cinemeta
            chain = [self.kitsu, self.tvdb]
            if has_tmdb:
                chain.append(self.tmdb)
            chain.extend([self.imdb, self.cinemeta])
            return chain

        if media_type == "movie":
            # 🎬 Movies: TMDB (if token) > IMDb > Cinemeta
            chain = []
            if has_tmdb:
                chain.append(self.tmdb)
            chain.extend([self.imdb, self.cinemeta])
            return chain

        # 📺 Series: TVDB > TMDB (if token) > IMDb > Cinemeta
        chain = [self.tvdb]
        if has_tmdb:
            chain.append(self.tmdb)
        chain.extend([self.imdb, self.cinemeta])
        return chain

    async def resolve_title(
        self,
        title: str,
        year: Optional[str] = None,
        media_type: Optional[str] = None,
        is_anime: bool = False,
    ) -> Optional[CanonicalMetadata]:
        cache_key = (
            title.strip().lower(),
            str(year or ""),
            str(media_type or ""),
            bool(is_anime),
            "title",
        )
        if cache_key in self.cache:
            return self.cache[cache_key]

        providers = self._get_provider_chain(media_type, is_anime)
        res = None
        for provider in providers:
            res = await provider.find_title(title, year, media_type)
            if res and res.title:
                break

        if res:
            if len(self.cache) > 200:
                self.cache.clear()
            self.cache[cache_key] = res
        return res

    async def resolve_episode(
        self,
        series_title: str,
        season: int,
        episode: int,
        year: Optional[str] = None,
        is_anime: bool = False,
    ) -> Optional[CanonicalMetadata]:
        cache_key = (
            series_title.strip().lower(),
            season,
            episode,
            str(year or ""),
            bool(is_anime),
            "episode",
        )
        if cache_key in self.cache:
            return self.cache[cache_key]

        providers = self._get_provider_chain("single_episode", is_anime)
        res = None
        for provider in providers:
            res = await provider.find_episode(series_title, season, episode, year)
            if res and res.series_title:
                if res.episode_title:
                    break

        if res:
            if len(self.cache) > 200:
                self.cache.clear()
            self.cache[cache_key] = res
        return res
