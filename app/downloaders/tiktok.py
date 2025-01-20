import re
import json
import logging
from typing import Optional, Dict, Any
from bs4 import BeautifulSoup
import aiohttp
from downloaders.base import VideoDownloader
from downloaders.types import DownloadResult, MediaType

logger = logging.getLogger(__name__)

class TikTokDownloader(VideoDownloader):
    LINK_REGEX = re.compile(r"https?://(?:vt\.)?(?:www\.)?tiktok\.com/[\w\-/.@]+")
    VIDEO_ID_REGEX = re.compile(r"/video/(\d+)")
    SHORT_LINK_REGEX = re.compile(r"https?://(?!www\.)[a-zA-Z0-9_-]+\.(?:tiktok|douyin)\.com")
    
    def can_handle(self, url: str) -> bool:
        return bool(self.LINK_REGEX.search(url) or self.SHORT_LINK_REGEX.search(url))
    
    async def download(self, url: str) -> Optional[DownloadResult]:
        try:
            if self.SHORT_LINK_REGEX.search(url):
                url = await self._resolve_short_url(url)
                if not url:
                    logger.error("Failed to resolve short URL")
                    return None
            
            # First try the new download method
            video_data = await self._download_via_tikdownloader(url)
            if video_data:
                return DownloadResult(
                    data=video_data,
                    media_type=MediaType.VIDEO
                )
                
            # If new method fails, try the old API method
            video_id = self._extract_video_id(url)
            if not video_id:
                logger.error("Failed to extract video ID")
                return None
                
            video_data = await self._download_via_api(video_id)
            if not video_data:
                return None
                
            return DownloadResult(
                data=video_data,
                media_type=MediaType.VIDEO
            )
            
        except Exception as e:
            logger.error(f"Error downloading TikTok video: {e}")
            return None
            
    async def _download_via_tikdownloader(self, url: str) -> Optional[bytes]:
        """Download video using tikdownloader.io API"""
        try:
            # First request to get download links
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": "https://tikdownloader.io",
                "Referer": "https://tikdownloader.io/en"
            }
            
            # URL encode the data parameters as shown in curl --data-raw example
            from urllib.parse import urlencode
            data = urlencode({
                "q": url,
                "lang": "en"
            })
            
            async with aiohttp.ClientSession() as session:
                # Get download links
                async with session.post(
                    "https://tikdownloader.io/api/ajaxSearch",
                    headers=headers,
                    data=data
                ) as response:
                    if response.status != 200:
                        logger.error(f"Failed to get download links: {response.status}")
                        return None
                        
                    response_data = await response.json()
                    if not response_data.get("status") == "ok":
                        logger.error("Invalid response from tikdownloader API")
                        return None
                        
                    # Parse HTML to extract download links
                    soup = BeautifulSoup(response_data["data"], "html.parser")
                    download_links = []
                    
                    # Find all download links
                    for link in soup.find_all("a", class_="tik-button-dl"):
                        href = link.get("href")
                        if not href:
                            continue
                        
                        # Determine quality from link text
                        link_text = link.get_text().strip().lower()
                        if "hd" in link_text:
                            download_links.insert(0, href)  # HD links get priority
                        elif "mp4" in link_text:
                            download_links.append(href)
                    
                    # Try downloading from available links
                    for download_url in download_links:
                        try:
                            async with session.get(download_url, headers=headers) as video_response:
                                if video_response.status == 200:
                                    return await video_response.read()
                                logger.warning(f"Failed to download from {download_url}: {video_response.status}")
                        except Exception as e:
                            logger.warning(f"Error downloading from {download_url}: {e}")
                            continue
                    
                    return None
                    
        except Exception as e:
            logger.error(f"Error in tikdownloader method: {e}")
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
        """Fallback method using ssstik API"""
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
                    else:
                        logger.error(f"API download failed, status {response.status}")
                        return None
                        
        except Exception as e:
            logger.error(f"API download failed: {e}")
            return None