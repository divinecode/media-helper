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

            # Try the new download method first
            logger.debug("Attempting download via tikdownloader.io")
            video_data = await self._download_via_tikdownloader(url)
            if video_data:
                logger.debug("Successfully downloaded video via tikdownloader.io")
                return [DownloadResult(
                    data=video_data,
                    media_type=MediaType.VIDEO
                )]

            # Fallback to old API method
            logger.debug("Tikdownloader method failed, falling back to API method")
            video_id = self._extract_video_id(url)
            if not video_id:
                logger.error("Failed to extract video ID")
                return None

            video_data = await self._download_via_api(video_id)
            if not video_data:
                return None
                
            # Log file details for debugging Telegram upload issues
            size_mb = len(video_data) / (1024 * 1024)
            logger.info(f"Downloaded video size: {size_mb:.2f} MB")
            if size_mb > 50:
                logger.warning(f"Video size ({size_mb:.2f} MB) exceeds Telegram's recommended limit of 50 MB")

            return [DownloadResult(
                data=video_data,
                media_type=MediaType.VIDEO
            )]

        except Exception as e:
            logger.error(f"Error downloading TikTok video: {e}", exc_info=True)
            return None

    async def _download_via_tikdownloader(self, url: str) -> Optional[bytes]:
        """Download video using tikdownloader.io service."""
        try:
            logger.debug("Starting download via tikdownloader.io")
            async with aiohttp.ClientSession() as session:
                # Get download information
                download_infos = await self._get_download_info(url, session)
                if not download_infos:
                    logger.debug("No download information found")
                    return None

                # Try each download URL
                for download_info in download_infos:
                    try:
                        logger.debug(f"Attempting download from URL: {download_info.download_url}")
                        async with session.get(
                            download_info.download_url,
                            headers=download_info.headers,
                            timeout=30
                        ) as response:
                            if response.status == 200:
                                content_type = response.headers.get('Content-Type', 'unknown')
                                content_length = response.headers.get('Content-Length', 'unknown')
                                logger.debug(f"Download response headers - Content-Type: {content_type}, Content-Length: {content_length}")
                                
                                video_data = await response.read()
                                size_mb = len(video_data) / (1024 * 1024)
                                logger.debug(f"Successfully downloaded video, size: {size_mb:.2f} MB")
                                return video_data
                            logger.debug(f"Download failed with status: {response.status}")
                    except Exception as e:
                        logger.debug(f"Error downloading from URL: {e}", exc_info=True)
                        continue

                return None

        except Exception as e:
            logger.error(f"Error in tikdownloader method: {e}", exc_info=True)
            return None

    async def _get_download_info(self, url: str, session: aiohttp.ClientSession) -> Optional[List[DownloadInfo]]:
        """Get download information from tikdownloader.io."""
        try:
            data = urlencode({"q": url, "lang": "en"})
            
            logger.debug(f"Making request to tikdownloader.io API with data: {data}")
            logger.debug(f"Request headers: {json.dumps(self.headers, indent=2)}")
            
            async with session.post(
                "https://tikdownloader.io/api/ajaxSearch",
                headers=self.headers,
                data=data
            ) as response:
                logger.debug(f"Received response with status: {response.status}")
                if response.status != 200:
                    logger.error(f"Failed to get download info: {response.status}")
                    response_text = await response.text()
                    logger.debug(f"Error response body: {response_text}")
                    return None

                response_data = await response.json()
                if response_data.get("status") != "ok":
                    logger.error("Invalid response from tikdownloader API")
                    return None

                return self._parse_download_links(response_data["data"])

        except Exception as e:
            logger.error(f"Error getting download info: {e}", exc_info=True)
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

    async def _handle_video_conversion(
        self,
        session: aiohttp.ClientSession,
        convert_data: Dict[str, Any]
    ) -> Optional[str]:
        """Handle video conversion process."""
        try:
            async with session.post(
                "https://s1.tik-cdn.com/api/json/convert",
                headers=self.headers,
                data=convert_data,
                timeout=30
            ) as convert_response:
                logger.debug(f"Conversion response status: {convert_response.status}")
                if convert_response.status != 200:
                    response_text = await convert_response.text()
                    logger.debug(f"Conversion error response: {response_text}")
                    return None

                convert_result = await convert_response.json()
                logger.debug(f"Conversion result: {json.dumps(convert_result, indent=2)}")
                if convert_result.get("status") != "success":
                    return None

                if convert_result.get("statusCode") == 200:
                    return convert_result.get("result")

                if convert_result.get("statusCode") == 300 and convert_result.get("jobId"):
                    return await self._wait_for_conversion(convert_result["jobId"])

                return None

        except Exception as e:
            logger.error(f"Error in video conversion: {e}", exc_info=True)
            return None

    async def _wait_for_conversion(self, job_id: str) -> Optional[str]:
        """Wait for video conversion to complete via WebSocket."""
        logger.debug(f"Starting WebSocket connection for job ID: {job_id}")
        ws_url = f"wss://s1.tik-cdn.com/sub/{job_id}?fname=TikDownloader.io"
        ws_headers = {
            **self.headers,
            "Sec-WebSocket-Version": "13",
            "Sec-WebSocket-Extensions": "permessage-deflate",
            "Connection": "keep-alive, Upgrade",
            "Sec-Fetch-Dest": "websocket",
            "Sec-Fetch-Mode": "websocket",
            "Sec-Fetch-Site": "cross-site"
        }

        try:
            async with websockets.connect(
                ws_url,
                extra_headers=ws_headers,
                subprotocols=['tik-cdn'],
                compression=None,
                max_size=None,
                close_timeout=10
            ) as websocket:
                return await self._handle_websocket_messages(websocket)

        except Exception as e:
            logger.error(f"WebSocket error: {e}", exc_info=True)
            return None

    async def _handle_websocket_messages(self, websocket: websockets.WebSocketClientProtocol) -> Optional[str]:
        """Handle WebSocket messages during conversion."""
        try:
            async def recv_message():
                while True:
                    try:
                        message = await websocket.recv()
                        logger.debug(f"WebSocket message received: {message}")

                        update = json.loads(message)
                        if update.get("action") == "success" and update.get("url"):
                            return update["url"]
                        elif update.get("action") == "progress":
                            logger.debug(f"Conversion progress: {update.get('value')}%")

                    except websockets.exceptions.ConnectionClosed:
                        logger.error("WebSocket connection closed")
                        return None

            return await asyncio.wait_for(recv_message(), timeout=30)

        except asyncio.TimeoutError:
            logger.error("WebSocket operation timed out")
            return None

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