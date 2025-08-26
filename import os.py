"""
hybrid_cloud_os.py  —  Single-file reference implementation
===========================================================

This monolithic script fuses the **Flask backend**, **JWT auth**, **Google-Cloud
integration**, **FUSE read-only client**, and **self-registration logic** that
were previously split across several modules.  Run with:

    # Start the backend
    $ CLOUD_MODE=server python3 hybrid_cloud_os.py

    # Or, run the client side (FUSE + self-registration)
    $ CLOUD_MODE=client \
      CLOUDROM_BACKEND=https://your-backend:8080 \
      MOUNTPOINT=/mnt/cloudfs \
      python3 hybrid_cloud_os.py

Environment variables required (client or server-side) are validated at
runtime and sensible error messages are emitted.
"""
import os, sys, json, uuid, jwt, time, subprocess, logging, requests
from datetime import datetime, timedelta
from functools import wraps
from errno import ENOENT, EIO
from cachetools import cached, TTLCache

# ---------------------------------------------------------------------------
# │ 1. CONFIGURATION & LOGGING                                              │
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
MODE = os.environ.get("CLOUD_MODE", "server").lower()           # server | client
BACKEND = os.environ.get("CLOUDROM_BACKEND")                    # client-side
MOUNTPOINT = os.environ.get("MOUNTPOINT", "/mnt/cloudfs")       # client-side

# Google-Cloud parameters (server-side)
GCP_PROJECT          = os.environ.get("GCP_PROJECT_ID")
IMAGE_BUCKET         = os.environ.get("IMAGE_BUCKET_NAME")
FIRESTORE_COLLECTION = os.environ.get("FIRESTORE_COLLECTION")
SECRET_NAME          = os.environ.get("SECRET_NAME")
SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")

# ---------------------------------------------------------------------------
# │ 2. COMMON UTILITIES                                                     │
# ---------------------------------------------------------------------------
def normalize_path(path: str) -> str:
    if not path: return ""
    p = os.path.normpath(path).lstrip(os.sep)
    if p.startswith(".."): abort(400, "Invalid path")
    return p

def get_device_id() -> str:
    try:
        with open("/sys/class/dmi/id/product_uuid") as f:
            if (d := f.read().strip()): return d
    except Exception: pass
    try: return str(uuid.getnode())
    except Exception: return os.uname()[1]

# ---------------------------------------------------------------------------
# │ 3. SERVER  (Flask + GCP + JWT)                                          │
# ---------------------------------------------------------------------------
if MODE == "server":
    #
    # -- Sanity-check env vars ---------------------------------------------
    #
    if not all([GCP_PROJECT, IMAGE_BUCKET, FIRESTORE_COLLECTION,
                SECRET_NAME, SERVICE_ACCOUNT_JSON]):
        logging.critical("Missing one or more GCP_* / SECRET_NAME variables"); sys.exit(1)

    #
    # -- Initialise Google-Cloud clients -----------------------------------
    #
    from flask import Flask, request, jsonify, abort
    from google.cloud import storage, firestore, secretmanager
    from google.api_core.exceptions import NotFound, GoogleAPIError
    from google.oauth2 import service_account

    creds_info = json.loads(SERVICE_ACCOUNT_JSON)
    creds      = service_account.Credentials.from_service_account_info(creds_info)
    storage_client  = storage.Client(credentials=creds)
    firestore_client= firestore.Client(credentials=creds)
    secret_client   = secretmanager.SecretManagerServiceClient(credentials=creds)
    bucket          = storage_client.bucket(IMAGE_BUCKET)
    logging.info("GCP clients initialised")

    secret_cache = TTLCache(maxsize=1, ttl=300)
    @cached(secret_cache)
    def jwt_secret():
        path = f"projects/{GCP_PROJECT}/secrets/{SECRET_NAME}/versions/latest"
        return secret_client.access_secret_version(name=path).payload.data.decode()

    # --------------------------- Flask setup ------------------------------
    app = Flask(__name__)

    def token_required(f):
        @wraps(f)
        def deco(*a, **kw):
            ah = request.headers.get("Authorization","")
            if not ah.startswith("Bearer "): abort(401,"Missing Bearer token")
            try:
                payload = jwt.decode(ah.split()[1], jwt_secret(), algorithms=["HS256"])
                request.device_id = payload["device_id"]
            except jwt.ExpiredSignatureError: abort(401,"Token expired")
            except jwt.PyJWTError:            abort(401,"Invalid token")
            return f(*a, **kw)
        return deco

    # --------------------------- API routes -------------------------------
    @app.route("/device/register", methods=["POST"])
    def register():
        data = request.get_json(silent=True) or {}
        dev  = data.get("device_id");  assert dev, abort(400,"device_id required")
        doc  = firestore_client.collection(FIRESTORE_COLLECTION).document(dev)
        if not doc.get().exists:
            doc.set({"status":"registered","created":datetime.utcnow()})
            logging.info("New device registered: %s", dev)
        token = jwt.encode({"device_id":dev,
                            "exp": datetime.utcnow()+timedelta(days=365)},
                           jwt_secret(), algorithm="HS256")
        return jsonify(token=token)

    @app.route("/image/latest")
    @token_required
    def latest_image():
        try:
            url = bucket.blob("rootfs.img").generate_signed_url(
                version="v4", expiration=timedelta(hours=1), method="GET")
            return jsonify(url=url)
        except GoogleAPIError as e:
            logging.error("Signed-URL generation failed: %s", e); abort(500)

    # ---------- CloudFS proxy endpoints (list / attrs / file) -------------
    def gcs_list(prefix):
        iterator = bucket.list_blobs(prefix=prefix, delimiter="/")
        dirs, files = [], []
        for p in iterator.prefixes:
            name = p.rstrip("/").split("/")[-1];  dirs.append(name)
        for b in iterator:
            if b.name!=prefix and (name:=b.name.split("/")[-1]): files.append(name)
        return dirs, files

    @app.route("/cloudfs/list")
    @token_required
    def cloudfs_list():
        path   = normalize_path(request.args.get("path","/"))
        prefix = "" if path=="/" else f"{path.rstrip('/')}/"
        dirs, files = gcs_list(prefix)
        return jsonify(dirs=dirs, files=files)

    @app.route("/cloudfs/file")
    @token_required
    def cloudfs_file():
        path = normalize_path(request.args.get("path") or abort(400))
        blob = bucket.blob(path);  blob.reload()
        if not blob.exists(): abort(404)
        url = blob.generate_signed_url(version="v4",
                                       expiration=timedelta(minutes=5),
                                       method="GET")
        return jsonify(url=url)

    @app.route("/cloudfs/attrs")
    @token_required
    def cloudfs_attrs():
        p = normalize_path(request.args.get("path") or abort(400))
        if p in ("","/"):  # root dir
            return jsonify(st_mode=0o040755, st_nlink=2, st_size=4096)
        if p.endswith("/"):  # explicit dir
            if gcs_list(p.rstrip("/")+"/")[0] or gcs_list(p.rstrip("/")+ "/")[1]:
                return jsonify(st_mode=0o040755, st_nlink=2, st_size=4096)
            abort(404)
        blob = bucket.blob(p)
        if not blob.exists(): abort(404)
        blob.reload()
        return jsonify(
            st_mode=0o100644, st_nlink=1, st_size=blob.size,
            st_ctime=blob.time_created.timestamp(),
            st_mtime=blob.updated.timestamp(),
            st_atime=blob.updated.timestamp(),
        )

    @app.route("/report/health", methods=["POST"])
    @token_required
    def health():
        logging.info("Health report from %s: %s", request.device_id, request.json); return "OK"

    @app.route("/")
    def root(): return "Backend Alive"

    if __name__ == "__main__":
        app.run(host="0.0.0.0", port=8080, debug=False)

