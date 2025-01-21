import os
import asyncio
import logging
from pathlib import Path
import subprocess
import json
from dataclasses import dataclass
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class VideoProcessor:
    def __init__(self, config: 'Config', temp_manager: 'TempManager'):
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
            
            # Run ffprobe in thread pool to avoid blocking
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
        user_id: int
    ) -> Optional[bytes]:
        """Compress video to target size using ffmpeg."""
        # Create temporary directory for this user's session
        temp_dir = self.temp_manager.create_user_temp_dir(user_id)
        
        try:
            input_path = temp_dir / "input.mp4"
            output_path = temp_dir / "compressed.mp4"
            
            # Save input video
            with open(input_path, "wb") as f:
                f.write(video_data)

            duration = await self._get_video_duration(input_path)
            if not duration:
                return None

            # Acquire semaphore before running ffmpeg
            async with self.ffmpeg_semaphore:
                # First compression attempt
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
                        # Second compression attempt if needed
                        second_output_path = temp_dir / "compressed_2.mp4"
                        compressed_data = await self._compress_video_pass(
                            output_path,
                            second_output_path,
                            self.config.compression.second_pass_crf,
                            self.config.compression.second_pass_scale,
                            self.config.compression.second_pass_preset,
                            self.config.compression.second_pass_audio_bitrate
                        )

            return compressed_data

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
        """Execute a single compression pass using multiple threads."""
        try:
            # Get optimal thread count
            cpu_count = os.cpu_count() or 4
            thread_count = max(1, min(cpu_count - 1, 8))

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
                "-vf", f"scale={scale}:-2:flags=lanczos",
                "-c:a", "aac",
                "-b:a", f"{audio_bitrate}k",
                "-ac", "2",
                "-ar", "44100",
                "-max_muxing_queue_size", "9999",
                "-movflags", "+faststart",
                str(output_path)
            ]
            
            # Run ffmpeg in thread pool
            loop = asyncio.get_event_loop()
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