"""
Handles the core logic for downloading, decrypting, and compiling video segments.
"""

import os
import re
import shutil
import subprocess
import shutil as _shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Callable
from urllib.parse import urlparse

from Crypto.Cipher import AES
from tqdm import tqdm

from ..utils import constants, logger
from .client import AnimePaheAPI
from ..core.signal_handler import is_shutdown_requested

from curl_cffi import requests


class Downloader:
    def __init__(self, api: AnimePaheAPI):
        self.api = api

    def download_segment(self, url: str, key: bytes, iv: bytes, kwik_stream_url: str, output_path: str, max_retries: int = 5) -> int:
        if os.path.exists(output_path):
            try:
                return os.path.getsize(output_path)
            except OSError:
                return 0

        segment_name = os.path.basename(urlparse(url).path)
        headers = {"Referer": kwik_stream_url}
        
        encrypted_data = None
        
        # Retry loop for handling 429s and connection drops
        for attempt in range(max_retries):
            try:
                response = requests.get(
                    url, 
                    headers=headers, 
                    impersonate="chrome120",
                    timeout=15  # Added a timeout to prevent hanging threads
                )
                
                if response.status_code == 200:
                    encrypted_data = response.content
                    break  # Success! Break out of the retry loop
                    
                elif response.status_code == 429:
                    wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s, 8s...
                    logger.warning(f"Rate limited (429) on {segment_name}. Retrying in {wait_time}s... ({attempt + 1}/{max_retries})")
                    time.sleep(wait_time)
                    
                else:
                    logger.error(f"Failed to download {segment_name} (Status: {response.status_code}). Retrying... ({attempt + 1}/{max_retries})")
                    time.sleep(1)
                    
            except Exception as e:
                logger.warning(f"Connection error on {segment_name}: {e}. Retrying... ({attempt + 1}/{max_retries})")
                time.sleep(2)

        # If it failed all 5 retries, return 0 to flag it as failed
        if not encrypted_data:
            logger.error(f"Max retries reached. Gave up on segment: {segment_name}")
            return 0

        # Decrypt logic remains the same...
        while len(encrypted_data) % 16 != 0:
            encrypted_data += b"\0"

        try:
            cipher = AES.new(key, AES.MODE_CBC, iv)
            decrypted_data = cipher.decrypt(encrypted_data)
            with open(output_path, "wb") as f:
                f.write(decrypted_data)
            return len(encrypted_data)
        except Exception as e:
            logger.error(f"Failed to decrypt segment {segment_name}: {e}")
            return 0

    def fetch_playlist(self, playlist_url: str, kwik_stream_url: str, output_dir: str) -> Optional[str]:
        os.makedirs(output_dir, exist_ok=True)
        file_path = os.path.join(output_dir, "playlist.m3u8")

        if os.path.exists(file_path):
            logger.info(f"Resuming: Playlist already exists: {file_path}")
            return file_path

        headers = {"Referer": kwik_stream_url}
        response = requests.get(
            playlist_url, 
            headers=headers, 
            impersonate="chrome120"
        )
        
        if response.status_code == 200:
            with open(file_path, "wb") as f:
                f.write(response.content)
            return file_path
        else:
            logger.error(f"Failed to download m3u8 playlist from: {playlist_url} (Status: {response.status_code})")
            return None

    def get_playlist_details(self, playlist_path: str) -> Optional[dict]:
        if not os.path.exists(playlist_path):
            return None

        key_url, segments, media_sequence, total_duration = "", [], 0, 0.0
        with open(playlist_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#EXT-X-MEDIA-SEQUENCE"):
                    media_sequence = int(line.split(":")[1])
                elif line.startswith("#EXT-X-KEY"):
                    key_url_match = re.search('URI="([^"]+)"', line)
                    if key_url_match:
                        key_url = key_url_match.group(1)
                elif line.startswith("#EXTINF:"):
                    try:
                        total_duration += float(line.split(":")[1].split(",")[0])
                    except (ValueError, IndexError):
                        continue
                elif line.startswith("https"):
                    segments.append(line)

        if not key_url or not segments:
            logger.error("Playlist parsing failed: Could not find key URL or segments.")
            return None

        return {
            "key_url": key_url,
            "segments": segments,
            "media_sequence": media_sequence,
            "duration": total_duration,
        }

    def download_from_playlist_cli(self, playlist_path: str, kwik_stream_url: str, num_threads: int) -> bool:
        playlist_details = self.get_playlist_details(playlist_path)
        if not playlist_details:
            logger.error(f"Could not parse playlist at {playlist_path}")
            return False

        headers = {"Referer": kwik_stream_url}
        key_response = requests.get(
            playlist_details["key_url"], 
            headers=headers, 
            impersonate="chrome120"
        )
        
        if key_response.status_code != 200:
            logger.error("Failed to download decryption key.")
            return False
            
        key = key_response.content
        episode_dir = os.path.dirname(playlist_path)
        segments = playlist_details["segments"]

        segments_to_download = [
            s for s in segments
            if not os.path.exists(os.path.join(episode_dir, os.path.basename(urlparse(s).path)))
        ]

        if not segments_to_download:
            logger.info("All segments already downloaded.")
            return True

        total_bytes_downloaded = 0
        start_time = time.time()

        with tqdm(total=len(segments_to_download), unit="seg", desc="Downloading") as pbar:
            with ThreadPoolExecutor(max_workers=num_threads) as executor:
                future_to_segment = {}
                for seg_url in segments_to_download:
                    original_index = segments.index(seg_url)
                    segment_index = playlist_details["media_sequence"] + original_index
                    iv = segment_index.to_bytes(16, byteorder="big")
                    segment_name = os.path.basename(urlparse(seg_url).path)
                    output_path = os.path.join(episode_dir, segment_name)
                    
                    future = executor.submit(self.download_segment, seg_url, key, iv, kwik_stream_url, output_path)
                    future_to_segment[future] = {
                        'url': seg_url,
                        'name': segment_name,
                        'index': original_index
                    }

                failed_segments = []
                for future in as_completed(future_to_segment):
                    if is_shutdown_requested():
                        logger.info("Shutdown requested, stopping segment downloads")
                        break
                    
                    segment_info = future_to_segment[future]
                    segment_name = segment_info['name']
                    
                    try:
                        bytes_downloaded = future.result()
                        if bytes_downloaded > 0:
                            pbar.update(1)
                            total_bytes_downloaded += bytes_downloaded
                            elapsed_time = time.time() - start_time
                            speed_mbps = ((total_bytes_downloaded / (1024 * 1024)) / elapsed_time if elapsed_time > 0 else 0)
                            pbar.set_postfix_str(f"{speed_mbps:.2f} MB/s")
                        else:
                            logger.error(f"Failed to download segment: {segment_name}")
                            failed_segments.append(segment_name)
                            pbar.update(1) 
                    except Exception as e:
                        logger.error(f"Exception downloading segment {segment_name}: {e}")
                        failed_segments.append(segment_name)
                        pbar.update(1) 
                
                if failed_segments:
                    logger.warning(f"Failed to download {len(failed_segments)} segments.")
                    return False
                
        return True


    def compile_video(
        self,
        segment_dir: str,
        output_path: str,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> bool:
        playlist_path = os.path.join(segment_dir, "playlist.m3u8")
        file_list_path = os.path.join(segment_dir, "file.list")
        playlist_details = self.get_playlist_details(playlist_path)
        total_duration = playlist_details.get("duration", 0.0) if playlist_details else 0.0

        with open(file_list_path, "w", encoding="utf-8") as f_out:
            if playlist_details:
                for segment_url in playlist_details.get("segments", []):
                    segment_name = os.path.basename(urlparse(segment_url).path)
                    f_out.write(f"file '{segment_name}'\n")

        ffmpeg_bin = os.environ.get("FFMPEG") or _shutil.which("ffmpeg")
        if not ffmpeg_bin:
            logger.error("ffmpeg not found in PATH and FFMPEG env var not set. Cannot compile video.")
            return False

        cmd = [
            ffmpeg_bin, "-y", "-f", "concat", "-safe", "0", "-i",
            file_list_path, "-c", "copy", output_path, "-progress", "pipe:1"
        ]

        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, bufsize=1
        )

        for line in iter(process.stdout.readline, ""):
            if "time=" in line and total_duration > 0 and progress_callback:
                match = re.search(r"time=(\d{2}):(\d{2}):(\d{2})\.(\d{2})", line)
                if match:
                    h, m, s, ms = map(int, match.groups())
                    current_time = h * 3600 + m * 60 + s + ms / 100
                    percent = int((current_time / total_duration) * 100)
                    progress_callback(min(percent, 100))

        process.wait()

        if process.returncode == 0:
            shutil.rmtree(segment_dir)
            if progress_callback:
                progress_callback(100)
            return True
        else:
            logger.error(f"Failed to compile video. FFmpeg exited with code {process.returncode}.")
            return False

    def compile_video(
        self,
        segment_dir: str,
        output_path: str,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> bool:
        playlist_path = os.path.join(segment_dir, "playlist.m3u8")
        file_list_path = os.path.join(segment_dir, "file.list")
        playlist_details = self.get_playlist_details(playlist_path)
        total_duration = playlist_details.get("duration", 0.0) if playlist_details else 0.0

        with open(file_list_path, "w", encoding="utf-8") as f_out:
            if playlist_details:
                for segment_url in playlist_details.get("segments", []):
                    segment_name = os.path.basename(urlparse(segment_url).path)
                    f_out.write(f"file '{segment_name}'\n")

        ffmpeg_bin = os.environ.get("FFMPEG") or _shutil.which("ffmpeg")
        if not ffmpeg_bin:
            logger.error("ffmpeg not found in PATH and FFMPEG env var not set. Cannot compile video.")
            return False

        cmd = [
            ffmpeg_bin, "-y", "-f", "concat", "-safe", "0", "-i",
            file_list_path, "-c", "copy", output_path, "-progress", "pipe:1"
        ]

        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, bufsize=1
        )

        for line in iter(process.stdout.readline, ""):
            if "time=" in line and total_duration > 0 and progress_callback:
                match = re.search(r"time=(\d{2}):(\d{2}):(\d{2})\.(\d{2})", line)
                if match:
                    h, m, s, ms = map(int, match.groups())
                    current_time = h * 3600 + m * 60 + s + ms / 100
                    percent = int((current_time / total_duration) * 100)
                    progress_callback(min(percent, 100))

        process.wait()

        if process.returncode == 0:
            shutil.rmtree(segment_dir)
            if progress_callback:
                progress_callback(100)
            return True
        else:
            logger.error(f"Failed to compile video. FFmpeg exited with code {process.returncode}.")
            return False