# ---------------------------------------------------------------------------
# │ 4. CLIENT  (FUSE mount + self-register)                                 │
# ---------------------------------------------------------------------------
else:
    #
    # -- Validate env -------------------------------------------------------
    #
    if not BACKEND:
        logging.critical("CLOUDROM_BACKEND env var not set"); sys.exit(1)
    try: import requests, stat, errno
    except ImportError as e:
        logging.critical("python-fuse and requests required: %s", e); sys.exit(1)
    from fuse import FUSE, Operations, FuseOSError

    # ---------------- Device auth & token ---------------------------------
    def authenticate() -> str:
        dev = get_device_id()
        logging.info("Registering device_id %s at %s", dev, BACKEND)
        try:
            r = requests.post(f"{BACKEND}/device/register", json={"device_id":dev}, timeout=10, verify=True)
            r.raise_for_status();  return r.json()["token"]
        except requests.RequestException as e:
            logging.critical("Device auth failed: %s", e); sys.exit(1)

    TOKEN = authenticate()
    HEAD  = {"Authorization": f"Bearer {TOKEN}"}
    meta_cache = TTLCache(maxsize=1024, ttl=30)
    data_cache = TTLCache(maxsize=2048, ttl=300)

    # ---------------- FUSE filesystem -------------------------------------
    class CloudFS(Operations):
        def _req(self, method, path, **kw):
            url=f"{BACKEND}{path}";  kw.setdefault("headers",HEAD)
            try:
                r=requests.request(method,url,timeout=10,verify=True,**kw)
                r.raise_for_status(); return r.json()
            except requests.RequestException as e:
                if isinstance(e, requests.HTTPError) and e.response.status_code==404: raise FuseOSError(ENOENT)
                logging.error("HTTP error: %s", e); raise FuseOSError(EIO)

        @cached(meta_cache)
        def getattr(self, path, fh=None):
            if path=="/": return dict(st_mode=0o040755, st_nlink=2, st_size=4096)
            return self._req("GET","/cloudfs/attrs", params={"path":path})

        @cached(meta_cache)
        def readdir(self, path, fh):
            j = self._req("GET","/cloudfs/list", params={"path":path})
            return [".",".."]+j["dirs"]+j["files"]

        def read(self, path, size, offset, fh):
            key=(path,offset,size)
            if key in data_cache: return data_cache[key]
            url=self._req("GET","/cloudfs/file", params={"path":path})["url"]
            h={"Range":f"bytes={offset}-{offset+size-1}"}
            r=requests.get(url,headers=h,timeout=30); r.raise_for_status()
            data_cache[key]=r.content; return r.content

        # R/O filesystem -- reject modifications
        def _ro(*a, **k): raise FuseOSError(errno.EROFS)
        create=write=unlink=rmdir=mkdir=rename=truncate=_ro

    # ---------------- Launch FUSE -----------------------------------------
    if not os.path.exists(MOUNTPOINT): os.makedirs(MOUNTPOINT, exist_ok=True)
    logging.info("Mounting CloudFS at %s", MOUNTPOINT)
    FUSE(CloudFS(), MOUNTPOINT, foreground=True, ro=True)
