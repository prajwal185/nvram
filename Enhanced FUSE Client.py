#!/usr/bin/env python3
import os
import sys
import requests
import signal
from errno import ENOENT, EIO
from fuse import FUSE, Operations, FuseOSError
from cachetools import TTLCache, cached
import logging # Import the logging module

# Configure logging for the FUSE client for better visibility
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [cloudfs_fuse] %(levelname)s: %(message)s')

# Caches for metadata (short TTL for freshness) and file data (longer TTL for performance)
metadata_cache = TTLCache(maxsize=1024, ttl=60)  # 1 min cache on metadata
data_cache = TTLCache(maxsize=8192, ttl=300)     # 5 min cache on file data

class CloudFS(Operations):
    """
    Implements FUSE operations to expose an S3-backed filesystem
    via a backend API. This client is designed to be read-only.
    """
    def __init__(self, endpoint, token):
        self.endpoint = endpoint.rstrip('/') # Ensure no trailing slash for consistent URL construction
        self.session = requests.Session()
        # Set Authorization header for all requests
        self.session.headers.update({'Authorization': f'Bearer {token}'})
        # Verify SSL certificates for secure communication
        self.session.verify = True
        logging.info(f"CloudFS initialized with backend endpoint: {self.endpoint}")

    def _api_request(self, method, path, **kwargs):
        """Helper method to make requests to the backend API."""
        url = f"{self.endpoint}{path}"
        try:
            resp = self.session.request(method, url, timeout=10, **kwargs)
            resp.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
            return resp.json()
        except requests.exceptions.HTTPError as e:
            logging.error(f"API HTTP error {e.response.status_code} for {url}: {e.response.text}")
            if e.response.status_code == 404:
                raise FuseOSError(ENOENT) # File or directory not found
            # For other HTTP errors, treat as I/O error
            raise FuseOSError(EIO)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            logging.error(f"Connection/timeout error for {url}: {e}")
            raise FuseOSError(EIO) # Treat network issues as I/O errors
        except Exception as e:
            logging.critical(f"Unexpected error during API request to {url}: {e}")
            raise FuseOSError(EIO)

    @cached(metadata_cache)
    def getattr(self, path, fh=None):
        """
        Get file/directory attributes.
        This method is called for every path lookup.
        """
        logging.debug(f"getattr called for path: {path}")
        if path == '/':
            # Root directory attributes
            return dict(st_mode=(0o040755), st_nlink=2, st_size=4096)
        
        try:
            # Fetch attributes from the backend API
            attrs = self._api_request('GET', '/cloudfs/attrs', params={'path': path})
            # Coerce types safely, providing default values if keys are missing
            return {
                'st_mode': int(attrs.get('st_mode', 0)),
                'st_nlink': int(attrs.get('st_nlink', 1)),
                'st_size': int(attrs.get('st_size', 0)),
                'st_ctime': float(attrs.get('st_ctime', 0)),
                'st_mtime': float(attrs.get('st_mtime', 0)),
                'st_atime': float(attrs.get('st_atime', 0)),
            }
        except FuseOSError:
            # Re-raise FuseOSError if already raised by _api_request
            raise
        except Exception as e:
            logging.error(f"Metadata format or unexpected error for {path}: {e}")
            raise FuseOSError(EIO)

    @cached(metadata_cache)
    def readdir(self, path, fh):
        """
        List contents of a directory.
        Returns a list of names of files and subdirectories.
        """
        logging.debug(f"readdir called for path: {path}")
        try:
            resp = self._api_request('GET', '/cloudfs/list', params={'path': path})
            # FUSE expects '.' and '..' entries for directories
            return ['.', '..'] + resp.get('dirs', []) + resp.get('files', [])
        except FuseOSError:
            raise # Re-raise FuseOSError if already raised by _api_request
        except Exception as e:
            logging.error(f"Error reading directory {path}: {e}")
            raise FuseOSError(EIO)

    def read(self, path, size, offset, fh):
        """
        Read data from a file.
        """
        logging.debug(f"read called for path: {path}, size: {size}, offset: {offset}")
        if size <= 0:
            return b'' # Return empty bytes if size is non-positive

        key = (path, offset, size)
        if key in data_cache:
            logging.debug(f"Cache hit for {path} at offset {offset}, size {size}")
            return data_cache[key]

        try:
            url_json = self._api_request('GET', '/cloudfs/file', params={'path': path})
            presigned_url = url_json.get('url')
            if not presigned_url:
                logging.error(f"No presigned URL received for {path}")
                raise FuseOSError(EIO)

            # Request specific byte range using the 'Range' header
            headers = {'Range': f'bytes={offset}-{offset + size - 1}'}
            s3_resp = requests.get(presigned_url, headers=headers, timeout=30)
            s3_resp.raise_for_status() # Raise HTTPError for bad responses
            data = s3_resp.content
            data_cache[key] = data # Cache the fetched data block
            logging.debug(f"Successfully read {len(data)} bytes for {path} at offset {offset}")
            return data
        except FuseOSError:
            raise # Re-raise FuseOSError if already raised by _api_request
        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to read from S3 presigned URL for {path}: {e}")
            raise FuseOSError(EIO)
        except Exception as e:
            logging.critical(f"Unexpected error during read operation for {path}: {e}")
            raise FuseOSError(EIO)

    # --- Write Operations (Explicitly Disabled) ---
    # These operations are explicitly disabled as this is a read-only filesystem.
    # Raising FuseOSError(EIO) (Input/output error) indicates that the operation is not supported.

    def create(self, path, mode):
        """Create a file (disabled)."""
        logging.info(f"Attempted create operation on read-only filesystem: {path}")
        raise FuseOSError(EIO)

    def write(self, path, data, offset, fh):
        """Write data to a file (disabled)."""
        logging.info(f"Attempted write operation on read-only filesystem: {path}")
        raise FuseOSError(EIO)

    def truncate(self, path, length, fh=None):
        """Truncate a file (disabled)."""
        logging.info(f"Attempted truncate operation on read-only filesystem: {path}")
        raise FuseOSError(EIO)

    def unlink(self, path):
        """Remove a file (disabled)."""
        logging.info(f"Attempted unlink operation on read-only filesystem: {path}")
        raise FuseOSError(EIO)

    def rmdir(self, path):
        """Remove a directory (disabled)."""
        logging.info(f"Attempted rmdir operation on read-only filesystem: {path}")
        raise FuseOSError(EIO)

    def mkdir(self, path, mode):
        """Create a directory (disabled)."""
        logging.info(f"Attempted mkdir operation on read-only filesystem: {path}")
        raise FuseOSError(EIO)

    def rename(self, old, new):
        """Rename a file or directory (disabled)."""
        logging.info(f"Attempted rename operation on read-only filesystem: {old} -> {new}")
        raise FuseOSError(EIO)

    def flush(self, path, fh):
        """
        Flush cached file data (no-op for this read-only client).
        """
        logging.debug(f"flush called for path: {path}")
        return 0

    def release(self, path, fh):
        """
        Release file handle (no-op for this read-only client).
        """
        logging.debug(f"release called for path: {path}")
        return 0

