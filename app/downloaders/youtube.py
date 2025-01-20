import logging
from typing import Optional, Dict, Any
import yt_dlp
from pathlib import Path
from downloaders.base import VideoDownloader
from downloaders.types import MediaType, DownloadResult

logger = logging.getLogger(__name__)

class YouTubeShortsDownloader(VideoDownloader):
    MAX_DURATION = 60  # seconds
    
    def can_handle(self, url: str) -> bool:
        return "youtube.com" in url.lower() or "youtu.be" in url.lower()
    
    async def download(self, url: str) -> Optional[DownloadResult]:
        try:
            # First check video duration and get info
            video_info = await self._get_video_info(url)
            if not video_info:
                return None
                
            if video_info.get('duration', 0) > self.MAX_DURATION:
                logger.info("Video longer than 60 seconds, skipping")
                return None
                
            # Download the video
            video_data = await self._download_video(url, video_info)
            if not video_data:
                return None
                
            return DownloadResult(
                data=video_data,
                media_type=MediaType.VIDEO,
                caption=video_info.get('title')  # Add video title as caption
            )
            
        except Exception as e:
            logger.error(f"Error downloading YouTube video: {e}")
            return None
            
    async def _get_video_info(self, url: str) -> Optional[Dict[str, Any]]:
        """Get video information without downloading"""
        try:
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'skip_download': True,
                'cookies': str(self.config.cookies_file),
                'proxy': self.config.yt_proxy
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)
                
        except Exception as e:
            logger.error(f"Error getting video info: {e}")
            return None
            
    async def _download_video(self, url: str, video_info: Dict[str, Any]) -> Optional[bytes]:
        """Download the video and return its bytes"""
        try:
            output_path = self.config.temp_dir / f"yt_{video_info['id']}.mp4"
            
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'outtmpl': str(output_path),
                'cookies': str(self.config.cookies_file),
                'proxy': self.config.yt_proxy,
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                'merge_output_format': 'mp4',
                # Post processors for ensuring we get a proper MP4
                'postprocessors': [{
                    'key': 'FFmpegVideoConvertor',
                    'preferedformat': 'mp4',
                }],
                # Additional FFmpeg options for better compatibility
                'ffmpeg_options': {
                    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                    'options': '-vcodec copy -acodec copy'
                }
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
                
            if not output_path.exists():
                logger.error("Output file not found after download")
                return None
                
            # Read the file and clean up
            try:
                with open(output_path, 'rb') as f:
                    data = f.read()
                    
                output_path.unlink()  # Clean up the temporary file
                return data
                
            except Exception as read_error:
                logger.error(f"Error reading downloaded file: {read_error}")
                if output_path.exists():
                    output_path.unlink()  # Clean up on error
                return None
                
        except Exception as e:
            logger.error(f"Error downloading video: {e}")
            # Clean up any partial downloads
            if 'output_path' in locals() and output_path.exists():
                output_path.unlink()
            return None