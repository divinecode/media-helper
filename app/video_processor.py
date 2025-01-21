import os
import asyncio
import logging
from pathlib import Path
import subprocess
import json
from dataclasses import dataclass
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor
from config import Config
from temp_manager import TempManager

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class VideoProcessor:
    def __init__(self, config: Config, temp_manager: TempManager):
        self.config = config
        self.temp_manager = temp_manager
        # Create a thread pool for CPU-intensive operations
        self.thread_pool = ThreadPoolExecutor(
            max_workers=min(32, (os.cpu_count() or 1) * 4)
        )
        # Semaphore to limit concurrent ffmpeg processes
        self.ffmpeg_semaphore = asyncio.Semaphore(
            min(8, (os.cpu_count() or 1))
        )

    async def _get_video_duration(self, input_path: Path) -> Optional[float]:
        """Get video duration using ffprobe."""
        try:
            probe_cmd = [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                str(input_path)
            ]
            
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                self.thread_pool,
                lambda: subprocess.run(probe_cmd, capture_output=True, text=True)
            )
            
            if result.returncode != 0:
                logger.error(f"ffprobe stderr: {result.stderr}")
                return None

            probe_data = json.loads(result.stdout)
            return float(probe_data['format']['duration'])

        except Exception as e:
            logger.error(f"Error getting video duration: {e}", exc_info=True)
            return None


    async def compress_video(
        self,
        video_data: bytes,
        max_size_mb: float,
        user_id: int,
        force_compress: bool = False
    ) -> Optional[bytes]:
        """Compress video to target size using ffmpeg."""
        temp_dir = self.temp_manager.create_user_temp_dir(user_id)
        
        try:
            input_path = temp_dir / "input.mp4"
            output_path = temp_dir / "compressed.mp4"
            
            # Save input video
            with open(input_path, "wb") as f:
                f.write(video_data)

            initial_size_mb = len(video_data) / (1024 * 1024)
            logger.debug(f"Initial video size: {initial_size_mb:.2f}MB")

            # Check if we need default compression
            if (initial_size_mb > self.config.compression.default_compress_threshold_mb or force_compress):
                if initial_size_mb <= max_size_mb:
                    # Apply default fast compression for videos > 5MB
                    logger.debug("Applying default compression")
                    async with self.ffmpeg_semaphore:
                        compressed_data = await self._compress_video_pass(
                            input_path,
                            output_path,
                            self.config.compression.default_crf,
                            self.config.compression.default_scale,
                            self.config.compression.default_preset,
                            self.config.compression.default_audio_bitrate
                        )
                        
                        if compressed_data:
                            compressed_size = len(compressed_data) / (1024 * 1024)
                            logger.debug(f"Default compression result: {compressed_size:.2f}MB")
                            # Only use compressed version if it's actually smaller
                            if compressed_size < initial_size_mb and compressed_size <= max_size_mb:
                                return compressed_data
                            else:
                                logger.debug("Compression resulted in larger file, using original")
                
                # If default compression wasn't enough or video is too large,
                # proceed with more aggressive compression
                if initial_size_mb > max_size_mb:
                    logger.debug("Applying first pass compression")
                    async with self.ffmpeg_semaphore:
                        compressed_data = await self._compress_video_pass(
                            input_path,
                            output_path,
                            self.config.compression.first_pass_crf,
                            self.config.compression.first_pass_scale,
                            self.config.compression.first_pass_preset,
                            self.config.compression.first_pass_audio_bitrate
                        )

                        if compressed_data:
                            compressed_size = len(compressed_data) / (1024 * 1024)
                            if compressed_size > max_size_mb:
                                logger.debug("Applying second pass compression")
                                second_output_path = temp_dir / "compressed_2.mp4"
                                compressed_data = await self._compress_video_pass(
                                    output_path,
                                    second_output_path,
                                    self.config.compression.second_pass_crf,
                                    self.config.compression.second_pass_scale,
                                    self.config.compression.second_pass_preset,
                                    self.config.compression.second_pass_audio_bitrate
                                )

            return video_data  # Return original if no compression was needed

        finally:
            # Clean up temporary directory
            self.temp_manager.cleanup_user_temp_dir(temp_dir)

    async def _compress_video_pass(
        self,
        input_path: Path,
        output_path: Path,
        crf: int,
        scale: int,
        preset: str,
        audio_bitrate: int
    ) -> Optional[bytes]:
        """Execute a single compression pass using multiple threads.
        Preserves input video dimensions if they're smaller than target scale."""
        try:
            cpu_count = os.cpu_count() or 4
            thread_count = max(1, min(cpu_count - 1, 8))

            # Get input video dimensions
            probe_cmd = [
                "ffprobe", "-v", "quiet",
                "-select_streams", "v:0",
                "-print_format", "json",
                "-show_entries", "stream=width,height",
                str(input_path)
            ]
            
            probe_result = await asyncio.create_subprocess_exec(
                *probe_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            probe_stdout, probe_stderr = await probe_result.communicate()
            
            # Default scale filter
            scale_filter = f"scale={scale}:-2:flags=lanczos"
            
            try:
                probe_data = json.loads(probe_stdout)
                if 'streams' in probe_data and probe_data['streams']:
                    width = probe_data['streams'][0].get('width', 0)
                    height = probe_data['streams'][0].get('height', 0)
                    
                    # Only scale down, never up
                    if width and height and width <= scale:
                        should_scale = False
                        logger.debug(f"Input dimensions: {width}x{height}, keeping original resolution")
                    else:
                        should_scale = True
                        logger.debug(f"Input dimensions: {width}x{height}, will scale down")
            except Exception as e:
                logger.error(f"Error parsing video dimensions: {e}")
                should_scale = True

            # Base command
            compress_cmd = [
                "ffmpeg",
                "-y",
                "-thread_queue_size", str(thread_count * 2),
                "-i", str(input_path),
                "-c:v", "libx264",
                "-preset", preset,
                "-crf", str(crf),
                "-threads", str(thread_count),
                "-filter_threads", str(thread_count),
                "-filter_complex_threads", str(thread_count),
            ]

            # Add scaling if needed
            if should_scale:
                compress_cmd.extend(["-vf", f"scale={scale}:-2:flags=lanczos"])

            # Add audio settings
            compress_cmd.extend([
                "-c:a", "aac",
                "-b:a", f"{audio_bitrate}k",
                "-ac", "2",
                "-ar", "44100",
                "-max_muxing_queue_size", "9999",
                "-movflags", "+faststart",
                str(output_path)
            ])
            
            # Run ffmpeg in subprocess
            process = await asyncio.create_subprocess_exec(
                *compress_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                logger.error(f"ffmpeg stderr: {stderr.decode()}")
                return None

            if not output_path.exists():
                return None

            with open(output_path, "rb") as f:
                return f.read()

        except Exception as e:
            logger.error(f"Error in compression pass: {e}", exc_info=True)
            return None