def signal_handler(signum, frame):
    """Handles termination signals for graceful unmounting."""
    logging.info("Received termination signal, unmounting...")
    sys.exit(0)

if __name__ == "__main__":
    # Retrieve environment variables for backend URL, token, and mountpoint
    backend_url = os.environ.get("CLOUDROM_BACKEND")
    token = os.environ.get("TOKEN")
    mountpoint = os.environ.get("MOUNTPOINT", "/mnt/cloud")

    # Validate required environment variables
    if not backend_url or not token:
        logging.critical("Missing required environment variables: CLOUDROM_BACKEND and TOKEN. Exiting.")
        sys.exit(1)

    # Create mountpoint directory if it does not exist
    if not os.path.exists(mountpoint):
        try:
            os.makedirs(mountpoint)
            logging.info(f"Created mountpoint directory: {mountpoint}")
        except OSError as e:
            logging.critical(f"Failed to create mountpoint directory {mountpoint}: {e}. Exiting.")
            sys.exit(1)

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logging.info(f"Mounting CloudFS at {mountpoint}...")
    try:
        # Initialize and run FUSE filesystem
        FUSE(CloudFS(backend_url, token), mountpoint, foreground=True, nothreads=False)
    except Exception as e:
        logging.critical(f"Failed to mount FUSE filesystem at {mountpoint}: {e}. Exiting.")
        sys.exit(1)
