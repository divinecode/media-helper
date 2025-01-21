import os
from pathlib import Path
import shutil
import logging
from typing import Optional
import uuid

logger = logging.getLogger(__name__)

class TempManager:
    def __init__(self, base_temp_dir: Path):
        self.base_temp_dir = base_temp_dir
        
    def create_user_temp_dir(self, user_id: int) -> Path:
        """Create a unique temporary directory for a user session."""
        # Create a unique session ID for this download
        session_id = str(uuid.uuid4())
        user_temp_dir = self.base_temp_dir / str(user_id) / session_id
        user_temp_dir.mkdir(parents=True, exist_ok=True)
        return user_temp_dir
        
    def cleanup_user_temp_dir(self, temp_dir: Path) -> None:
        """Clean up a user's temporary directory."""
        try:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
        except Exception as e:
            logger.error(f"Error cleaning up temporary directory {temp_dir}: {e}")
            
    def cleanup_all_temp_dirs(self) -> None:
        """Clean up all temporary directories."""
        try:
            if self.base_temp_dir.exists():
                shutil.rmtree(self.base_temp_dir)
                self.base_temp_dir.mkdir(exist_ok=True)
        except Exception as e:
            logger.error(f"Error cleaning up all temporary directories: {e}")