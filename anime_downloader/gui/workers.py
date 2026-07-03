"""
Background workers for handling long-running tasks in the GUI.

This module provides `QThread` subclasses that perform tasks like fetching data
from the API and downloading files in the background. This prevents the GUI
from freezing and allows for progress reporting.
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from PyQt6.QtCore import QThread, pyqtSignal
from typing import List, Dict, Any
from plyer import notification
from curl_cffi import requests  # Added for TLS spoofing

from ..api import AnimePaheAPI, Downloader
from ..models import Anime, Episode
from ..utils import config_manager
from ..cli.commands import get_video_path, get_episode_dir


class UpdateCacheWorker(QThread):
    finished = pyqtSignal(int)

    def __init__(self, api: AnimePaheAPI):
        super().__init__()
        self.api = api

    def run(self):
        try:
            count = self.api.download_anime_list_cache()
            self.finished.emit(count)
        except Exception as e:
            print(f"Error in UpdateCacheWorker: {e}")
            self.finished.emit(-1)


class EpisodeWorker(QThread):
    finished = pyqtSignal(object)

    def __init__(self, api: AnimePaheAPI, anime_data: Dict[str, str]):
        super().__init__()
        self.api = api
        self.anime_data = anime_data

    def run(self):
        try:
            anime_name = self.anime_data["title"]
            anime_slug = self.anime_data["session"]
            episode_data = self.api.fetch_episode_data(anime_name, anime_slug)

            app_config = config_manager.load_config()
            download_dir = app_config["download_directory"]

            anime = Anime(name=anime_name, slug=anime_slug)
            for ep_data in episode_data:
                ep_num = int(ep_data["episode"])
                video_path = get_video_path(anime_name, ep_num, download_dir)
                episode = Episode(
                    number=ep_num,
                    session=ep_data["session"]
                )
                if os.path.exists(video_path):
                    episode.mark_as_downloaded(video_path)
                anime.episodes.append(episode)
            self.finished.emit(anime)
        except Exception as e:
            print(f"Error in EpisodeWorker: {e}")
            self.finished.emit(None)


class DownloadWorker(QThread):
    progress_update = pyqtSignal(int, int, str)
    log = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(
        self,
        api: AnimePaheAPI,
        downloader: Downloader,
        anime: Anime,
        episodes: List[Episode],
        app_config: Dict[str, Any],
    ):
        super().__init__()
        self.api = api
        self.downloader = downloader
        self.anime = anime
        self.episodes = episodes
        self.app_config = app_config

    def run(self):
        for i, episode in enumerate(self.episodes):
            try:
                self.log.emit(
                    f"Starting Ep {episode.number}/{self.episodes[-1].number} of {self.anime.name}..."
                )

                stream_url = self.api.get_stream_url(
                    self.anime.slug,
                    episode.session,
                    self.app_config["quality"],
                    self.app_config["audio"],
                )
                if not stream_url:
                    self.log.emit(f"Error: Could not find a matching stream for Ep {episode.number}")
                    continue

                playlist_url = self.api.get_playlist_url(stream_url)
                if not playlist_url:
                    self.log.emit(f"Error: Could not get playlist link for Ep {episode.number}")
                    continue

                episode_dir = get_episode_dir(
                    self.anime.name,
                    episode.number,
                    self.app_config["download_directory"],
                )
                playlist_path = self.downloader.fetch_playlist(
                    playlist_url, stream_url, episode_dir
                )
                if not playlist_path:
                    continue

                playlist_details = self.downloader.get_playlist_details(playlist_path)
                if not playlist_details:
                    self.log.emit(f"Error: Could not parse playlist for Ep {episode.number}")
                    continue

                # FIX: Download key using curl_cffi and the correct Referer
                headers = {"Referer": stream_url}
                key_response = requests.get(
                    playlist_details["key_url"], 
                    headers=headers, 
                    impersonate="chrome120"
                )
                if key_response.status_code != 200:
                    self.log.emit(f"Error: Failed to download decryption key for Ep {episode.number}")
                    continue
                key = key_response.content

                segments = playlist_details["segments"]
                segments_to_download = [
                    s for s in segments
                    if not os.path.exists(os.path.join(episode_dir, os.path.basename(urlparse(s).path)))
                ]

                if not segments_to_download:
                    self.log.emit(f"Segments for Ep {episode.number} already downloaded.")
                else:
                    self.log.emit(f"Downloading {len(segments_to_download)} segments for Ep {episode.number}...")
                    # FIX: Pass stream_url into the segment downloader
                    self._run_segment_downloads(
                        segments_to_download, playlist_details, key, episode_dir, stream_url
                    )

                self.log.emit(f"Compiling Episode {episode.number}...")
                output_path = get_video_path(
                    self.anime.name,
                    episode.number,
                    self.app_config["download_directory"],
                )

                progress_callback = lambda p: self.on_compilation_progress(p, episode.number)
                self.downloader.compile_video(episode_dir, output_path, progress_callback)
                self.log.emit(f"Finished processing Episode {episode.number}")

                try:
                    notification.notify(
                        title="Animepahe-dl",
                        message=f"Finished downloading {self.anime.name} Episode {episode.number}",
                        app_name="Animepahe Downloader",
                        timeout=10
                    )
                except Exception as e:
                    print(f"Failed to send notification: {e}")

            except Exception as e:
                self.log.emit(f"An error occurred while downloading Ep {episode.number}: {e}")
                continue

        self.finished.emit()

    def _run_segment_downloads(self, segments_to_download, playlist_details, key, episode_dir, stream_url):
        total_segments = len(segments_to_download)
        segments_done = 0
        total_bytes = 0
        start_time = time.time()

        with ThreadPoolExecutor(max_workers=self.app_config["threads"]) as executor:
            futures = []
            for seg_url in segments_to_download:
                original_index = playlist_details["segments"].index(seg_url)
                segment_index = playlist_details["media_sequence"] + original_index
                iv = segment_index.to_bytes(16, byteorder="big")
                segment_name = os.path.basename(urlparse(seg_url).path)
                output_path = os.path.join(episode_dir, segment_name)
                
                # FIX: Passed stream_url to the downloder
                futures.append(
                    executor.submit(
                        self.downloader.download_segment, seg_url, key, iv, stream_url, output_path
                    )
                )

            for future in as_completed(futures):
                bytes_downloaded = future.result()
                if bytes_downloaded > 0:
                    segments_done += 1
                    total_bytes += bytes_downloaded
                    elapsed_time = time.time() - start_time
                    speed_mbps = ((total_bytes / (1024 * 1024)) / elapsed_time if elapsed_time > 0 else 0)
                    message = f"({segments_done}/{total_segments}) {total_bytes / (1024*1024):.2f} MB @ {speed_mbps:.2f} MB/s"
                    self.progress_update.emit(segments_done, total_segments, message)

    def on_compilation_progress(self, percent: int, episode_number: int):
        message = f"Compiling Ep {episode_number}... {percent}%"
        self.progress_update.emit(percent, 100, message)


class MultiAnimeDownloadWorker(QThread):
    progress_update = pyqtSignal(int, int, str)
    log = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(
        self,
        api: AnimePaheAPI,
        downloader: Downloader,
        anime_list: List[Dict[str, str]],
        app_config: Dict[str, Any],
    ):
        super().__init__()
        self.api = api
        self.downloader = downloader
        self.anime_list = anime_list
        self.app_config = app_config

    def run(self):
        download_dir = self.app_config["download_directory"]
        all_episodes_to_download = []

        self.log.emit(f"Fetching episode data for {len(self.anime_list)} anime...")

        for anime_data in self.anime_list:
            try:
                anime_name = anime_data["title"]
                anime_slug = anime_data["session"]

                self.log.emit(f"Fetching episodes for {anime_name}...")
                episode_data = self.api.fetch_episode_data(anime_name, anime_slug)

                if not episode_data:
                    self.log.emit(f"No episodes found for {anime_name}")
                    continue

                anime = Anime(name=anime_name, slug=anime_slug)
                for ep_data in episode_data:
                    ep_num = int(ep_data["episode"])
                    video_path = get_video_path(anime_name, ep_num, download_dir)
                    episode = Episode(
                        number=ep_num,
                        session=ep_data["session"]
                    )
                    if os.path.exists(video_path):
                        episode.mark_as_downloaded(video_path)
                    anime.episodes.append(episode)

                    if not episode.is_downloaded:
                        all_episodes_to_download.append((anime, episode))

            except Exception as e:
                self.log.emit(f"Error fetching episodes for {anime_data['title']}: {e}")
                continue

        if not all_episodes_to_download:
            self.log.emit("No new episodes to download.")
            self.finished.emit()
            return

        total_episodes = len(all_episodes_to_download)
        self.log.emit(f"Starting download of {total_episodes} episodes...")

        for idx, (anime, episode) in enumerate(all_episodes_to_download, 1):
            try:
                self.log.emit(f"[{idx}/{total_episodes}] Downloading {anime.name} - Episode {episode.number}...")

                stream_url = self.api.get_stream_url(
                    anime.slug,
                    episode.session,
                    self.app_config["quality"],
                    self.app_config["audio"],
                )
                if not stream_url:
                    continue

                playlist_url = self.api.get_playlist_url(stream_url)
                if not playlist_url:
                    continue

                episode_dir = get_episode_dir(anime.name, episode.number, download_dir)
                playlist_path = self.downloader.fetch_playlist(playlist_url, stream_url, episode_dir)
                if not playlist_path:
                    continue

                playlist_details = self.downloader.get_playlist_details(playlist_path)
                if not playlist_details:
                    continue

                # FIX: Download key using curl_cffi and the correct Referer
                headers = {"Referer": stream_url}
                key_response = requests.get(
                    playlist_details["key_url"], 
                    headers=headers, 
                    impersonate="chrome120"
                )
                if key_response.status_code != 200:
                    self.log.emit(f"Error: Failed to get decryption key for {anime.name} Ep {episode.number}")
                    continue
                key = key_response.content

                segments = playlist_details["segments"]
                segments_to_download = [
                    s for s in segments
                    if not os.path.exists(os.path.join(episode_dir, os.path.basename(urlparse(s).path)))
                ]

                if segments_to_download:
                    self.log.emit(f"Downloading {len(segments_to_download)} segments...")
                    # FIX: Pass stream_url into segment downloader
                    self._run_segment_downloads(
                        segments_to_download, playlist_details, key, episode_dir, stream_url
                    )

                self.log.emit(f"Compiling {anime.name} Episode {episode.number}...")
                output_path = get_video_path(anime.name, episode.number, download_dir)
                self.downloader.compile_video(episode_dir, output_path, lambda p: None)

                self.log.emit(f"Completed {anime.name} Episode {episode.number}")
                self.progress_update.emit(idx, total_episodes, f"Completed {idx}/{total_episodes} episodes")

            except Exception as e:
                self.log.emit(f"Error downloading {anime.name} Ep {episode.number}: {e}")
                continue

        self.log.emit(f"Finished downloading {total_episodes} episodes!")
        self.finished.emit()

    def _run_segment_downloads(self, segments_to_download, playlist_details, key, episode_dir, stream_url):
        with ThreadPoolExecutor(max_workers=self.app_config["threads"]) as executor:
            futures = []
            for seg_url in segments_to_download:
                original_index = playlist_details["segments"].index(seg_url)
                segment_index = playlist_details["media_sequence"] + original_index
                iv = segment_index.to_bytes(16, byteorder="big")
                segment_name = os.path.basename(urlparse(seg_url).path)
                output_path = os.path.join(episode_dir, segment_name)
                
                # FIX: Passed stream_url to the downloder
                futures.append(
                    executor.submit(
                        self.downloader.download_segment, seg_url, key, iv, stream_url, output_path
                    )
                )

            for future in as_completed(futures):
                future.result()


class PlaybackWorker(QThread):
    finished = pyqtSignal()
    error = pyqtSignal(str)
    episode_started = pyqtSignal(int, str)

    def __init__(
        self,
        api: AnimePaheAPI,
        anime_name: str,
        anime_slug: str,
        episodes: List[tuple], 
        quality: str,
        audio: str,
        media_player: str,
    ):
        super().__init__()
        self.api = api
        self.anime_name = anime_name
        self.anime_slug = anime_slug
        self.episodes = episodes
        self.quality = quality
        self.audio = audio
        self.media_player = media_player

    def run(self):
        import subprocess

        try:
            for episode_num, episode_session in self.episodes:
                self.episode_started.emit(episode_num, self.anime_name)

                stream_url = self.api.get_stream_url(
                    self.anime_slug, episode_session, self.quality, self.audio
                )
                if not stream_url:
                    self.error.emit(f"Could not find stream for Episode {episode_num}")
                    continue

                playlist_url = self.api.get_playlist_url(stream_url)
                if not playlist_url:
                    self.error.emit(f"Could not get playlist for Episode {episode_num}")
                    continue

                try:
                    if self.media_player == "mpv":
                        cmd = [self.media_player, "--title", f"{self.anime_name} - Episode {episode_num}", "--force-window=immediate", playlist_url]
                    elif self.media_player == "vlc":
                        cmd = [self.media_player, "--intf", "qt", "--meta-title", f"{self.anime_name} - Episode {episode_num}", playlist_url]
                    elif self.media_player == "ffplay":
                        cmd = [self.media_player, "-window_title", f"{self.anime_name} - Episode {episode_num}", "-autoexit", playlist_url]
                    elif self.media_player == "mplayer":
                        cmd = [self.media_player, "-title", f"{self.anime_name} - Episode {episode_num}", playlist_url]
                    else:
                        cmd = [self.media_player, playlist_url]

                    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if self.media_player in ["ffplay"]:
                        process.wait()

                except subprocess.SubprocessError as e:
                    self.error.emit(f"Failed to launch {self.media_player}: {str(e)}")
                    continue
                except Exception as e:
                    self.error.emit(f"Unexpected error playing Episode {episode_num}: {str(e)}")
                    continue

            self.finished.emit()
        except Exception as e:
            self.error.emit(f"Playback worker error: {str(e)}")