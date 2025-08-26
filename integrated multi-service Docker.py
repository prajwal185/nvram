version: '3.9'

services:
  # ----------- Local DynamoDB for development/testing -----------
  dynamodb:
    image: amazon/dynamodb-local
    ports:
      - "8000:8000"
    command: "-jar DynamoDBLocal.jar -inMemory -sharedDb"

  # ----------- Local S3-compatible storage (MinIO) -------------
  minio:
    image: minio/minio
    environment:
      MINIO_ACCESS_KEY: minio
      MINIO_SECRET_KEY: miniosecret
    command: server /data
    ports:
      - "9000:9000"
      - "9001:9001"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/ready"]
      interval: 30s
      timeout: 20s
      retries: 5

  # ----------- (Optional) Redis for future Celery tasks --------
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"

  # ----------- Backend Flask API -----------
  backend:
    build:
      context: ./backend
      dockerfile: Dockerfile
    restart: always
    env_file: ./backend/.env
    environment:
      IMAGE_BUCKET_NAME: cloudrom-bucket
      DEVICE_TABLE_NAME: cloudrom-dev-table
      CLOUDROM_SECRET_ARN: dev-jwt-secret
      AWS_ACCESS_KEY_ID: minio
      AWS_SECRET_ACCESS_KEY: miniosecret
      AWS_REGION: us-east-1
      # Point to local stack services
      AWS_ENDPOINT_URL: http://minio:9000
      DYNAMODB_ENDPOINT_URL: http://dynamodb:8000
      # (Prometheus metrics exposed by default)
    depends_on:
      - minio
      - dynamodb
    ports:
      - "8080:8080"
    # In production, add a reverse proxy (nginx/ALB) for HTTPS
    command: >
      gunicorn --bind 0.0.0.0:8080 --workers 4 main:app

  # ----------- FUSE Client -----------
  cloudfs_fuse:
    build:
      context: ./cloudfs_fuse
      dockerfile: Dockerfile
    environment:
      CLOUDROM_BACKEND: http://backend:8080
      TOKEN: ""                      # Set via entrypoint or bootstrap
      MOUNTPOINT: /mnt/cloud
    privileged: true                 # Required for FUSE in Docker
    depends_on:
      - backend

    # For dev you can enter the container and run FUSE manually, or automate via bootstrap.
    volumes:
      - ./mnt_cloud:/mnt/cloud       # Mount host folder for FUSE

  # ----------- Device Bootstrap -----------
  cloudrom_init:
    build:
      context: ./bootstrap
      dockerfile: Dockerfile
    environment:
      CLOUDROM_BACKEND: http://backend:8080    # Internal DockerNet address
      MOUNTPOINT: /mnt/cloud
    depends_on:
      - backend
      - cloudfs_fuse

    # Entrypoint launches device register and FUSE client
    command: ["python3", "cloudrom-init.py"]

# ---------- Volumes ----------
volumes:
  mnt_cloud:

# ---------- Networks (optional for clear separation) ----------
networks:
  default:
    driver: bridge
