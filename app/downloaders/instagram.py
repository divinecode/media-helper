import re
import logging
from typing import Optional, List, Tuple, Dict
from pathlib import Path
import instaloader
from downloaders.base import VideoDownloader
from media_types import DownloadResult, MediaType
from config import Config

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class InstagramDownloader(VideoDownloader):
    LINK_REGEX = re.compile(r"https?://(?:www\.)?instagram\.com/(?:p|reel)/([A-Za-z0-9_-]+)")
    
    def __init__(self, config: Config):
        super().__init__(config)
        logger.debug("Initializing Instagram downloader")
        self.loader = instaloader.Instaloader(
            dirname_pattern=str(self.config.temp_dir),
            download_pictures=True,
            download_videos=True,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            post_metadata_txt_pattern="",
            compress_json=False,
            filename_pattern="{shortcode}"  # Simplified filename pattern
        )
        
        # Try to load session file if provided
        if self.config.instagram_session_file:
            try:
                logger.info(f"Using Instagram session from {self.config.instagram_session_file}")
                if self.config.instagram_session_file.exists():
                    # Load existing session
                    self.loader.load_session_from_file(
                        username=None,  # Username will be determined from session file
                        filename=str(self.config.instagram_session_file)
                    )
                    logger.info("Successfully loaded Instagram session")
                else:
                    logger.warning(f"Session file not found: {self.config.instagram_session_file}")
            except Exception as e:
                logger.warning(f"Failed to load Instagram session: {e}")
    
    def can_handle(self, url: str) -> bool:
        return bool(self.LINK_REGEX.search(url))
    
    async def download(self, url: str) -> Optional[List[DownloadResult]]:
        try:
            match = self.LINK_REGEX.search(url)
            if not match:
                logger.error("Failed to extract Instagram shortcode from URL")
                return None
                
            shortcode = match.group(1)
            logger.info(f"Processing Instagram post with shortcode: {shortcode}")
            
            # Get post from shortcode
            post = instaloader.Post.from_shortcode(self.loader.context, shortcode)
            
            # Create a temporary directory for this download
            temp_dir = self.config.temp_dir / f"instagram_{shortcode}"
            temp_dir.mkdir(exist_ok=True)
            
            try:
                # Set download directory
                self.loader.dirname_pattern = str(temp_dir)
                
                # Download the post
                self.loader.download_post(post, target=shortcode)
                logger.info(f"Downloaded post to {temp_dir}")
                
                # Log all files in temp directory
                all_files = list(temp_dir.glob("*"))
                logger.info(f"Files in temp directory: {[f.name for f in all_files]}")
                
                # Process all media files
                results: List[DownloadResult] = []
                
                # Handle sidecar (multiple media items)
                if post.typename == 'GraphSidecar':
                    logger.info("Processing sidecar post with multiple media items")
                    sidecar_nodes = list(post.get_sidecar_nodes())
                    logger.info(f"Found {len(sidecar_nodes)} items in sidecar")
                    
                    # First, collect all media files
                    video_files = sorted(temp_dir.glob("*.mp4"))
                    image_files = sorted(temp_dir.glob("*.jpg"))
                    logger.info(f"Found {len(video_files)} video files and {len(image_files)} image files")
                    
                    # Match files to nodes
                    video_idx = 0
                    image_idx = 0
                    
                    for node in sidecar_nodes:
                        if node.is_video:
                            if video_idx < len(video_files):
                                video_file = video_files[video_idx]
                                logger.info(f"Processing video file: {video_file.name}")
                                with open(video_file, 'rb') as f:
                                    results.append(DownloadResult(
                                        data=f.read(),
                                        media_type=MediaType.VIDEO,
                                        caption=post.caption if post.caption else None
                                    ))
                                video_idx += 1
                        else:
                            if image_idx < len(image_files):
                                image_file = image_files[image_idx]
                                logger.info(f"Processing image file: {image_file.name}")
                                with open(image_file, 'rb') as f:
                                    results.append(DownloadResult(
                                        data=f.read(),
                                        media_type=MediaType.PHOTO,
                                        caption=post.caption if post.caption else None
                                    ))
                                image_idx += 1
                else:
                    # Handle single video or photo
                    if post.is_video:
                        video_files = list(temp_dir.glob("*.mp4"))
                        if video_files:
                            logger.info(f"Processing single video: {video_files[0].name}")
                            with open(video_files[0], 'rb') as f:
                                results.append(DownloadResult(
                                    data=f.read(),
                                    media_type=MediaType.VIDEO,
                                    caption=post.caption if post.caption else None
                                ))
                    else:
                        image_files = list(temp_dir.glob("*.jpg"))
                        if image_files:
                            logger.info(f"Processing single image: {image_files[0].name}")
                            with open(image_files[0], 'rb') as f:
                                results.append(DownloadResult(
                                    data=f.read(),
                                    media_type=MediaType.PHOTO,
                                    caption=post.caption if post.caption else None
                                ))
                
                if not results:
                    logger.error("No media files were processed")
                    return None
                    
                logger.info(f"Successfully processed {len(results)} media items")
                return results
                
            finally:
                # Clean up temporary files
                for file in temp_dir.glob("*"):
                    try:
                        file.unlink()
                    except Exception as e:
                        logger.warning(f"Failed to delete temporary file {file}: {e}")
                try:
                    temp_dir.rmdir()
                except Exception as e:
                    logger.warning(f"Failed to delete temporary directory: {e}")
                
        except instaloader.exceptions.InstaloaderException as e:
            logger.error(f"Instaloader error: {e}")
            return None
        except Exception as e:
            logger.error(f"Error downloading Instagram post: {e}")
            return None