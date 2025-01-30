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

                soup = BeautifulSoup(html_data, "html.parser")
                logger.debug("Parsing HTML for content downloads")

                # First try to get video download links
                video_links = self._parse_download_links(html_data)
                if video_links:
                    return await self._try_download_video(session, video_links[0])
                
                # If no video, try to get both audio and photos as they usually come together
                logger.debug("No video links found, trying to find audio and photos")
                    
                photos: List[DownloadResult] = await self._try_download_photos(session, soup)
                music: List[DownloadResult] = await self._try_download_music(session, soup)
                logger.debug(f"Found {len(photos)} photos and {len(music)} music tracks")

                # It is slideshow, generate video!
                if photos and len(music) == 1:
                    slideshow: DownloadResult = await self._create_slideshow_with_music(photos, music[0])
                    if slideshow:
                        return [slideshow]

                return photos + music

        except Exception as e:
            logger.error(f"Error in tikdownloader method: {e}", exc_info=True)
            return None

    async def _try_download_video(
        self,
        session: aiohttp.ClientSession,
        link_info: DownloadInfo,
    ) -> List[DownloadResult]:
        """Try to download video content."""
        try:
            logger.debug(f"Attempting video download from URL: {link_info.download_url}")
            async with session.get(
                link_info.download_url,
                headers=link_info.headers,
                timeout=30
            ) as response:
                if response.status == 200:
                    return [DownloadResult(
                        data=await response.read(),
                        media_type=MediaType.VIDEO
                    )]
                else:
                    logger.error(f"Failed to download video from {link_info.download_url} with status {response.status} and message: {response.text}")
                    return []
        except Exception as e:
            logger.debug(f"Video download failed: {e}")

    async def _create_slideshow_with_music(
        self,
        images: List[DownloadResult],
        audio: DownloadResult
    ) -> Optional[DownloadResult]:
        """Create a slideshow video from images with music using FFmpeg."""
        try:
            import tempfile
            import os
            from PIL import Image
            import io
            slideshow_config = self.config.slideshow
            
            # Parse configured resolution
            max_width, max_height = map(int, slideshow_config.resolution.split(':'))
            
            # Determine largest image dimensions
            max_img_width = 0
            max_img_height = 0
            
            # First pass to determine largest dimensions
            for image in images:
                with Image.open(io.BytesIO(image.data)) as img:
                    width, height = img.size
                    max_img_width = max(max_img_width, width)
                    max_img_height = max(max_img_height, height)
            
            # Scale dimensions to fit within config bounds while maintaining aspect ratio
            scale = min(max_width / max_img_width, max_height / max_img_height)
            target_width = int(max_img_width * scale)
            target_height = int(max_img_height * scale)
            if (target_height % 2) != 0:
                target_height -= 1
            if (target_width % 2) != 0:
                target_width -= 1
            
            # Create temporary directory for processing
            with tempfile.TemporaryDirectory() as temp_dir:
                # Save and scale images
                image_paths = []
                for i, image in enumerate(images):
                    # Open and scale image
                    with Image.open(io.BytesIO(image.data)) as img:
                        # Calculate scale for this specific image
                        img_width, img_height = img.size
                        img_scale = min(target_width / img_width, target_height / img_height)
                        new_width = int(img_width * img_scale)
                        new_height = int(img_height * img_scale)
                        
                        # Resize image
                        resized_img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                        
                        # Create new image with padding
                        padded_img = Image.new('RGB', (target_width, target_height), slideshow_config.background_color)
                        
                        # Calculate padding to center the image
                        left_padding = (target_width - new_width) // 2
                        top_padding = (target_height - new_height) // 2
                        
                        # Paste resized image onto padded background
                        padded_img.paste(resized_img, (left_padding, top_padding))
                        
                        # Save processed image
                        image_path = os.path.join(temp_dir, f'image_{i:03d}.jpg')
                        padded_img.save(image_path, 'JPEG', quality=95)
                        image_paths.append(image_path)
                
                # Save audio
                audio_path = os.path.join(temp_dir, 'audio.mp3')
                with open(audio_path, 'wb') as f:
                    f.write(audio.data)
                
                # Get audio duration using FFprobe
                duration_cmd = f'ffprobe -i {audio_path} -show_entries format=duration -v quiet -of csv="p=0"'
                process = await asyncio.create_subprocess_shell(
                    duration_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await process.communicate()
                audio_duration = float(stdout.decode().strip())
                
                # Calculate how many times to loop the images to match audio duration
                frame_duration = float(slideshow_config.fps.split('/')[1])  # Get seconds per frame from FPS
                total_image_duration = frame_duration * len(image_paths)
                loops_needed = int(audio_duration / total_image_duration) + 1
                
                # Create input file for FFmpeg with loops
                input_txt = os.path.join(temp_dir, 'input.txt')
                with open(input_txt, 'w') as f:
                    for _ in range(loops_needed):
                        for img_path in image_paths:
                            f.write(f"file '{img_path}'\n")
                            f.write(f"duration {frame_duration}\n")
                
                # Output video path
                output_path = os.path.join(temp_dir, 'output.mp4')
                
                # FFmpeg command for creating slideshow with music
                cmd = (
                    f'ffmpeg -y -f concat -safe 0 -i {input_txt} '
                    f'-i {audio_path} '
                    f'-vf "fps={slideshow_config.fps},format=yuv420p" '
                    f'-c:v {slideshow_config.video_codec} '
                    f'-preset {slideshow_config.video_preset} '
                    f'-crf {slideshow_config.video_crf} '
                    f'-c:a {slideshow_config.audio_codec} '
                    f'-b:a {slideshow_config.audio_bitrate} '
                    f'-shortest '  # End when audio finishes
                    f'{output_path}'
                )
                
                # Execute FFmpeg command
                process = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                stdout, stderr = await process.communicate()
                
                if process.returncode != 0:
                    logger.error(f"FFmpeg error: {stderr.decode()}")
                    return None
                
                # Read the output video
                with open(output_path, 'rb') as f:
                    return DownloadResult(f.read(), MediaType.VIDEO)
                    
        except Exception as e:
            logger.error(f"Error creating slideshow: {e}", exc_info=True)
        return None

    async def _try_download_music(
        self,
        session: aiohttp.ClientSession,
        soup: BeautifulSoup,
    ) -> List[DownloadResult]:
        """Try to download music content."""

        # Then try to get music from dl-action section
        music_link = soup.find("div", class_="dl-action").find("a", attrs={
            "class": "tik-button-dl button dl-success",
            "href": lambda h: h and "dl.snapcdn.app/get" in h
        })

        if not music_link or not music_link.get("href"):
            return []
        
        url = music_link["href"]

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

            results: List[DownloadResult] = []
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
            
            return results
        except Exception as e:
            logger.debug(f"Music download failed: {e}", exc_info=True)

    async def _try_download_photos(
        self,
        session: aiohttp.ClientSession,
        soup: BeautifulSoup,
    ) -> List[DownloadResult]:
        """Try to download photo content."""
        try:
            photo_links = soup.find_all("a", attrs={
                "class": lambda c: c and "btn-premium" in c,
                "href": lambda h: h and "dl.snapcdn.app/get" in h
            })
            
            results: List[DownloadResult] = []
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
            
            return results
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
            
# HD files are too big, so we don't want them now, but it might be useful in the future
#            if "hd" in link_text:
#                download_links.insert(0, download_info)
#            el
            if "mp4" in link_text and "convert" not in link.get("class", []):
                download_links.append(download_info)

        logger.debug(f"Found {len(download_links)} download links")
        return download_links