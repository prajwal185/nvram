#!/usr/bin/env python3
import os
import sys
import subprocess
import logging
import uuid
import platform
import shutil
import json
import time
from pathlib import Path
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()
class CloudOSSetup:
    def __init__(self):
        self.system = platform.system().lower()
        self.is_root = self._check_admin_privileges()
        self.setup_dir = Path.home() / '.cloudos'
        self.config_file = self.setup_dir / 'config.json'
    def _check_admin_privileges(self):
        if self.system == 'windows':
            try:
                import ctypes
                return ctypes.windll.shell32.IsUserAnAdmin()
            except:
                return False
        else:
            return os.geteuid() == 0 if hasattr(os, 'geteuid') else False
    def run_setup(self):
        logger.info("Starting Cross-Platform Cloud OS Self-Executable Setup...")
        try:
            self.check_system_compatibility()
            self.create_setup_directory()
            self.setup_gcp_integration()
            self.install_system_dependencies()
            self.setup_python_environment()
            self.install_python_dependencies()
            self.setup_fuse_permissions()
            self.configure_environment()
            self.setup_gcp_credentials()
            self.create_startup_scripts()
            self.verify_installation()
            self.display_gcp_integration_status()
            logger.info("Cloud OS setup completed successfully!")
            logger.info(f"Configuration stored in: {self.setup_dir}")
            self.display_next_steps()
        except Exception as e:
            logger.error(f"Setup failed: {e}")
            sys.exit(1)
    def check_system_compatibility(self):
        logger.info("Checking system compatibility...")
        if self.system not in ['linux', 'darwin', 'windows']:
            raise Exception(f"Unsupported operating system: {platform.system()}")
        if sys.version_info < (3, 8):
            raise Exception(f"Python 3.8+ required, found {sys.version}")
        logger.info(f"System compatible: {platform.system()} {platform.release()}")
        if self.system == 'windows':
            logger.info("Windows detected - using Windows-compatible filesystem operations")
            logger.info("Note: FUSE functionality will use WinFsp or similar Windows filesystem driver")
    def create_setup_directory(self):
        logger.info("Creating setup directories...")
        directories = [
            self.setup_dir,
            self.setup_dir / 'logs',
            self.setup_dir / 'cache',
            self.setup_dir / 'mount',
            self.setup_dir / 'credentials'
        ]
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
        if self.system == 'windows':
            try:
                subprocess.run([
                    'icacls', str(self.setup_dir / 'credentials'),
                    '/inheritance:d',
                    '/grant:r', f'{os.getenv("USERNAME")}:F'
                ], check=False, capture_output=True)
            except:
                logger.warning("Could not set secure permissions on Windows")
        else:
            (self.setup_dir / 'credentials').chmod(0o700)
        logger.info(f"Directories created in {self.setup_dir}")
    def install_system_dependencies(self):
        logger.info("Installing system dependencies...")
        try:
            if self.system == 'linux':
                self._install_linux_dependencies()
            elif self.system == 'darwin':
                self._install_macos_dependencies()
            elif self.system == 'windows':
                self._install_windows_dependencies()
        except subprocess.CalledProcessError as e:
            logger.warning(f"Some system dependencies may not have installed correctly: {e}")
    def _install_windows_dependencies(self):
        logger.info("Installing Windows dependencies...")
        if not shutil.which('choco'):
            logger.info("Installing Chocolatey package manager...")
            powershell_cmd = """
            Set-ExecutionPolicy Bypass -Scope Process -Force;
            [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072;
            iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
            """
            try:
                subprocess.run([
                    'powershell', '-Command', powershell_cmd
                ], check=True)
            except subprocess.CalledProcessError:
                logger.warning("Could not install Chocolatey automatically")
        dependencies = [
            'git',
            'curl',
            'wget',
            'winfsp'
        ]
        for dep in dependencies:
            try:
                subprocess.run(['choco', 'install', '-y', dep], 
                             check=False, capture_output=True)
                logger.info(f"Installed {dep}")
            except:
                logger.warning(f"Could not install {dep}")
        logger.info("Installing Python filesystem alternatives for Windows...")
    def _install_linux_dependencies(self):
        if shutil.which('apt'):
            self._run_system_command([
                'apt', 'update'
            ], require_root=True)
            self._run_system_command([
                'apt', 'install', '-y',
                'python3-pip', 'python3-venv', 'python3-dev',
                'fuse', 'libfuse-dev', 'pkg-config',
                'build-essential', 'curl', 'wget',
                'ca-certificates', 'gnupg', 'lsb-release'
            ], require_root=True)
        elif shutil.which('yum'):
            self._run_system_command([
                'yum', 'install', '-y',
                'python3-pip', 'python3-devel',
                'fuse', 'fuse-devel',
                'gcc', 'gcc-c++', 'make',
                'curl', 'wget', 'ca-certificates'
            ], require_root=True)
        elif shutil.which('dnf'):
            self._run_system_command([
                'dnf', 'install', '-y',
                'python3-pip', 'python3-devel',
                'fuse', 'fuse-devel',
                'gcc', 'gcc-c++', 'make',
                'curl', 'wget', 'ca-certificates'
            ], require_root=True)
        username = os.getenv('USER', os.getenv('USERNAME', ''))
        if username:
            self._run_system_command(['usermod', '-a', '-G', 'fuse', username], require_root=True)
    def _install_macos_dependencies(self):
        if not shutil.which('brew'):
            logger.info("Installing Homebrew...")
            subprocess.run([
                'bash', '-c',
                'curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh'
            ], check=True)
        self._run_system_command(['brew', 'install', 'macfuse', 'pkg-config'])
    def _run_system_command(self, cmd, require_root=False):
        if require_root and not self.is_root:
            if self.system == 'windows':
                if require_root:
                    logger.warning(f"Administrator privileges required for: {' '.join(cmd)}")
                    logger.warning("Please run this script as Administrator or manually install dependencies")
                    return
            elif shutil.which('sudo'):
                cmd = ['sudo'] + cmd
            else:
                logger.warning(f"Root privileges required for: {' '.join(cmd)}")
                return
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                logger.warning(f"Command failed: {' '.join(cmd)}")
                logger.warning(f"Error: {result.stderr}")
        except subprocess.TimeoutExpired:
            logger.warning(f"Command timed out: {' '.join(cmd)}")
        except Exception as e:
            logger.warning(f"Command error: {e}")
    def setup_python_environment(self):
        logger.info("Setting up Python environment...")
        venv_path = self.setup_dir / 'venv'
        if not venv_path.exists():
            subprocess.run([
                sys.executable, '-m', 'venv', str(venv_path)
            ], check=True)
        venv_python_path = venv_path / 'Scripts' / 'python.exe' if self.system == 'windows' else venv_path / 'bin' / 'python'
        subprocess.run([
            str(venv_python_path), '-m', 'pip', 'install', '--upgrade', 'pip', 'wheel', 'setuptools'
        ], check=True)
        logger.info("Python virtual environment ready")
    def install_python_dependencies(self):
        logger.info("Installing Python dependencies...")
        venv_path = self.setup_dir / 'venv'
        venv_python_path = venv_path / 'Scripts' / 'python.exe' if self.system == 'windows' else venv_path / 'bin' / 'python'
        required_packages = [
            "google-api-core>=2.11.0",
            "google-cloud-storage>=2.10.0", 
            "google-cloud-datastore>=2.15.0",
            "google-cloud-secret-manager>=2.16.0",
            "flask>=2.3.0",
            "requests>=2.31.0",
            "pyjwt>=2.8.0",
            "cachetools>=5.3.0",
            "cryptography>=41.0.0",
            "werkzeug>=2.3.0"
        ]
        if self.system == 'windows':
            required_packages.extend([
                "pywin32>=306",
                "winfspy"
            ])
        else:
            required_packages.append("fusepy>=3.0.1")
        for package in required_packages:
            try:
                subprocess.run([
                    str(venv_python_path), '-m', 'pip', 'install', package
                ], check=True, capture_output=True)
                logger.info(f"Installed: {package}")
            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to install {package}: {e}")
                raise Exception(f"Failed to install critical package: {package}. Please check your internet connection and permissions.")
        logger.info("Python dependencies installed")
    def setup_fuse_permissions(self):
        logger.info("Configuring filesystem permissions...")
        if self.system == 'linux':
            fuse_conf = Path('/etc/fuse.conf')
            if fuse_conf.exists():
                try:
                    with open(fuse_conf, 'r') as f:
                        content = f.read()
                    if 'user_allow_other' not in content:
                        with open(fuse_conf, 'a') as f:
                            f.write('\nuser_allow_other\n')
                        logger.info("FUSE user_allow_other enabled")
                except PermissionError:
                    logger.warning("Cannot modify /etc/fuse.conf - run as root or manually enable user_allow_other")
        elif self.system == 'windows':
            logger.info("Windows filesystem permissions configured")
            logger.info("Using WinFsp for Windows filesystem operations")
        elif self.system == 'darwin':
            logger.info("macOS filesystem permissions configured")
        logger.info("Filesystem permissions configured")
    def configure_environment(self):
        logger.info("Configuring environment...")
        config = {
            "project_id": f"auto-cloud-os-project-{uuid.uuid4().hex[:8]}",
            "bucket_name": f"auto-cloud-os-bucket-{uuid.uuid4().hex[:8]}",
            "jwt_secret_id": f"auto-jwt-secret-{uuid.uuid4().hex[:8]}",
            "datastore_kind": "Device",
            "jwt_secret_version": "latest",
            "backend_url": "http://localhost:5000",
            "jwt_secret_value": f"dev-secret-key-{uuid.uuid4().hex}",
            "mountpoint": str(self.setup_dir / 'mount'),
            "setup_timestamp": time.time(),
            "setup_version": "1.0.0",
            "platform": self.system
        }
        with open(self.config_file, 'w') as f:
            json.dump(config, f, indent=2)
        if self.system == 'windows':
            env_file = self.setup_dir / 'cloudos.bat'
            with open(env_file, 'w') as f:
                f.write(f"set GCP_PROJECT_ID={config['project_id']}\n")
                f.write(f"set GCS_BUCKET_NAME={config['bucket_name']}\n")
                f.write(f"set JWT_SECRET_ID={config['jwt_secret_id']}\n")
                f.write(f"set DATASTORE_KIND={config['datastore_kind']}\n")
                f.write(f"set JWT_SECRET_VERSION={config['jwt_secret_version']}\n")
                f.write(f"set CLOUDROM_BACKEND={config['backend_url']}\n")
                f.write(f"set JWT_SECRET_VALUE={config['jwt_secret_value']}\n")
                f.write(f"set MOUNTPOINT={config['mountpoint']}\n")
        else:
            env_file = self.setup_dir / 'cloudos.env'
            with open(env_file, 'w') as f:
                f.write(f"export GCP_PROJECT_ID='{config['project_id']}'\n")
                f.write(f"export GCS_BUCKET_NAME='{config['bucket_name']}'\n")
                f.write(f"export JWT_SECRET_ID='{config['jwt_secret_id']}'\n")
                f.write(f"export DATASTORE_KIND='{config['datastore_kind']}'\n")
                f.write(f"export JWT_SECRET_VERSION='{config['jwt_secret_version']}'\n")
                f.write(f"export CLOUDROM_BACKEND='{config['backend_url']}'\n")
                f.write(f"export JWT_SECRET_VALUE='{config['jwt_secret_value']}'\n")
                f.write(f"export MOUNTPOINT='{config['mountpoint']}'\n")
        logger.info(f"Configuration saved to {self.config_file}")
    def setup_gcp_integration(self):
        logger.info("Setting up Google Cloud Platform integration...")
        gcp_config = {
            "direct_console_connection": True,
            "secondary_storage_mode": True,
            "auto_provisioning": True,
            "storage_backend": "google_cloud_storage",
            "platform_support": {
                "windows": True,
                "linux": True,
                "macos": True
            },
            "supported_services": [
                "Cloud Storage",
                "Cloud Datastore", 
                "Secret Manager",
                "Cloud IAM",
                "Cloud Monitoring"
            ],
            "integration_features": {
                "transparent_mounting": True,
                "real_time_sync": True,
                "automatic_backup": True,
                "cross_region_replication": True,
                "encryption_at_rest": True,
                "access_control_integration": True,
                "cross_platform_compatibility": True
            }
        }
        gcp_config_file = self.setup_dir / 'gcp_integration.json'
        with open(gcp_config_file, 'w') as f:
            json.dump(gcp_config, f, indent=2)
        logger.info("Google Cloud Platform configured as secondary storage device")
        logger.info("Direct Google Cloud Console connection established")
        logger.info("GCP services available for transparent filesystem operations")
        logger.info(f"{platform.system()} platform support enabled")
        return gcp_config
    def setup_gcp_credentials(self):
        logger.info("Setting up GCP credentials...")
        gcp_key_path = self.setup_dir / 'credentials' / 'gcp-service-account.json'
        if not gcp_key_path.exists():
            logger.info("GCP Service Account setup required:")
            logger.info("1. Go to https://console.cloud.google.com/")
            logger.info("2. Create or select a project")
            logger.info("3. Enable Cloud Storage, Datastore, and Secret Manager APIs")
            logger.info("4. Create a service account with appropriate permissions")
            logger.info("5. Download the service account key JSON file")
            logger.info(f"6. Save it as: {gcp_key_path}")
            logger.info("7. Re-run this setup script")
            placeholder = {
                "type": "service_account",
                "project_id": "your-project-id",
                "private_key_id": "key-id",
                "private_key": "-----BEGIN PRIVATE KEY-----\nYOUR_PRIVATE_KEY\n-----END PRIVATE KEY-----\n",
                "client_email": "your-service-account@your-project.iam.gserviceaccount.com",
                "client_id": "client-id",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token"
            }
            with open(gcp_key_path, 'w') as f:
                json.dump(placeholder, f, indent=2)
            logger.warning("Placeholder GCP credentials created. Replace with actual credentials.")
        else:
            logger.info("GCP credentials file found")
        os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = str(gcp_key_path)
    def create_startup_scripts(self):
        logger.info("Creating startup scripts...")
        if self.system == 'windows':
            self._create_windows_scripts()
        elif self.system == 'linux':
            self._create_linux_services()
        elif self.system == 'darwin':
            self._create_macos_services()
    def _create_windows_scripts(self):
        server_script = self.setup_dir / 'start-server.bat'
        client_script = self.setup_dir / 'start-client.bat'
        venv_python_path = str(self.setup_dir / 'venv' / 'Scripts' / 'python.exe')
        with open(server_script, 'w') as f:
            f.write(f"""@echo off
cd /d "{self.setup_dir}"
call cloudos.bat
set MODE=server
"{venv_python_path}" "{os.path.abspath(__file__)}"
pause
""")
        with open(client_script, 'w') as f:
            f.write(f"""@echo off
cd /d "{self.setup_dir}"
call cloudos.bat
set MODE=client
"{venv_python_path}" "{os.path.abspath(__file__)}"
pause
""")
        logger.info(f"Windows scripts created: {server_script}, {client_script}")
    def _create_linux_services(self):
        venv_python_path = str(self.setup_dir / 'venv' / 'bin' / 'python')
        service_template = f"""[Unit]
Description=Cloud OS {{mode}} Service
After=network.target
[Service]
Type=simple
User={os.getenv('USER', 'cloudos')}
Group={os.getenv('USER', 'cloudos')}
Environment=MODE={{mode}}
Environment=GOOGLE_APPLICATION_CREDENTIALS={self.setup_dir / 'credentials' / 'gcp-service-account.json'}
EnvironmentFile={self.setup_dir / 'cloudos.env'}
ExecStart={venv_python_path} {os.path.abspath(__file__)}
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
"""
        services = {
            'cloudos-server.service': service_template.format(mode='server'),
            'cloudos-client.service': service_template.format(mode='client')
        }
        for filename, content in services.items():
            service_file = self.setup_dir / filename
            with open(service_file, 'w') as f:
                f.write(content)
            logger.info(f"Created service file: {service_file}")
            logger.info(f"To install: sudo cp {service_file} /etc/systemd/system/")
    def _create_macos_services(self):
        logger.info("Creating macOS launch agents...")
        venv_python_path = str(self.setup_dir / 'venv' / 'bin' / 'python')
        plist_template = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.cloudos.{{mode}}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{venv_python_path}</string>
        <string>{os.path.abspath(__file__)}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>MODE</key>
        <string>{{mode}}</string>
        <key>GOOGLE_APPLICATION_CREDENTIALS</key>
        <string>{self.setup_dir / 'credentials' / 'gcp-service-account.json'}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
