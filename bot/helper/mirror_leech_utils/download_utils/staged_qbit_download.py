from asyncio import Event, gather, sleep
from contextlib import suppress
from os import link as hardlink
from pathlib import Path
from shutil import disk_usage
from time import time

from aiofiles.os import makedirs, path as aiopath, remove
from aioqbt.api import AddFormBuilder, StopCondition

from .... import (
    DOWNLOAD_DIR,
    LOGGER,
    non_queued_up,
    queue_dict_lock,
    task_dict,
    task_dict_lock,
)
from ....core.config_manager import Config
from ....core.torrent_manager import TorrentManager
from ...ext_utils.bot_utils import sync_to_async
from ...ext_utils.files_utils import clean_download
from ...ext_utils.status_utils import get_readable_file_size
from ...ext_utils.task_manager import (
    check_running_tasks,
    limit_checker,
    start_from_queued,
)
from ...telegram_helper.message_utils import send_message, send_status_message
from ..status_utils.staged_qbit_status import StagedQbitStatus
from ..status_utils.qbit_status import QbittorrentStatus
from .staged_qbit_planner import StagedFile, plan_batch, preflight_error, usable_space


FILE_PRIORITY_SKIP = 0
FILE_PRIORITY_NORMAL = 1


class StagedQbitCoordinator:
    def __init__(
        self, listener, torrent_hash: str, content_root: str, torrent_root_name: str
    ):
        self.listener = listener
        self.hash = torrent_hash
        self.content_root = content_root
        self.torrent_root_name = torrent_root_name
        self.files: list[StagedFile] = []
        self.pending: list[StagedFile] = []
        self.current_batch: list[StagedFile] = []
        self.completed_bytes = 0
        self.completed_files = 0
        self.total_bytes = 0
        self.phase = "Preparing"
        self.speed = 0
        self.current_bytes = 0
        self.cancelled = False
        self._queue_event = None
        self.active_uploader = None
        self.payload_started = False
        self._stage_dir = f"{self.listener.dir}-stage"

    async def cancel(self):
        self.cancelled = True
        self.listener.is_cancelled = True
        if self._queue_event is not None:
            self._queue_event.set()
        if self.active_uploader is not None and hasattr(
            self.active_uploader, "cancel_task"
        ):
            with suppress(Exception):
                await self.active_uploader.cancel_task()
        if TorrentManager.qbittorrent is not None:
            await TorrentManager.qbittorrent.torrents.stop([self.hash])
            await TorrentManager.qbittorrent.torrents.delete([self.hash], True)
            await TorrentManager.qbittorrent.torrents.delete_tags(
                [f"{self.listener.mid}"]
            )

    async def _free(self):
        return (await sync_to_async(disk_usage, DOWNLOAD_DIR)).free

    async def load_manifest(self):
        raw_files = await TorrentManager.qbittorrent.torrents.files(self.hash)
        selected = [
            StagedFile(f.index, f.name, f.size) for f in raw_files if f.priority != 0
        ]
        free = await self._free()
        budget = usable_space(free, Config.STAGED_TORRENT_STORAGE_PERCENT)
        max_upload_size = self.listener.max_split_size if self.listener.is_leech else 0
        if error := preflight_error(selected, budget, max_upload_size):
            if "safe free storage" in error and selected:
                largest = max(selected, key=lambda item: item.size)
                error = (
                    f"File '{largest.name}' ({get_readable_file_size(largest.size)}) "
                    f"is larger than safe free storage ({get_readable_file_size(budget)})."
                )
            elif "upload destination" in error:
                error += " Staged mode cannot split files because splitting needs extra storage."
            raise ValueError(error)
        self.files = selected
        self.pending = selected.copy()
        self.total_bytes = sum(item.size for item in selected)
        self.listener.size = self.total_bytes
        if limit_message := await limit_checker(self.listener):
            raise ValueError(limit_message)

    async def _set_batch_priorities(self, batch: list[StagedFile]):
        all_ids = [item.index for item in self.files]
        await TorrentManager.qbittorrent.torrents.file_prio(
            self.hash, all_ids, FILE_PRIORITY_SKIP
        )
        await TorrentManager.qbittorrent.torrents.file_prio(
            self.hash, [item.index for item in batch], FILE_PRIORITY_NORMAL
        )

    async def _wait_for_batch(self):
        previous = 0
        while not self.cancelled and not self.listener.is_cancelled:
            info = await TorrentManager.qbittorrent.torrents.files(
                self.hash, [item.index for item in self.current_batch]
            )
            self.current_bytes = sum(int(item.size * item.progress) for item in info)
            self.speed = max(0, self.current_bytes - previous) // 3
            previous = self.current_bytes
            if info and all(item.progress >= 1 for item in info):
                return
            await sleep(3)
        raise RuntimeError("Staged torrent was cancelled.")

    async def _make_stage_tree(self):
        await clean_download(self._stage_dir)
        root = Path(self._stage_dir) / self.torrent_root_name
        for item in self.current_batch:
            source = Path(self.content_root) / item.name
            target = Path(self._stage_dir) / item.name
            await makedirs(str(target.parent), exist_ok=True)
            await sync_to_async(hardlink, source, target)
        return str(root if root.exists() else Path(self._stage_dir))

    async def _delete_batch(self):
        failures = []
        for item in self.current_batch:
            path = f"{self.content_root}/{item.name}"
            if await aiopath.exists(path):
                try:
                    await remove(path)
                except Exception as error:
                    failures.append(f"{item.name}: {error}")
        await clean_download(self._stage_dir)
        if failures:
            raise RuntimeError(
                "Could not delete uploaded files: " + "; ".join(failures)
            )

    async def run(self):
        try:
            await self.load_manifest()
            async with task_dict_lock:
                task_dict[self.listener.mid] = StagedQbitStatus(self.listener, self)
            await send_status_message(self.listener.message)
            while self.pending and not self.listener.is_cancelled:
                budget = usable_space(
                    await self._free(), Config.STAGED_TORRENT_STORAGE_PERCENT
                )
                self.current_batch = plan_batch(self.pending, budget)
                if not self.current_batch:
                    raise RuntimeError(
                        "Available storage dropped below the size of every pending file."
                    )
                self.phase = "Downloading batch"
                self.current_bytes = 0
                await self._set_batch_priorities(self.current_batch)
                self.payload_started = True
                await TorrentManager.qbittorrent.torrents.start([self.hash])
                await self._wait_for_batch()
                await TorrentManager.qbittorrent.torrents.stop([self.hash])
                self.phase = "Uploading batch"
                upload_queued, upload_event = await check_running_tasks(
                    self.listener, "up"
                )
                await start_from_queued()
                if upload_queued:
                    self._queue_event = upload_event
                    await upload_event.wait()
                    if self.listener.is_cancelled:
                        raise RuntimeError("Staged torrent was cancelled.")
                    self._queue_event = None
                stage_root = await self._make_stage_tree()
                await self.listener.upload_staged_batch(stage_root, self)
                if self.listener.staged_upload_error:
                    raise RuntimeError(self.listener.staged_upload_error)
                await self._delete_batch()
                batch_bytes = sum(item.size for item in self.current_batch)
                self.completed_bytes += batch_bytes
                self.completed_files += len(self.current_batch)
                self.active_uploader = None
                batch_ids = {item.index for item in self.current_batch}
                self.pending = [
                    item for item in self.pending if item.index not in batch_ids
                ]
                self.current_batch = []
                async with queue_dict_lock:
                    non_queued_up.discard(self.listener.mid)
                await start_from_queued()
                if self.pending:
                    download_queued, download_event = await check_running_tasks(
                        self.listener, "dl"
                    )
                    if download_queued:
                        self.phase = "Queued for next batch"
                        self._queue_event = download_event
                        await download_event.wait()
                        if self.listener.is_cancelled:
                            raise RuntimeError("Staged torrent was cancelled.")
                        self._queue_event = None
            if self.listener.is_cancelled:
                raise RuntimeError("Staged torrent was cancelled.")
            self.phase = "Finalizing"
            await TorrentManager.qbittorrent.torrents.delete([self.hash], True)
            await TorrentManager.qbittorrent.torrents.delete_tags(
                [f"{self.listener.mid}"]
            )
            await self.listener.staged_complete()
        except Exception as error:
            LOGGER.error(f"Staged torrent failed: {error}")
            clean_local = self.listener.is_cancelled or not self.payload_started
            if TorrentManager.qbittorrent is not None:
                await gather(
                    TorrentManager.qbittorrent.torrents.stop([self.hash]),
                    TorrentManager.qbittorrent.torrents.delete(
                        [self.hash], clean_local
                    ),
                    TorrentManager.qbittorrent.torrents.delete_tags(
                        [f"{self.listener.mid}"]
                    ),
                    return_exceptions=True,
                )
            await self.listener.staged_error(
                "Stopped by user!" if self.listener.is_cancelled else str(error),
                cleanup=clean_local,
            )


