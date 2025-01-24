import re
import json
import logging
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from bs4 import BeautifulSoup
import aiohttp
import asyncio
import websockets
from urllib.parse import urlencode
from downloaders.base import VideoDownloader
from media_types import DownloadResult, MediaType

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

@dataclass
class DownloadInfo:
    download_url: str
    headers: Dict[str, str]

class TikTokDownloader(VideoDownloader):
    LINK_REGEX = re.compile(r"https?://(?:vt\.)?(?:www\.)?tiktok\.com/[\w\-/.@]+")
    VIDEO_ID_REGEX = re.compile(r"/video/(\d+)")
    SHORT_LINK_REGEX = re.compile(r"https?://(?!www\.)[a-zA-Z0-9_-]+\.(?:tiktok|douyin)\.com")

    def __init__(self, config):
        super().__init__(config)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://tikdownloader.io",
            "Referer": "https://tikdownloader.io/en"
        }

    def can_handle(self, url: str) -> bool:
        return bool(self.LINK_REGEX.search(url) or self.SHORT_LINK_REGEX.search(url))

    async def download(self, url: str) -> Optional[List[DownloadResult]]:
        """Main download method that orchestrates the download process."""
        logger.debug(f"Starting download process for URL: {url}")
        try:
            if self.SHORT_LINK_REGEX.search(url):
                logger.debug("Detected short URL, attempting to resolve")
                url = await self._resolve_short_url(url)
                if not url:
                    logger.error("Failed to resolve short URL")
                    return None

            # Try tikdownloader.io method
            logger.debug("Attempting download via tikdownloader.io")
            results = await self._download_via_tikdownloader(url)
            if results:
                return results

            # Fallback to old API method
            logger.debug("Tikdownloader method failed, falling back to API method")
            video_id = self._extract_video_id(url)
            if not video_id:
                logger.error("Failed to extract video ID")
                return None

            video_data = await self._download_via_api(video_id)
            if not video_data:
                return None
                
            return [DownloadResult(
                data=video_data,
                media_type=MediaType.VIDEO
            )]

        except Exception as e:
            logger.error(f"Error downloading TikTok video: {e}", exc_info=True)
            return None

    async def _download_via_tikdownloader(self, url: str) -> Optional[List[DownloadResult]]:
        """Download content using tikdownloader.io service."""
        try:
            logger.debug("Starting download via tikdownloader.io")
            async with aiohttp.ClientSession() as session:
                html_data = await self._fetch_tikdownloader_data(url, session)
                if not html_data:
                    return None

                results = []
                soup = BeautifulSoup(html_data, "html.parser")
                logger.debug("Parsing HTML for content downloads")

                # First try to get video download links
                video_links = self._parse_download_links(html_data)
                if video_links:
                    await self._try_download_video(session, video_links[0], results)
                else:
                    # If no video, try to get both audio and photos as they usually come together
                    logger.debug("No video links found, trying to find audio and photos")
                    
                    # Try to get photos first
                    await self._try_download_photos(session, soup, results)
                    
                    # Then try to get music from dl-action section
                    music_link = soup.find("div", class_="dl-action").find("a", attrs={
                        "class": "tik-button-dl button dl-success",
                        "href": lambda h: h and "dl.snapcdn.app/get" in h
                    })

                    if music_link and music_link.get("href"):
                        logger.debug(f"Found music link: {music_link['href']}")
                        await self._try_download_music(session, music_link["href"], results)
                    else:
                        logger.debug("No music link found in the dl-action section")

                return results if results else None

        except Exception as e:
            logger.error(f"Error in tikdownloader method: {e}", exc_info=True)
            return None

    async def _try_download_video(
        self,
        session: aiohttp.ClientSession,
        link_info: DownloadInfo,
        results: List[DownloadResult]
    ) -> None:
        """Try to download video content."""
        try:
            logger.debug(f"Attempting video download from URL: {link_info.download_url}")
            async with session.get(
                link_info.download_url,
                headers=link_info.headers,
                timeout=30
            ) as response:
                if response.status == 200:
                    video_data = await response.read()
                    results.append(DownloadResult(
                        data=video_data,
                        media_type=MediaType.VIDEO
                    ))
        except Exception as e:
            logger.debug(f"Video download failed: {e}")

    async def _try_download_music(
        self,
        session: aiohttp.ClientSession,
        url: str,
        results: List[DownloadResult]
    ) -> None:
        """Try to download music content."""
        try:
            headers = {
                "User-Agent": self.headers["User-Agent"],
                "Accept": "*/*",
                "Referer": "https://tikdownloader.io/",
                "Origin": "https://tikdownloader.io",
                "Connection": "keep-alive",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "cross-site",
            }
            logger.debug(f"Attempting music download from URL: {url}")
            async with session.get(url, headers=headers, timeout=30) as response:
                if response.status == 200:
                    music_data = await response.read()
                    content_type = response.headers.get('Content-Type', '')
                    content_length = response.headers.get('Content-Length', '0')
                    logger.debug(f"Music download successful - Type: {content_type}, Size: {content_length} bytes")
                    results.append(DownloadResult(
                        data=music_data,
                        media_type=MediaType.AUDIO,
                        caption="Audio track"
                    ))
                else:
                    logger.debug(f"Music download failed with status: {response.status}")
                    logger.debug(await response.text())
        except Exception as e:
            logger.debug(f"Music download failed: {e}", exc_info=True)

    async def _try_download_photos(
        self,
        session: aiohttp.ClientSession,
        soup: BeautifulSoup,
        results: List[DownloadResult]
    ) -> None:
        """Try to download photo content."""
        try:
            photo_links = soup.find_all("a", attrs={
                "class": lambda c: c and "btn-premium" in c,
                "href": lambda h: h and "dl.snapcdn.app/get" in h
            })
            
            for link in photo_links:
                try:
                    async with session.get(
                        link["href"],
                        headers=self.headers,
                        timeout=30
                    ) as response:
                        if response.status == 200:
                            photo_data = await response.read()
                            results.append(DownloadResult(
                                data=photo_data,
                                media_type=MediaType.PHOTO
                            ))
                except Exception as e:
                    logger.debug(f"Photo download failed: {e}")
                    continue

        except Exception as e:
            logger.debug(f"Error processing photos: {e}")

    async def _fetch_tikdownloader_data(self, url: str, session: aiohttp.ClientSession) -> Optional[str]:
        """Fetch initial data from tikdownloader.io."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
            "Accept": "*/*",
            "Accept-Language": "en,en-US;q=0.8,ru-RU;q=0.5,ru;q=0.3",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://tikdownloader.io",
            "Referer": "https://tikdownloader.io/en",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

        data = {"q": url, "lang": "en"}
        async with session.post(
            "https://tikdownloader.io/api/ajaxSearch",
            headers=headers,
            data=data
        ) as response:
            if response.status != 200:
                logger.error(f"Initial request failed: {response.status}")
                return None

            json_response = await response.json()
            return json_response["data"] if json_response.get("status") == "ok" else None

    async def _resolve_short_url(self, url: str) -> Optional[str]:
        """Resolve TikTok short URL to full URL."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.head(url, allow_redirects=True) as response:
                    return str(response.url)
        except Exception as e:
            logger.error(f"Error resolving short URL: {e}", exc_info=True)
            return None

    def _extract_video_id(self, url: str) -> Optional[str]:
        """Extract video ID from TikTok URL."""
        match = self.VIDEO_ID_REGEX.search(url)
        return match.group(1) if match else None

    async def _download_via_api(self, video_id: str) -> Optional[bytes]:
        """Fallback method using ssstik API."""
        try:
            logger.debug(f"Attempting fallback API download for video ID: {video_id}")
            download_url = f"https://tikcdn.io/ssstik/{video_id}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.tiktok.com/"
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(download_url, headers=headers) as response:
                    if response.status == 200:
                        content_type = response.headers.get('Content-Type', 'unknown')
                        content_length = response.headers.get('Content-Length', 'unknown')
                        logger.debug(f"Download response headers - Content-Type: {content_type}, Content-Length: {content_length}")
                        
                        video_data = await response.read()
                        size_mb = len(video_data) / (1024 * 1024)
                        logger.debug(f"Successfully downloaded video via API, size: {size_mb:.2f} MB")
                        return video_data
                        
                    logger.error(f"API download failed, status {response.status}")
                    return None

        except Exception as e:
            logger.error(f"API download failed: {e}", exc_info=True)
            return None

    def _parse_download_links(self, html_content: str) -> List[DownloadInfo]:
        """Parse download links from HTML content."""
        logger.debug("Starting to parse download links from HTML content")
        soup = BeautifulSoup(html_content, "html.parser")
        download_links = []

        for link in soup.find_all("a", class_="tik-button-dl"):
            href = link.get("href")
            if not href or "#" in href:
                continue

            link_text = link.get_text().strip().lower()
            download_info = DownloadInfo(
                download_url=href,
                headers=self.headers
            )
            
            if "hd" in link_text:
                download_links.insert(0, download_info)
            elif "mp4" in link_text and "convert" not in link.get("class", []):
                download_links.append(download_info)

        logger.debug(f"Found {len(download_links)} download links")
        return download_links