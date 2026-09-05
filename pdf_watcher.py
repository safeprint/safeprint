#!/usr/bin/env python3
"""
PDF Watcher - Local HTTP server for automatic PDF deletion.
Listens on port 8765 and schedules deletion of TEMP_PDF_*.pdf files.

Usage:
    python pdf_watcher.py

The server will run on http://127.0.0.1:8765
"""

import json
import os
import sys
import time
import threading
import logging
import re
import platform
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timedelta

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Configuration
PORT = 8765
HOST = '127.0.0.1'
DELETE_DELAY_SECONDS = 5  # Delete after 5 seconds
MAX_WAIT_FOR_FILE = 30  # Wait up to 30 seconds for file to appear

# Get the user's Downloads folder based on OS
def get_downloads_folder():
    """Get the user's Downloads folder path."""
    home = Path.home()
    
    if platform.system() == 'Windows':
        downloads = home / 'Downloads'
        if not downloads.exists():
            downloads = home / 'Desktop'
        return downloads
    elif platform.system() == 'Darwin':  # macOS
        return home / 'Downloads'
    else:  # Linux and others
        downloads = home / 'Downloads'
        if not downloads.exists():
            downloads = home / 'Desktop'
        return downloads

# Common download directories to check (with fallbacks)
DOWNLOAD_DIRS = [
    get_downloads_folder(),
    Path.home() / 'Downloads',
    Path.home() / 'Downloads' / 'XeroxTemp',
    Path.home() / 'Desktop',
    Path.home() / 'Documents',
    Path.cwd(),
]

# Add browser-specific folders
def get_browser_download_folders():
    home = Path.home()
    folders = []
    
    if platform.system() == 'Windows':
        folders.append(home / 'Downloads')
    elif platform.system() == 'Darwin':
        folders.append(home / 'Downloads')
    else:
        folders.append(home / 'Downloads')
    
    return folders

DOWNLOAD_DIRS.extend(get_browser_download_folders())
DOWNLOAD_DIRS = list(dict.fromkeys(DOWNLOAD_DIRS))
DOWNLOAD_DIRS = [d for d in DOWNLOAD_DIRS if d is not None]

# Patterns for validation
TEMP_PDF_PATTERN = re.compile(r'^TEMP_PDF_\d+_[a-z0-9]+\.pdf$', re.IGNORECASE)

# Store active jobs
active_jobs = {}
job_lock = threading.Lock()
server_instance = None

