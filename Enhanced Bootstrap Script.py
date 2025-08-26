	#!/usr/bin/env python3
import os
import sys
import requests
import subprocess
import signal
import time
import uuid # Import uuid for get_device_id fallback
import logging # Import the logging module

# Configure logging for the bootstrap script
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [cloudrom-init] %(levelname)s: %(message)s')

# Environment variables for backend and mountpoint
BACKEND = os.environ.get("CLOUDROM_BACKEND", "")
MOUNTPOINT = os.environ.get("MOUNTPOINT", "/mnt/cloud")
FUSE_BIN = os.environ.get("FUSE_BIN", "/usr/local/bin/cloudfs_fuse.py") # Path to the FUSE client script

def get_device_id():
    """
    Attempts to retrieve a unique device ID using multiple methods,
    prioritizing hardware-based identifiers.
    """
    try:
        # Try to get UUID from DMI (Desktop Management Interface) on Linux systems
        with open('/sys/class/dmi/id/product_uuid') as f:
            device_id = f.read().strip()
            if device_id:
                logging.info(f"Device ID retrieved from DMI: {device_id}")
                return device_id
    except Exception as e:
        logging.warning(f"Could not read product_uuid from DMI: {e}")

    try:
        # Fallback 1: Use MAC address as a unique identifier
        # uuid.getnode() returns an integer, convert to string for device ID
        mac = uuid.getnode()
        if mac:
            device_id = f"mac-{mac}"
            logging.info(f"Device ID retrieved from MAC address: {device_id}")
            return device_id
    except Exception as e:
        logging.warning(f"Could not get MAC address for device ID: {e}")

    # Fallback 2: Use hostname as a last resort (less unique but always available)
    device_id = os.uname()[1]
    logging.info(f"Device ID retrieved from hostname: {device_id}")
    return device_id

def authenticate(max_retries=3, backoff=2):
    """
    Authenticates the device with the backend and retrieves a JWT token
    with retry logic and exponential backoff.
    """
    device_id = get_device_id()
    logging.info(f"Registering device_id: {device_id}")

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(f"{BACKEND}/device/register", json={'device_id': device_id}, timeout=10, verify=True)
            resp.raise_for_status() # Raise an exception for HTTP errors (4xx or 5xx)
            json_resp = resp.json()
            
            # The backend (main.py) consistently returns 'token'
            if "token" in json_resp:
                logging.info("Device authenticated and token received.")
                return json_resp["token"]
            else:
                logging.error(f"Unexpected response format from backend during registration: {json_resp}")
                sys.exit(1) # Exit if token is not found in the response
        except requests.exceptions.RequestException as e:
            logging.error(f"Attempt {attempt} failed to register device: {e}")
            if attempt < max_retries:
                wait_time = backoff ** attempt
                logging.info(f"Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
            else:
                logging.critical("Max retries reached for device registration. Exiting.")
                sys.exit(1)
        except Exception as e:
            logging.critical(f"An unexpected error occurred during authentication: {e}. Exiting.")
            sys.exit(1)

def run_fuse(token):
    """Launches the FUSE client as a subprocess and handles its lifecycle."""
    env = os.environ.copy() # Copy current environment
    env['TOKEN'] = token # Add the JWT token
    env['CLOUDROM_BACKEND'] = BACKEND # Pass the backend URL
    env['MOUNTPOINT'] = MOUNTPOINT # Pass the mountpoint

    # Create the mountpoint directory if it doesn't exist
    if not os.path.exists(MOUNTPOINT):
        try:
            os.makedirs(MOUNTPOINT)
            logging.info(f"Created mountpoint directory: {MOUNTPOINT}")
        except OSError as e:
            logging.critical(f"Failed to create mountpoint directory {MOUNTPOINT}: {e}. Exiting.")
            sys.exit(1)

    logging.info(f"Launching FUSE client {FUSE_BIN} at mountpoint {MOUNTPOINT}...")
    try:
        # Use subprocess.Popen to allow for graceful termination handling
        proc = subprocess.Popen(["python3", FUSE_BIN], env=env)

        # Gracefully handle termination signals
        def signal_handler(signum, frame):
            logging.info("Received termination signal, stopping FUSE client...")
            proc.terminate() # Send SIGTERM to the FUSE client process
            proc.wait()      # Wait for the FUSE client to exit
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)  # Ctrl+C
        signal.signal(signal.SIGTERM, signal_handler) # Standard termination signal

        proc.wait() # Wait for the FUSE client process to complete
        if proc.returncode != 0:
            logging.critical(f"FUSE client exited with non-zero code {proc.returncode}. Exiting.")
            sys.exit(proc.returncode)
        else:
            logging.info("FUSE client exited gracefully.")
    except FileNotFoundError:
        logging.critical(f"FUSE client script not found at {FUSE_BIN}. Ensure it's correctly placed in the Docker image. Exiting.")
        sys.exit(1)
    except Exception as e:
        logging.critical(f"Failed to launch FUSE client: {e}. Exiting.")
        sys.exit(1)

def main():
    """Main function to bootstrap the CloudROM device."""
    if not BACKEND:
        logging.critical("ERROR: CLOUDROM_BACKEND environment variable not set. Exiting.")
        sys.exit(1)

    token = authenticate()
    run_fuse(token)

if __name__ == '__main__':
    main()