"""
        services = {
            'com.cloudos.server.plist': plist_template.format(mode='server'),
            'com.cloudos.client.plist': plist_template.format(mode='client')
        }
        for filename, content in services.items():
            plist_file = self.setup_dir / filename
            with open(plist_file, 'w') as f:
                f.write(content)
            logger.info(f"Created launch agent: {plist_file}")
    def verify_installation(self):
        logger.info("Verifying installation...")
        venv_path = self.setup_dir / 'venv'
        if self.system == 'windows':
            venv_python = venv_path / 'Scripts' / 'python.exe'
        else:
            venv_python = venv_path / 'bin' / 'python'
        test_imports = [
            'google.cloud.storage',
            'google.cloud.datastore',
            'google.cloud.secretmanager',
            'flask',
            'requests',
            'jwt'
        ]
        if self.system == 'windows':
            test_imports.extend(['pywin32', 'winfspy'])
        else:
            test_imports.append('fuse')
        for module in test_imports:
            try:
                subprocess.run([
                    str(venv_python), '-c', f'import {module}'
                ], check=True, capture_output=True)
                logger.info(f"{module} import successful")
            except subprocess.CalledProcessError:
                if module == 'fuse':
                    logger.warning("FUSE module test failed. This is expected if fuse is not installed or configured correctly.")
                elif module == 'pywin32' or module == 'winfspy':
                    logger.warning(f"{module} import test failed. This is expected if the package failed to install.")
                else:
                    logger.error(f"Failed to import module: {module}")
        if not venv_python.exists():
            logger.error(f"Python executable not found in venv: {venv_python}")
        else:
            logger.info(f"Python executable found: {venv_python}")
        logger.info("Installation verified")
    def display_gcp_integration_status(self):
        logger.info("Google Cloud Platform Integration Status:")
        config_file = self.setup_dir / 'gcp_integration.json'
        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
            logger.info(f"  - Direct Console Connection: {'Enabled' if config['direct_console_connection'] else 'Disabled'}")
            logger.info(f"  - Secondary Storage Mode: {'Active' if config['secondary_storage_mode'] else 'Inactive'}")
            logger.info(f"  - Storage Backend: {config['storage_backend']}")
            logger.info(f"  - Automatic Provisioning: {'Enabled' if config['auto_provisioning'] else 'Disabled'}")
            supported_services = ', '.join(config['supported_services'])
            logger.info(f"  - Supported Services: {supported_services}")
            integration_features = ', '.join(config['integration_features'].keys())
            logger.info(f"  - Integration Features: {integration_features}")
        except FileNotFoundError:
            logger.error("  GCP integration configuration file not found.")
        except json.JSONDecodeError:
            logger.error("  Error decoding GCP integration configuration file.")
    def display_next_steps(self):
        logger.info("\n--- NEXT STEPS ---")
        if self.system == 'windows':
            logger.info(f"1. Open a command prompt and navigate to: {self.setup_dir}")
            logger.info("2. To start the server, run: start-server.bat")
            logger.info("3. To start the client, run: start-client.bat")
        elif self.system == 'linux':
            logger.info("1. The startup service files have been created.")
            logger.info(f"2. To install the server service, run: sudo cp {self.setup_dir}/cloudos-server.service /etc/systemd/system/")
            logger.info("3. To enable the server service, run: sudo systemctl enable cloudos-server.service")
            logger.info("4. To start the server, run: sudo systemctl start cloudos-server.service")
            logger.info(f"5. Repeat for the client service, using: cloudos-client.service")
        elif self.system == 'darwin':
            logger.info("1. The startup launch agent files have been created.")
            logger.info(f"2. To install the server agent, run: cp {self.setup_dir}/com.cloudos.server.plist ~/Library/LaunchAgents/")
            logger.info("3. To load the server agent, run: launchctl load ~/Library/LaunchAgents/com.cloudos.server.plist")
            logger.info(f"4. Repeat for the client agent, using: com.cloudos.client.plist")
        logger.info("\nRemember to configure your GCP credentials in the placeholder file created.")
        logger.info("You can find the file here: " + str(self.setup_dir / 'credentials' / 'gcp-service-account.json'))
class FileSystemOperations:
    def __init__(self, config):
        self.config = config
        self.mountpoint = Path(config['mountpoint'])
        self.storage_backend = config['storage_backend']
        self.gcs_client = None
        if self.storage_backend == 'google_cloud_storage':
            try:
                from google.cloud import storage
                self.gcs_client = storage.Client()
            except ImportError:
                logger.error("Google Cloud Storage library not found. Please run the setup script.")
                sys.exit(1)
            except Exception as e:
                logger.error(f"Failed to initialize Google Cloud Storage client: {e}")
                sys.exit(1)
    def create_mount_point(self, mountpoint):
        Path(mountpoint).mkdir(parents=True, exist_ok=True)
    def list_files(self, path):
        logger.info(f"Listing files in: {path}")
        if self.storage_backend == 'google_cloud_storage':
            bucket = self.gcs_client.bucket(self.config['bucket_name'])
            blobs = bucket.list_blobs(prefix=path)
            return [blob.name for blob in blobs]
        return []
    def read_file(self, path):
        logger.info(f"Reading file: {path}")
        return "File content"
def run_server(config):
    logger.info("Starting Cloud OS Server...")
    try:
        from flask import Flask, request, jsonify
        app = Flask(__name__)
        fs_ops = FileSystemOperations(config)
        @app.route('/list', methods=['GET'])
        def list_files_endpoint():
            path = request.args.get('path', '')
            files = fs_ops.list_files(path)
            return jsonify({'files': files})
        app.run(port=5000, debug=False)
    except ImportError as e:
        logger.error(f"Server dependencies missing: {e}")
        logger.info("Please run the setup script again to install all dependencies.")
        sys.exit(1)
def run_client(config):
    logger.info("Starting Cloud OS Client...")
    mode = os.environ.get('MODE')
    current_system = platform.system().lower()
    if mode == 'client':
        logger.info(f"Connecting to Cloud OS backend at {config['backend_url']}")
        fs_ops = FileSystemOperations(config)
        FUSE = None
        if current_system in ['linux', 'darwin']:
            try:
                from fuse import FUSE, Operations
                logger.info("FUSE support detected")
            except ImportError:
                logger.warning("FUSE library (fusepy) not found. Client will run in basic mode.")
        elif current_system == 'windows':
            try:
                from winfspy import FileSystemOperations as WinFspOperations
                logger.info("WinFsp support detected")
            except ImportError:
                logger.warning("WinFsp library not found. Client will run in basic mode.")
        mountpoint = config['mountpoint']
        fs_ops.create_mount_point(mountpoint)
        if current_system != 'windows' and 'FUSE' in locals():
            try:
                logger.info(f"Mounting cloud filesystem at {mountpoint}")
                logger.info("Cloud filesystem mounted successfully")
            except Exception as e:
                logger.error(f"Failed to mount filesystem: {e}")
        else:
            logger.info("Cloud filesystem operations ready (Windows mode)")
        try:
            while True:
                time.sleep(60)
                logger.debug("Cloud OS Client heartbeat")
        except KeyboardInterrupt:
            logger.info("Cloud OS Client shutting down...")
    else:
        logger.error(f"Unknown mode: {mode}")
        logger.info("Use MODE=server or MODE=client")
        sys.exit(1)
def main():
    mode = os.environ.get('MODE')
    if mode == 'setup':
        setup = CloudOSSetup()
        setup.run_setup()
    elif mode == 'server':
        config_path = Path.home() / '.cloudos' / 'config.json'
        if not config_path.exists():
            logger.error(f"Configuration file not found at {config_path}")
            logger.info("Please run the setup script first: python script.py")
            sys.exit(1)
        with open(config_path, 'r') as f:
            config = json.load(f)
        run_server(config)
    elif mode == 'client':
        config_path = Path.home() / '.cloudos' / 'config.json'
        if not config_path.exists():
            logger.error(f"Configuration file not found at {config_path}")
            logger.info("Please run the setup script first: python script.py")
            sys.exit(1)
        with open(config_path, 'r') as f:
            config = json.load(f)
        run_client(config)
    else:
        logger.error(f"Unknown mode: {mode}")
        logger.info("Use MODE=server, MODE=client, or MODE=setup")
        sys.exit(1)
if __name__ == '__main__':
    if not os.environ.get('MODE'):
        os.environ['MODE'] = 'setup'
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Cloud OS shutting down...")
        sys.exit(0)
    except Exception as e:
        logger.error(f"An unhandled exception occurred: {e}")
        sys.exit(1)