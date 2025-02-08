import re
import time
import hashlib
import logging
import aiohttp
from typing import Optional, List
from pathlib import Path
import instaloader
from downloaders.base import VideoDownloader
from media_types import DownloadResult, MediaType
from config import Config

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class InstagramDownloader(VideoDownloader):
    LINK_REGEX = re.compile(r"https?://(?:www\.)?instagram\.com/(?:p|reel)/([A-Za-z0-9_-]+)")
    BASE_URL = "https://fastdl.app"
    API_URL = f"{BASE_URL}/api/convert"
    MSEC_URL = f"{BASE_URL}/msec"
    
    def __init__(self, config: Config):
        super().__init__(config)
        self.uid = hex(int(time.time() * 1000))[2:]  # Random user ID for cookies
        self.cookies = {}
        
    def can_handle(self, url: str) -> bool:
        return bool(self.LINK_REGEX.search(url))

    async def _init_session(self) -> bool:
        """Step 1: Visit website to get cookies."""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                logger.debug(f"Making initial request to {self.BASE_URL}/en")
                async with session.get(f"{self.BASE_URL}/en", headers=headers) as response:
                    logger.debug(f"Initial response status: {response.status}")
                    if response.status != 200:
                        logger.error(f"Initial request failed with status {response.status}")
                        return False
                        
                    # Extract cookies from response
                    cookies = response.cookies
                    logger.debug(f"Got cookies from response: {cookies}")
                    
                    self.cookies = {
                        'uid': self.uid,
                        'adsUnderSearchInput': '37'
                    }
                    return True
        except Exception as e:
            logger.error(f"Failed to initialize session: {str(e)}", exc_info=True)
            return False

    async def _get_server_msec(self) -> Optional[float]:
        """Step 2: Get server timestamp."""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'Referer': f'{self.BASE_URL}/en',
            'Origin': self.BASE_URL,
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
        }
        
        try:
            async with aiohttp.ClientSession(cookies=self.cookies) as session:
                logger.debug(f"Getting server timestamp with cookies: {self.cookies}")
                async with session.get(self.MSEC_URL, headers=headers) as response:
                    if response.status != 200:
                        logger.error(f"Timestamp request failed with status {response.status}")
                        return None
                    data = await response.json()
                    logger.debug(f"Got server timestamp: {data}")
                    return data.get('msec')
        except Exception as e:
            logger.error(f"Failed to get timestamp: {str(e)}", exc_info=True)
            return None

    def _calculate_signature(self, url: str, ts: int, _ts: int) -> str:
        """Calculate request signature."""
        # Convert values to strings without any formatting/scientific notation
        current_ts = f"{ts}"
        base_ts = f"{_ts}"

        # Match browser's exact formatting - no spaces or separators
        data = f"{url}{current_ts}{base_ts}0fastdlapp"
        logger.debug(f"Signature input data: {data}")
        hexdigest = hashlib.sha256(data.encode()).hexdigest()
        logger.debug(f"Calculated signature: {hexdigest}")
        return hexdigest

    def _calculate_timestamp_offset(self, ts_now: int) -> int:
        """Calculate base timestamp by matching server behavior"""
        # Ensure ts_now matches the server's 13-digit timestamp format
        ts_now_str = str(ts_now)
        if len(ts_now_str) > 13:
            ts_now = int(ts_now_str[:13])
            
        # Extract last 5 digits to match server pattern
        last_digits = int(str(ts_now)[-5:])
        base_offset = 301561695
        return ts_now - base_offset - last_digits

    async def _make_api_request(self, url: str) -> Optional[dict]:
        """Make request to FastDL API."""
        if not await self._init_session():
            logger.error("Failed to initialize session")
            return None

        server_msec = await self._get_server_msec()
        if not server_msec:
            logger.error("Failed to get server timestamp")
            return None
            
        current_time = int(server_msec * 1000)
        ts_now = int(time.time() * 1000)
        base_time = self._calculate_timestamp_offset(ts_now)
        
        # Clean URL - remove query params but preserve exact format
        clean_url = url.split('?')[0]
        
        signature = self._calculate_signature(clean_url, current_time, base_time)
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en,en-US;q=0.8,ru-RU;q=0.5,ru;q=0.3',
            'Accept-Encoding': 'gzip, deflate, br, zstd',
            'Content-Type': 'application/json',
            'Origin': self.BASE_URL,
            'DNT': '1',
            'Connection': 'keep-alive',
            'Referer': f'{self.BASE_URL}/en',
            'Cookie': f'uid={self.uid}; adsUnderSearchInput=37',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'Pragma': 'no-cache',
            'Cache-Control': 'no-cache',
            'TE': 'trailers'
        }
        
        payload = {
            "url": clean_url,  # Use cleaned URL in payload too
            "ts": current_time,
            "_ts": base_time,
            "_tsc": 0,
            "_s": signature
        }
        
        logger.debug(f"Making API request with payload: {payload}")
        
        try:
            async with aiohttp.ClientSession(cookies=self.cookies) as session:
                async with session.post(self.API_URL, json=payload, headers=headers) as response:
                    response_text = await response.text()
                    logger.debug(f"API response status: {response.status}")
                    logger.debug(f"API response headers: {dict(response.headers)}")
                    logger.debug(f"API response text: {response_text}")
                    
                    if response.status != 200:
                        logger.error(f"API request failed with status {response.status}: {response_text}")
                        return None
                        
                    return await response.json()
        except Exception as e:
            logger.error(f"API request failed: {str(e)}", exc_info=True)
            return None

    async def download(self, url: str) -> Optional[List[DownloadResult]]:
        try:
            match = self.LINK_REGEX.search(url)
            if not match:
                logger.error("Failed to extract Instagram shortcode from URL")
                return None
                
            logger.debug(f"Processing Instagram URL: {url}")
            api_response = await self._make_api_request(url)
            
            if not api_response:
                logger.error("Failed to get response from FastDL API")
                return None
                
            results: List[DownloadResult] = []
            
            # Handle multiple images response
            if isinstance(api_response, list):
                logger.debug("Processing multiple images post")
                for item in api_response:
                    if media_url := item.get('url', [{}])[0].get('url'):
                        data = await self._download_file(media_url)
                        if data:
                            results.append(DownloadResult(
                                data=data,
                                media_type=MediaType.PHOTO,
                                caption=item.get('meta', {}).get('title')
                            ))
                            
            # Handle single video/image response
            else:
                logger.debug("Processing single media post")
                if media_url := api_response.get('url', [{}])[0].get('url'):
                    data = await self._download_file(media_url)
                    if data:
                        media_type = (MediaType.VIDEO 
                                    if api_response.get('url', [{}])[0].get('type') == 'mp4'
                                    else MediaType.PHOTO)
                        results.append(DownloadResult(
                            data=data,
                            media_type=media_type,
                            caption=api_response.get('meta', {}).get('title')
                        ))
            
            if not results:
                logger.error("No media was downloaded")
                return None
                
            logger.info(f"Successfully processed {len(results)} media items")
            return results
            
        except Exception as e:
            logger.error(f"Error downloading Instagram post: {e}")
            return None