async def add_staged_qb_torrent(listener, path):
    if Config.DISABLE_TORRENTS:
        await listener.on_download_error("Torrents are disabled in the configuration.")
        return
    try:
        form = AddFormBuilder.with_client(TorrentManager.qbittorrent)
        if await aiopath.exists(listener.link):
            from aiofiles import open as aiopen

            async with aiopen(listener.link, "rb") as torrent_file:
                form = form.include_file(await torrent_file.read())
        else:
            form = form.include_url(listener.link)
        add_to_queue, event = await check_running_tasks(listener)
        form = (
            form.savepath(path)
            .tags([f"{listener.mid}"])
            .stopped(add_to_queue)
            .stop_condition(StopCondition.METADATA_RECEIVED)
        )
        await TorrentManager.qbittorrent.torrents.add(form.build())
        info = []
        while not info:
            info = await TorrentManager.qbittorrent.torrents.info(tag=f"{listener.mid}")
            await sleep(1)
    except Exception as error:
        await listener.on_download_error(f"Unable to add staged torrent: {error}")
        return
    torrent = info[0]
    listener.name = listener.name or torrent.name
    listener.staged_hash = torrent.hash
    async with task_dict_lock:
        task_dict[listener.mid] = QbittorrentStatus(listener, queued=add_to_queue)
    await listener.on_download_start()
    if add_to_queue:
        await event.wait()
        if listener.is_cancelled:
            return
        await TorrentManager.qbittorrent.torrents.start([torrent.hash])
    metadata_started = time()
    while True:
        torrent = (
            await TorrentManager.qbittorrent.torrents.info(hashes=[torrent.hash])
        )[0]
        if torrent.state not in ("metaDL", "checkingResumeData"):
            break
        if (
            Config.TORRENT_TIMEOUT
            and time() - metadata_started >= Config.TORRENT_TIMEOUT
        ):
            await TorrentManager.qbittorrent.torrents.delete([torrent.hash], True)
            await listener.on_download_error("Torrent metadata timed out.")
            return
        await sleep(1)
    await TorrentManager.qbittorrent.torrents.stop([torrent.hash])
    if listener.select:
        from ...ext_utils.bot_utils import bt_selection_buttons

        listener.staged_selection_event = Event()
        await send_message(
            listener.message,
            "<b>Download Paused!</b>\n\nSelect files and press <b>Done Selecting</b> to start staged downloading.",
            bt_selection_buttons(torrent.hash),
        )
        await listener.staged_selection_event.wait()
        if listener.is_cancelled:
            return
    content_root = torrent.content_path.rsplit("/", 1)[0]
    torrent_root_name = torrent.content_path.rsplit("/", 1)[-1]
    coordinator = StagedQbitCoordinator(
        listener, torrent.hash, content_root, torrent_root_name
    )
    listener.staged_coordinator = coordinator
    await coordinator.run()
