import re
import logging
from typing import Optional
import aiohttp
from downloaders.base import VideoDownloader

logger = logging.getLogger(__name__)

class TikTokDownloader(VideoDownloader):
    LINK_REGEX = re.compile(r"https?://(?:vt\.)?(?:www\.)?tiktok\.com/[\w\-/.@]+")
    VIDEO_ID_REGEX = re.compile(r"/video/(\d+)")
    SHORT_LINK_REGEX = re.compile(r"https?://(?!www\.)[a-zA-Z0-9_-]+\.(?:tiktok|douyin)\.com")
    
    def can_handle(self, url: str) -> bool:
        return bool(self.LINK_REGEX.search(url) or self.SHORT_LINK_REGEX.search(url))
    
    async def download(self, url: str) -> Optional[bytes]:
        try:
            if self.SHORT_LINK_REGEX.search(url):
                url = await self._resolve_short_url(url)
                if not url:
                    logger.error("Failed to resolve short URL")
                    return None
                    
            video_id = self._extract_video_id(url)
            if not video_id:
                logger.error("Failed to extract video ID")
                return None
                
            # First try the API method
            video_data = await self._download_via_api(video_id)
            if video_data:
                return video_data
                
            # Fallback to direct download if API fails
            return await self._download_direct(url)
            
        except Exception as e:
            logger.error(f"Error downloading TikTok video: {e}")
            return None
            
    async def _resolve_short_url(self, url: str) -> Optional[str]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.head(url, allow_redirects=True) as response:
                    return str(response.url)
        except Exception as e:
            logger.error(f"Error resolving short URL: {e}")
            return None
            
    def _extract_video_id(self, url: str) -> Optional[str]:
        match = self.VIDEO_ID_REGEX.search(url)
        return match.group(1) if match else None
        
    async def _download_via_api(self, video_id: str) -> Optional[bytes]:
        """Try downloading through ssstik API"""
        try:
            download_url = f"https://tikcdn.io/ssstik/{video_id}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36",
                "Referer": "https://www.tiktok.com/"
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(download_url, headers=headers) as response:
                    if response.status == 200:
                        return await response.read()
            return None
            
        except Exception as e:
            logger.error(f"API download failed: {e}")
            return None
            
    async def _download_direct(self, url: str) -> Optional[bytes]:
        """Fallback method - direct download attempt"""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36",
                "Referer": "https://www.tiktok.com/"
            }
            return await self._download_file(url, headers=headers)
        except Exception as e:
            logger.error(f"Direct download failed: {e}")
            return None