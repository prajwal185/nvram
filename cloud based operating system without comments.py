import os
import sys
import subprocess
import logging
import uuid

# Configure logging early for setup messages
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()

# --- Automatic Environment Setup ---
def set_default_environment():
    """Set default environment variables if not already set"""
    env_defaults = {
        "GCP_PROJECT_ID": f"auto-cloud-os-project-{uuid.uuid4().hex[:8]}",
        "GCS_BUCKET_NAME": f"auto-cloud-os-bucket-{uuid.uuid4().hex[:8]}",
        "JWT_SECRET_ID": f"auto-jwt-secret-{uuid.uuid4().hex[:8]}",
        "DATASTORE_KIND": "Device",
        "JWT_SECRET_VERSION": "latest",
        "CLOUDROM_BACKEND": "http://localhost:5000",
        "JWT_SECRET_VALUE": "dev-secret-key-for-local-development",  # Added fallback
        "MOUNTPOINT": "/mnt/cloud"
    }
    
    for key, value in env_defaults.items():
        if key not in os.environ:
            os.environ[key] = value
            logger.info(f"Set default environment variable: {key}={value}")

# --- Silent Dependency Installation ---
def install_dependencies():
    """Install required Python packages if missing"""
    required_packages = [
        "google-api-core",
        "google-cloud-storage",
        "google-cloud-datastore",
        "google-cloud-secret-manager",
        "flask",
        "requests",
        "pyjwt",
        "cachetools",
        "fusepy"
    ]
    
    try:
        import pkg_resources
        installed = {pkg.key for pkg in pkg_resources.working_set}
        missing = [pkg for pkg in required_packages if pkg.lower() not in installed]
        
        if missing:
            logger.info(f"Installing {len(missing)} missing dependencies...")
            subprocess.check_call([
                sys.executable,
                "-m", "pip", "install",
                "--quiet", "--no-input", "--disable-pip-version-check"
            ] + missing, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info("Dependencies installed successfully")
    except Exception as e:
        logger.error(f"Dependency installation failed: {e}")
        sys.exit(1)

# --- Run Setup ---
set_default_environment()
install_dependencies()

# --- Import Dependencies After Installation ---
try:
    from google.api_core.exceptions import NotFound
    from google.cloud.storage.retry import DEFAULT_RETRY
    from google.cloud import storage
    from google.cloud import datastore
    from google.cloud import secretmanager
    import requests
    import jwt
    from functools import wraps
    from flask import Flask, request, jsonify, abort
    from cachetools import cached, TTLCache
    from errno import ENOENT, EIO
    from fuse import FUSE, Operations, FuseOSError
except ImportError as e:
    logger.critical(f"Critical import failed after installation: {e}")
    sys.exit(1)

# --- Backend Application (Flask) ---
# Initialize the Flask application
app = Flask(__name__)

# Get environment variables
GCP_PROJECT_ID = os.environ["GCP_PROJECT_ID"]
GCS_BUCKET_NAME = os.environ["GCS_BUCKET_NAME"]
DATASTORE_KIND = os.environ["DATASTORE_KIND"]
JWT_SECRET_ID = os.environ["JWT_SECRET_ID"]
JWT_SECRET_VERSION = os.environ["JWT_SECRET_VERSION"]

# Log environment configuration
logger.info(f"GCP Project ID: {GCP_PROJECT_ID}")
logger.info(f"GCS Bucket Name: {GCS_BUCKET_NAME}")
logger.info(f"Datastore Kind: {DATASTORE_KIND}")
logger.info(f"JWT Secret ID: {JWT_SECRET_ID}")

# Initialize Google Cloud clients
try:
    storage_client = storage.Client(project=GCP_PROJECT_ID)
    datastore_client = datastore.Client(project=GCP_PROJECT_ID)
    secret_manager_client = secretmanager.SecretManagerServiceClient()
    logger.info("Google Cloud clients initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize Google Cloud clients: {e}")
    # Continue execution but note that operations may fail

# Cache for the JWT secret, to reduce API calls
secret_cache = TTLCache(maxsize=1, ttl=300)

@cached(secret_cache)
def get_jwt_secret():
    """Retrieves the JWT secret from Google Cloud Secret Manager with local fallback."""
    try:
        secret_name = f"projects/{GCP_PROJECT_ID}/secrets/{JWT_SECRET_ID}/versions/{JWT_SECRET_VERSION}"
        response = secret_manager_client.access_secret_version(request={"name": secret_name})
        return response.payload.data.decode('UTF-8')
    except Exception as e:
        app.logger.warning(f"JWT secret retrieval failed; falling back to env JWT_SECRET_VALUE: {e}")
        fallback = os.environ.get("JWT_SECRET_VALUE")
        if not fallback:
            app.logger.error("No JWT secret available from Secret Manager or JWT_SECRET_VALUE env var")
            raise
        return fallback

def token_required(f):
    """A decorator to validate JWT tokens from the 'Authorization' header."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            app.logger.warning("Authorization header missing or malformed.")
            abort(401, "Authorization header required with Bearer token.")
        token = auth_header.split(' ')[-1]
        if not token:
            app.logger.warning("Authorization token is empty.")
            abort(401, "Authorization token missing.")
        try:
            secret = get_jwt_secret()
            payload = jwt.decode(token, secret, algorithms=['HS256'])
            request.device_id = payload['device_id']
        except jwt.ExpiredSignatureError:
            app.logger.warning("Token expired.")
            abort(401, "Token expired.")
        except jwt.PyJWTError as e:
            app.logger.error(f"JWT decode error: {e}")
            abort(401, "Invalid token.")
        except Exception as e:
            app.logger.critical(f"Unexpected error during token validation: {e}")
            abort(500, "Internal server error during authentication.")
        return f(*args, **kwargs)
    return decorated

def normalize_path(path):
    """Normalizes a path to prevent directory traversal attacks."""
    if not path:
        return ''
    normalized = os.path.normpath(path).lstrip(os.sep)
    if normalized.startswith('..') or normalized.startswith('./..'):
        app.logger.warning(f"Attempted path traversal detected: {path}")
        abort(400, "Invalid path parameter.")
    return normalized

@app.route('/device/register', methods=['POST'])
def register():
    """Registers a new device and issues a JWT token."""
    data = request.get_json()
    if not data or 'device_id' not in data:
        app.logger.warning("Missing 'device_id' in registration request.")
        abort(400, "Missing device_id parameter.")
    device_id = data['device_id']
    try:
        # Use Datastore to check for existing device and register it
        key = datastore_client.key(DATASTORE_KIND, device_id)
        entity = datastore_client.get(key)
        if not entity:
            entity = datastore.Entity(key=key)
            entity.update({'status': 'registered'})
            datastore_client.put(entity)
            app.logger.info(f"Device {device_id} registered successfully.")
        else:
            app.logger.info(f"Device {device_id} already registered. Issuing new token.")
    except Exception as e:
        app.logger.error(f"Datastore error during device registration for {device_id}: {e}")
        abort(500, "Device registration failed.")

    try:
        secret = get_jwt_secret()
        token = jwt.encode({'device_id': device_id}, secret, algorithm='HS256')
        return jsonify({'token': token})
    except Exception as e:
        app.logger.error(f"Token generation failed for device {device_id}: {e}")
        abort(500, "Token generation failed.")

@app.route('/image/latest', methods=['GET'])
@token_required
def latest_image():
    """Generates a signed URL for the latest image."""
    image_key = "rootfs.img"
    try:
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(image_key)
        url = blob.generate_signed_url(expiration=3600) # URL expires in 1 hour
        app.logger.info(f"Generated signed URL for {image_key} for device {request.device_id}.")
        return jsonify({'url': url})
    except NotFound:
        app.logger.error(f"Image not found: {image_key}")
        abort(404, "Image not found.")
    except Exception as e:
        app.logger.critical(f"Unexpected error retrieving latest image: {e}")
        abort(500, "Internal server error.")

@app.route('/cloudfs/list', methods=['GET'])
@token_required
def list_dir():
    """Lists files and directories in a given path in Cloud Storage."""
    path = normalize_path(request.args.get('path', ''))
    # Ensure path ends with a '/' for consistent listing
    if path and not path.endswith('/'):
        path += '/'
    app.logger.info(f"Listing directory {path} for device {request.device_id}.")
    try:
        # List blobs with a prefix and delimiter to simulate directories
        blobs = storage_client.list_blobs(GCS_BUCKET_NAME, prefix=path, delimiter='/')
        
        # Get sub-directories from the prefixes
        dirs = [d.rstrip('/').split('/')[-1] for d in blobs.prefixes]
        
        # Get files from the blobs
        files = [blob.name.split('/')[-1] for blob in blobs if blob.name != path]
        
        return jsonify({'dirs': dirs, 'files': files})
    except Exception as e:
        app.logger.error(f"Cloud Storage list directory failed for path {path}: {e}")
        abort(500, "Failed to list directory.")

@app.route('/cloudfs/file', methods=['GET'])
@token_required
def get_file():
    """Generates a signed URL for a specific file in Cloud Storage."""
    path = normalize_path(request.args.get('path'))
    if not path:
        app.logger.warning("Missing 'path' parameter for file retrieval.")
        abort(400, "Missing 'path' parameter.")
    app.logger.info(f"Retrieving file URL for {path} for device {request.device_id}.")
    try:
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(path)
        
        # Check if the blob exists first to handle 404
        if not blob.exists():
            app.logger.info(f"File not found: {path}")
            abort(404, "File not found.")
            
        url = blob.generate_signed_url(expiration=300) # URL expires in 5 minutes
        return jsonify({'url': url})
    except Exception as e:
        app.logger.error(f"Cloud Storage get file URL failed for {path}: {e}")
        abort(500, "Failed to retrieve file URL.")

@app.route('/cloudfs/attrs', methods=['GET'])
@token_required
def get_attrs():
    """Retrieves file attributes for a given path."""
    path = normalize_path(request.args.get('path'))
    if path is None:
        app.logger.warning("Missing 'path' parameter for attribute retrieval.")
        abort(400, "Missing 'path' parameter.")
    app.logger.info(f"Retrieving attributes for {path} for device {request.device_id}.")

    try:
        if path in ('', '/'):
            # Root directory attributes
            return jsonify(dict(st_mode=0o040755, st_nlink=2, st_size=4096))

        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        
        # Check if it's a directory by looking for objects with the path as a prefix
        prefix = path + '/' if not path.endswith('/') else path
        blobs = storage_client.list_blobs(GCS_BUCKET_NAME, prefix=prefix, max_results=1, retry=DEFAULT_RETRY)
        
        # Check if any blobs exist with that prefix
        if any(blobs):
            return jsonify(dict(st_mode=0o040755, st_nlink=2, st_size=4096))
        
        # If not a directory, assume it's a file and get its attributes
        blob = bucket.blob(path)
        blob.reload()
        
        attrs = {
            'st_mode': 0o100644,
            'st_nlink': 1,
            'st_size': blob.size,
            'st_ctime': blob.time_created.timestamp(),
            'st_mtime': blob.updated.timestamp(),
            'st_atime': blob.updated.timestamp(),
        }
        return jsonify(attrs)
    except NotFound:
        app.logger.info(f"File or directory not found: {path}")
        abort(404, "File or directory not found.")
    except Exception as e:
        app.logger.critical(f"Unexpected error getting attributes for {path}: {e}")
        abort(500, "Internal server error.")

@app.route('/report/health', methods=['POST'])
@token_required
def report_health():
    """Receives a health report from the client."""
    app.logger.info(f"Health report from device {request.device_id}: {request.json}")
    return "OK", 200

@app.route('/', methods=['GET'])
def health_check():
    """A simple health check endpoint."""
    return "OK", 200

# --- FUSE Client ---
# Caching for metadata and data
metadata_cache = TTLCache(maxsize=1024, ttl=60)
data_cache = TTLCache(maxsize=4096, ttl=300)

class CloudFS(Operations):
    """
    FUSE filesystem client for a read-only cloud-backed filesystem.
    This class now interacts with the new Google Cloud-based backend.
    """
    def __init__(self, endpoint, token):
        self.endpoint = endpoint.rstrip('/')
        self.session = requests.Session()
        self.session.headers.update({'Authorization': f'Bearer {token}'})
        self.session.verify = True
        logging.info(f"CloudFS initialized with backend endpoint: {self.endpoint}")

    def _api_request(self, method, path, **kwargs):
        """Helper to make API requests to the backend."""
        url = f"{self.endpoint}{path}"
        try:
            resp = self.session.request(method, url, timeout=10, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            logging.error(f"API HTTP error for {url}: {e.response.status_code} - {e.response.text}")
            if e.response.status_code == 404:
                raise FuseOSError(ENOENT)
            raise FuseOSError(EIO)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            logging.error(f"API connection/timeout error for {url}: {e}")
            raise FuseOSError(EIO)
        except Exception as e:
            logging.critical(f"Unexpected error during API request to {url}: {e}")
            raise FuseOSError(EIO)

    @cached(metadata_cache)
    def getattr(self, path, fh=None):
        logging.debug(f"getattr called for path: {path}")
        if path == '/':
            return dict(st_mode=(0o040755), st_nlink=2, st_size=4096)
        return self._api_request('GET', '/cloudfs/attrs', params={'path': path})

    @cached(metadata_cache)
    def readdir(self, path, fh):
        logging.debug(f"readdir called for path: {path}")
        resp = self._api_request('GET', '/cloudfs/list', params={'path': path})
        return ['.', '..'] + resp.get('dirs', []) + resp.get('files', [])

    def read(self, path, size, offset, fh):
        logging.debug(f"read called for path: {path}, size: {size}, offset: {offset}")
        key = (path, offset, size)
        if key in data_cache:
            logging.debug(f"Cache hit for {path} at offset {offset}, size {size}")
            return data_cache[key]
        url_json = self._api_request('GET', '/cloudfs/file', params={'path': path})
        presigned_url = url_json.get('url')
        if not presigned_url:
            logging.error(f"Presigned URL not received for {path}")
            raise FuseOSError(EIO)
        
        # Read data from the signed URL
        headers = {'Range': f'bytes={offset}-{offset + size - 1}'}
        try:
            s3_resp = requests.get(presigned_url, headers=headers, timeout=30)
            s3_resp.raise_for_status()
            data = s3_resp.content
            data_cache[key] = data
            logging.debug(f"Successfully read {len(data)} bytes for {path} at offset {offset}")
            return data
        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to read from signed URL for {path}: {e}")
            raise FuseOSError(EIO)
        except Exception as e:
            logging.critical(f"Unexpected error during read operation for {path}: {e}")
            raise FuseOSError(EIO)

    def create(self, path, mode):
        logging.info(f"Attempted create operation on read-only filesystem: {path}")
        raise FuseOSError(EIO)

    def write(self, path, data, offset, fh):
        logging.info(f"Attempted write operation on read-only filesystem: {path}")
        raise FuseOSError(EIO)

    def truncate(self, path, length, fh=None):
        logging.info(f"Attempted truncate operation on read-only filesystem: {path}")
        raise FuseOSError(EIO)

    def unlink(self, path):
        logging.info(f"Attempted unlink operation on read-only filesystem: {path}")
        raise FuseOSError(EIO)

    def rmdir(self, path):
        logging.info(f"Attempted rmdir operation on read-only filesystem: {path}")
        raise FuseOSError(EIO)

    def mkdir(self, path, mode):
        logging.info(f"Attempted mkdir operation on read-only filesystem: {path}")
        raise FuseOSError(EIO)

    def rename(self, old, new):
        logging.info(f"Attempted rename operation on read-only filesystem: {old} -> {new}")
        raise FuseOSError(EIO)

# --- Entrypoint Script ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
BACKEND = os.environ.get("CLOUDROM_BACKEND", "")
MOUNTPOINT = os.environ.get("MOUNTPOINT", "/mnt/cloud")

def get_device_id():
    """
    Retrieves a unique device ID. This function is unchanged.
    """
    try:
        with open('/sys/class/dmi/id/product_uuid') as f:
            device_id = f.read().strip()
            if device_id:
                logging.info(f"Device ID retrieved from DMI: {device_id}")
                return device_id
    except Exception as e:
        logging.warning(f"Could not read product_uuid from DMI: {e}")
    try:
        device_id = str(uuid.getnode())
        logging.info(f"Device ID retrieved from MAC address: {device_id}")
        return device_id
    except Exception as e:
        logging.warning(f"Could not get MAC address for device ID: {e}")
    device_id = os.uname()[1]
    logging.info(f"Device ID retrieved from hostname: {device_id}")
    return device_id

def authenticate():
    """
    Authenticates the device with the backend and retrieves a JWT token.
    This function now calls the GCP-based backend.
    """
    device_id = get_device_id()
    logging.info(f"Registering device_id: {device_id}")
    try:
        resp = requests.post(f"{BACKEND}/device/register", json={'device_id': device_id}, timeout=10, verify=True)
        resp.raise_for_status()
        token = resp.json()['token']
        logging.info("Device authenticated and token received.")
        return token
    except requests.exceptions.RequestException as e:
        logging.critical(f"Authentication failed: {e}")
        sys.exit(1)

def main():
    """Main function to set up and launch the FUSE client."""
    if not BACKEND:
        logging.error("Error: CLOUDROM_BACKEND environment variable not set.")
        sys.exit(1)
    token = authenticate()
    env = os.environ.copy()
    env['TOKEN'] = token
    env['CLOUDROM_BACKEND'] = BACKEND
    env['MOUNTPOINT'] = MOUNTPOINT
    
    if not os.path.exists(MOUNTPOINT):
        try:
            os.makedirs(MOUNTPOINT)
            logging.info(f"Created mountpoint directory: {MOUNTPOINT}")
        except OSError as e:
            logging.critical(f"Failed to create mountpoint directory {MOUNTPOINT}: {e}")
            sys.exit(1)

    logging.info(f"Launching FUSE client at {MOUNTPOINT}...")
    try:
        FUSE(CloudFS(BACKEND, token), MOUNTPOINT, foreground=True, nothreads=False)
    except Exception as e:
        logging.critical(f"Failed to mount FUSE filesystem at {MOUNTPOINT}: {e}")
        sys.exit(1)

if __name__ == '__main__':
    mode = os.environ.get('MODE', 'client').lower()
    
    if mode == 'server':
        port = int(os.environ.get('PORT', '5000'))
        app.logger.info(f"Starting backend server on 0.0.0.0:{port}")
        app.run(host='0.0.0.0', port=port, debug=False)
    elif mode == 'client':
        main()  # Run FUSE client
    else:
        print(f"Invalid MODE: {mode}. Use 'server' or 'client'")
        sys.exit(1)
