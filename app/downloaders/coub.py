import re
import asyncio
import logging
from typing import Optional, Dict
import aiohttp
from pathlib import Path
from downloaders.base import VideoDownloader
from downloaders.types import MediaType, DownloadResult

logger = logging.getLogger(__name__)

class CoubDownloader(VideoDownloader):
    LINK_REGEX = re.compile(r"https?://coub\.com/view/(\w+)")
    API_URL = "https://coub.com/api/v2/coubs/{coub_id}"
    
    def can_handle(self, url: str) -> bool:
        return bool(self.LINK_REGEX.search(url))
    
    async def download(self, url: str) -> Optional[DownloadResult]:
        try:
            coub_data = await self._fetch_coub_data(url)
            if not coub_data or not coub_data.get("file_versions"):
                logger.error("Failed to fetch Coub data")
                return None
            
            # Get video and audio URLs
            video_url = coub_data["file_versions"]["html5"]["video"]["high"]["url"]
            audio_url = coub_data["file_versions"]["html5"]["audio"]["high"]["url"]
            
            # Download both streams
            video_data = await self._download_file(video_url)
            audio_data = await self._download_file(audio_url)
            
            if not video_data or not audio_data:
                return None
            
            # Save temporary files
            temp_video = self.config.temp_dir / f"coub_video_{coub_data['id']}.mp4"
            temp_audio = self.config.temp_dir / f"coub_audio_{coub_data['id']}.mp3"
            temp_output = self.config.temp_dir / f"coub_output_{coub_data['id']}.mp4"
            
            await self._save_temp_files(temp_video, video_data, temp_audio, audio_data)
            
            # Merge with ffmpeg
            success = await self._merge_audio_video(temp_video, temp_audio, temp_output)
            if not success:
                return None
            
            # Read result
            with open(temp_output, "rb") as f:
                result_data = f.read()
            
            # Cleanup
            await self._cleanup_temp_files(temp_video, temp_audio, temp_output)
            
            return DownloadResult(
                data=result_data,
                media_type=MediaType.VIDEO,
                caption=coub_data.get('title')  # Add Coub title as caption
            )
            
        except Exception as e:
            logger.error(f"Error downloading Coub video: {e}")
            return None
    
    async def _fetch_coub_data(self, url: str) -> Optional[Dict]:
        try:
            coub_id = self.LINK_REGEX.search(url).group(1)
            api_url = self.API_URL.format(coub_id=coub_id)
            
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url) as response:
                    response.raise_for_status()
                    return await response.json()
        except Exception as e:
            logger.error(f"Error fetching Coub data: {e}")
            return None
    
    async def _save_temp_files(
        self, 
        video_path: Path, 
        video_data: bytes, 
        audio_path: Path, 
        audio_data: bytes
    ) -> None:
        video_path.write_bytes(video_data)
        audio_path.write_bytes(audio_data)
    
    async def _merge_audio_video(
        self, 
        video_path: Path, 
        audio_path: Path, 
        output_path: Path
    ) -> bool:
        try:
            # Use ffmpeg to loop video until audio ends, with H.265 compression
            cmd = (
                f"ffmpeg -y -stream_loop -1 -i {video_path} -i {audio_path} "
                f"-c:v copy -crf 28 -c:a aac -shortest {output_path}"
            )
            
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                logger.error(f"FFmpeg error: {stderr.decode()}")
                return False
                
            return True
            
        except Exception as e:
            logger.error(f"Error merging audio and video: {e}")
            return False
    
    async def _cleanup_temp_files(self, *paths: Path) -> None:
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except Exception as e:
                logger.error(f"Error cleaning up {path}: {e}")