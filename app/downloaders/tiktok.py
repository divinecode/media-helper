import re
import json
import logging
import time
import hashlib
from typing import Optional, List, Tuple, Dict
from bs4 import BeautifulSoup
import aiohttp
import asyncio
import websockets
from urllib.parse import urlencode, quote, unquote
from downloaders.base import VideoDownloader
from downloaders.types import DownloadResult, MediaType

logger = logging.getLogger(__name__)

class TikTokDownloader(VideoDownloader):
    LINK_REGEX = re.compile(r"https?://(?:vt\.)?(?:www\.)?tiktok\.com/[\w\-/.@]+")
    VIDEO_ID_REGEX = re.compile(r"/video/(\d+)")
    SHORT_LINK_REGEX = re.compile(r"https?://(?!www\.)[a-zA-Z0-9_-]+\.(?:tiktok|douyin)\.com")
    
    def can_handle(self, url: str) -> bool:
        return bool(self.LINK_REGEX.search(url) or self.SHORT_LINK_REGEX.search(url))
    
    async def download(self, url: str) -> Optional[List[DownloadResult]]:
        try:
            if self.SHORT_LINK_REGEX.search(url):
                url = await self._resolve_short_url(url)
                if not url:
                    logger.error("Failed to resolve short URL")
                    return None
            
            # First try the new download method
            video_data = await self._download_via_tikdownloader(url)
            if video_data:
                return [DownloadResult(
                    data=video_data,
                    media_type=MediaType.VIDEO
                )]
                
            # If new method fails, try the old API method
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
            logger.error(f"Error downloading TikTok video: {e}")
            return None

    async def _download_via_tikdownloader(self, url: str) -> Optional[bytes]:
        """Download video using tikdownloader.io API with conversion support"""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": "https://tikdownloader.io",
                "Referer": "https://tikdownloader.io/en"
            }
            
            # URL encode the data parameters
            from urllib.parse import urlencode
            data = urlencode({
                "q": url,
                "lang": "en"
            })
            
            async with aiohttp.ClientSession() as session:
                # Get download information
                logger.debug("Making initial search request:")
                logger.debug(f"URL: https://tikdownloader.io/api/ajaxSearch")
                logger.debug(f"Headers: {json.dumps(headers, indent=2)}")
                logger.debug(f"Data: {json.dumps(data, indent=2)}")
                
                async with session.post(
                    "https://tikdownloader.io/api/ajaxSearch",
                    headers=headers,
                    data=data
                ) as response:
                    if response.status != 200:
                        logger.error(f"Failed to get download info: {response.status}")
                        return None
                        
                    response_data = await response.json()
                    if not response_data.get("status") == "ok":
                        logger.error("Invalid response from tikdownloader API")
                        return None
                    
                    soup = BeautifulSoup(response_data["data"], "html.parser")
                    download_links = []
                    
                    # First check for direct download links
                    for link in soup.find_all("a", class_="tik-button-dl"):
                        href = link.get("href")
                        if not href or "#" in href:  # Skip conversion buttons
                            continue
                        
                        link_text = link.get_text().strip().lower()
                        if "hd" in link_text:
                            download_links.insert(0, href)
                        elif "mp4" in link_text and "convert" not in link.get("class", []):
                            download_links.append(href)
                    
                    # If no direct links, try conversion
                    if not download_links:
                        convert_link = soup.find("a", id="ConvertToVideo")
                        if convert_link:
                            # Get conversion parameters
                            audio_url = convert_link.get("data-audiourl")
                            image_data = convert_link.get("data-imagedata")
                            tiktok_id = soup.find("input", {"id": "TikTokId"})
                            
                            # Extract conversion tokens from script
                            script_tag = soup.find("script", string=re.compile("k_exp"))
                            if script_tag:
                                script_content = script_tag.string
                                k_exp_match = re.search(r'k_exp = "([^"]+)"', script_content)
                                k_token_match = re.search(r'k_token = "([^"]+)"', script_content)
                                k_url_match = re.search(r'k_url_convert = "([^"]+)"', script_content)
                                
                                if all([k_exp_match, k_token_match, k_url_match, audio_url, image_data, tiktok_id]):
                                    try:
                                        # Prepare conversion request data
                                        v_id = tiktok_id.get("value")
                                        convert_data = {
                                            "ftype": "mp4",
                                            "v_id": v_id,
                                            "audioUrl": audio_url,
                                            "audioType": "audio/mp3",
                                            "imageUrl": image_data,
                                            "fquality": "1080p",
                                            "fname": "TikDownloader.io",
                                            "exp": k_exp_match.group(1),
                                            "token": k_token_match.group(1)
                                        }
                                        
                                        logger.debug(f"Conversion request data:")
                                        logger.debug(json.dumps(convert_data, indent=2))
                                        
                                        # Initial conversion request
                                        conversion_headers = {
                                            **headers,
                                            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                                            "Origin": "https://tikdownloader.io",
                                            "Referer": "https://tikdownloader.io/"
                                        }
                                        
                                        logger.debug(f"Conversion request headers:")
                                        logger.debug(json.dumps(conversion_headers, indent=2))
                                        
                                        async with session.post(
                                            "https://s1.tik-cdn.com/api/json/convert",
                                            headers=conversion_headers,
                                            data=convert_data,
                                            timeout=30
                                        ) as convert_response:
                                            convert_text = await convert_response.text()
                                            logger.debug(f"Conversion response status: {convert_response.status}")
                                            logger.debug(f"Conversion response headers:")
                                            logger.debug(json.dumps(dict(convert_response.headers), indent=2))
                                            logger.debug(f"Conversion response body:")
                                            logger.debug(convert_text)
                                            
                                            if convert_response.status == 200:
                                                convert_result = json.loads(convert_text)
                                                if convert_result.get("status") == "success":
                                                    if convert_result.get("statusCode") == 200 and convert_result.get("result"):
                                                        # Direct URL available
                                                        download_url = convert_result["result"]
                                                        logger.debug(f"Got direct download URL: {download_url}")
                                                        download_links.append(download_url)
                                                    elif convert_result.get("statusCode") == 300 and convert_result.get("jobId"):
                                                        # Need to wait for conversion
                                                        job_id = convert_result["jobId"]
                                                        logger.debug(f"Got conversion job ID: {job_id}")
                                                        
                                                        # Connect to WebSocket for status updates
                                                        ws_url = f"wss://s1.tik-cdn.com/sub/{job_id}?fname=TikDownloader.io"
                                                        ws_headers = {
                                                            "User-Agent": headers["User-Agent"],
                                                            "Origin": "https://tikdownloader.io",
                                                            "Sec-WebSocket-Version": "13",
                                                            "Sec-WebSocket-Extensions": "permessage-deflate",
                                                            "Accept": "*/*",
                                                            "Accept-Language": "en,en-US;q=0.8,ru-RU;q=0.5,ru;q=0.3",
                                                            "Accept-Encoding": "gzip, deflate, br",
                                                            "Pragma": "no-cache",
                                                            "Cache-Control": "no-cache",
                                                            "Connection": "keep-alive, Upgrade",
                                                            "Sec-Fetch-Dest": "websocket",
                                                            "Sec-Fetch-Mode": "websocket",
                                                            "Sec-Fetch-Site": "cross-site"
                                                        }
                                                        
                                                        try:
                                                            async with websockets.connect(
                                                                ws_url,
                                                                additional_headers=ws_headers,
                                                                subprotocols=['tik-cdn'],
                                                                compression=None,
                                                                max_size=None,
                                                                close_timeout=10
                                                            ) as websocket:
                                                                async def recv_message():
                                                                    while True:
                                                                        try:
                                                                            message = await websocket.recv()
                                                                            logger.debug(f"WebSocket message received: {message}")
                                                                            
                                                                            update = json.loads(message)
                                                                            if update.get("action") == "success" and update.get("url"):
                                                                                download_links.append(update["url"])
                                                                                logger.debug(f"Got final download URL: {update['url']}")
                                                                                return True
                                                                            elif update.get("action") == "progress":
                                                                                logger.debug(f"Conversion progress: {update.get('value')}%")
                                                                        except websockets.exceptions.ConnectionClosed:
                                                                            logger.error("WebSocket connection closed")
                                                                            return False
                                                                
                                                                # Set a timeout for websocket operations
                                                                try:
                                                                    await asyncio.wait_for(recv_message(), timeout=30)
                                                                except asyncio.TimeoutError:
                                                                    logger.error("WebSocket operation timed out")
                                                        except Exception as ws_error:
                                                            logger.error(f"WebSocket error: {ws_error}")
                                                    else:
                                                        logger.error(f"Unexpected conversion result: {convert_result}")
                                                else:
                                                    logger.error(f"Conversion failed with status: {convert_response.status}")

                                            else:
                                                logger.error(f"Conversion failed with status: {convert_response.status}")
                                                
                                    except Exception as e:
                                        logger.error(f"Error during conversion: {e}")
                                        return None  # Don't fall back to old method after conversion attempt
                    
                    # Try downloading from available links
                    for download_url in download_links:
                        try:
                            logger.debug(f"Attempting download from URL: {download_url}")
                            logger.debug(f"Download headers: {json.dumps(headers, indent=2)}")
                            
                            async with session.get(download_url, headers=headers, timeout=30) as video_response:
                                logger.debug(f"Download response status: {video_response.status}")
                                logger.debug(f"Download response headers: {json.dumps(dict(video_response.headers), indent=2)}")
                                
                                if video_response.status == 200:
                                    logger.info("Successfully downloaded video")
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