class PDFWatcherHandler(BaseHTTPRequestHandler):
    """HTTP handler for the PDF watcher server."""
    
    def log_message(self, format, *args):
        logger.info(f"{self.address_string()} - {format % args}")
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
    
    def do_GET(self):
        parsed = urlparse(self.path)
        
        if parsed.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            
            with job_lock:
                job_count = len(active_jobs)
            
            response = {
                'status': 'running',
                'active_jobs': job_count,
                'uptime': str(datetime.now() - self.server.start_time).split('.')[0] if hasattr(self.server, 'start_time') else 'unknown',
                'delete_delay': DELETE_DELAY_SECONDS,
                'download_dirs': [str(d) for d in DOWNLOAD_DIRS if d.exists()]
            }
            self.wfile.write(json.dumps(response).encode())
            return
        
        if parsed.path == '/jobs':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            
            with job_lock:
                job_list = []
                for job_id, job_info in list(active_jobs.items()):
                    job_list.append({
                        'id': job_id,
                        'filename': job_info.get('filename', 'unknown'),
                        'status': job_info.get('status', 'unknown'),
                        'started': job_info.get('started', '').isoformat() if isinstance(job_info.get('started'), datetime) else str(job_info.get('started', ''))
                    })
            
            self.wfile.write(json.dumps({'jobs': job_list}).encode())
            return
        
        self.send_response(404)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(b'{"error": "Not found"}')
    
    def do_POST(self):
        parsed = urlparse(self.path)
        
        if parsed.path != '/schedule':
            self.send_response(404)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'Not found'}).encode())
            return
        
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        
        try:
            data = json.loads(body.decode())
            filename = data.get('filename')
            minutes = data.get('minutes', 5)  # Keep for compatibility but ignore
            
            if not filename:
                self._send_error(400, 'Missing filename')
                return
            
            if not self._validate_filename(filename):
                self._send_error(400, 'Invalid filename - must start with TEMP_PDF_ and end with .pdf')
                return
            
            # Schedule the deletion job with fast deletion
            job_id = f"job_{int(time.time())}_{id(filename)}"
            logger.info(f"Scheduling FAST deletion for {filename} (job: {job_id}) - will delete in {DELETE_DELAY_SECONDS} seconds")
            
            thread = threading.Thread(
                target=self._fast_cleanup_job,
                args=(filename, job_id),
                daemon=True
            )
            thread.start()
            
            with job_lock:
                active_jobs[job_id] = {
                    'filename': filename,
                    'started': datetime.now(),
                    'status': 'scheduled',
                    'thread': thread,
                    'delete_delay': DELETE_DELAY_SECONDS
                }
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            response = {
                'status': 'scheduled',
                'job_id': job_id,
                'filename': filename,
                'delete_delay': DELETE_DELAY_SECONDS,
                'message': f'Deletion scheduled for {filename} in {DELETE_DELAY_SECONDS} seconds'
            }
            self.wfile.write(json.dumps(response).encode())
            
        except json.JSONDecodeError:
            self._send_error(400, 'Invalid JSON')
        except Exception as e:
            logger.error(f"Error processing request: {e}")
            self._send_error(500, f'Internal error: {str(e)}')
    
    def _send_error(self, code, message):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps({'error': message}).encode())
    
    def _validate_filename(self, filename):
        if '..' in filename or '/' in filename or '\\' in filename:
            return False
        return bool(TEMP_PDF_PATTERN.match(filename))
    
    def _find_file(self, filename):
        """Search for the file in common download directories."""
        for directory in DOWNLOAD_DIRS:
            if directory is None or not directory.exists():
                continue
            file_path = directory / filename
            if file_path.exists() and file_path.is_file():
                logger.debug(f"Found file in: {file_path}")
                return file_path
            
            # Also check subdirectories
            try:
                for subdir in directory.iterdir():
                    if subdir.is_dir():
                        sub_path = subdir / filename
                        if sub_path.exists() and sub_path.is_file():
                            logger.debug(f"Found file in subdirectory: {sub_path}")
                            return sub_path
            except Exception:
                pass
        
        return None
    
    def _delete_with_retry(self, file_path, max_retries=30, retry_delay=0.5):
        """
        Attempt to delete a file with retries if it's locked.
        """
        if not file_path.exists():
            logger.info(f"File no longer exists: {file_path}")
            return True
            
        for attempt in range(max_retries):
            try:
                file_path.unlink()
                logger.info(f"✅ Deleted: {file_path}")
                return True
            except PermissionError:
                # File is locked - wait and retry
                if attempt % 5 == 0:
                    logger.debug(f"File locked, retry {attempt + 1}/{max_retries}: {file_path}")
                time.sleep(retry_delay)
            except Exception as e:
                logger.error(f"Error deleting {file_path}: {e}")
                return False
        
        logger.warning(f"Failed to delete after {max_retries} retries: {file_path}")
        return False
    
    def _fast_cleanup_job(self, filename, job_id):
        """
        Fast deletion job: find the file and delete it after a short delay.
        """
        try:
            logger.info(f"[Job {job_id}] Started FAST deletion for {filename}")
            
            with job_lock:
                if job_id in active_jobs:
                    active_jobs[job_id]['status'] = 'searching'
            
            # Step 1: Wait for the file to appear (up to MAX_WAIT_FOR_FILE seconds)
            wait_start = time.time()
            file_path = None
            found = False
            
            logger.info(f"[Job {job_id}] Looking for file: {filename}")
            
            while time.time() - wait_start < MAX_WAIT_FOR_FILE:
                file_path = self._find_file(filename)
                if file_path:
                    found = True
                    logger.info(f"[Job {job_id}] ✅ Found file: {file_path}")
                    break
                time.sleep(0.5)
            
            if not found:
                logger.warning(f"[Job {job_id}] ❌ File not found: {filename}")
                logger.info(f"[Job {job_id}] Searched in: {[str(d) for d in DOWNLOAD_DIRS if d and d.exists()]}")
                with job_lock:
                    active_jobs.pop(job_id, None)
                return
            
            # Update job status
            with job_lock:
                if job_id in active_jobs:
                    active_jobs[job_id]['status'] = 'waiting'
                    active_jobs[job_id]['file_path'] = str(file_path)
            
            # Step 2: Wait for the short delay
            logger.info(f"[Job {job_id}] Waiting {DELETE_DELAY_SECONDS} seconds before deletion...")
            time.sleep(DELETE_DELAY_SECONDS)
            
            # Check if file still exists
            if not file_path.exists():
                logger.info(f"[Job {job_id}] File was already deleted: {file_path}")
                with job_lock:
                    active_jobs.pop(job_id, None)
                return
            
            # Step 3: Delete the file with retries
            logger.info(f"[Job {job_id}] 🗑️ Deleting: {file_path}")
            success = self._delete_with_retry(file_path)
            
            if success:
                logger.info(f"[Job {job_id}] ✅ Successfully deleted {filename}")
            else:
                logger.warning(f"[Job {job_id}] ❌ Failed to delete {filename}")
            
            # Clean up job record
            with job_lock:
                active_jobs.pop(job_id, None)
            
        except Exception as e:
            logger.error(f"[Job {job_id}] Error: {e}")
            with job_lock:
                active_jobs.pop(job_id, None)


def main():
    """Start the HTTP server."""
    global server_instance
    
    server = HTTPServer((HOST, PORT), PDFWatcherHandler)
    server.start_time = datetime.now()
    server_instance = server
    
    logger.info("=" * 60)
    logger.info("📄 PDF Watcher Server Started")
    logger.info(f"🌐 Listening on http://{HOST}:{PORT}")
    logger.info(f"📡 Endpoint: POST http://{HOST}:{PORT}/schedule")
    logger.info(f"⏱️  Delete delay: {DELETE_DELAY_SECONDS} seconds")
    logger.info("\n📁 Monitoring download directories:")
    for d in DOWNLOAD_DIRS:
        if d and d.exists():
            logger.info(f"  ✅ {d}")
        elif d:
            logger.info(f"  ⚠️  {d} (does not exist)")
    logger.info("\nPress Ctrl+C to stop")
    logger.info("=" * 60)
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("\nShutting down...")
        server.shutdown()
        server.server_close()
        logger.info("Server stopped.")


if __name__ == '__main__':
    main()