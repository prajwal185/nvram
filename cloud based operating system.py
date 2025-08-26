import os
import boto3
import jwt
from botocore.exceptions import ClientError
from flask import Flask, request, jsonify, abort
from functools import wraps
from cachetools import cached, TTLCache
import logging
import sys
import requests
import subprocess
import uuid
from errno import ENOENT, EIO
from fuse import FUSE, Operations, FuseOSError

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
app = Flask(__name__)

# --- Corrected Section ---
# Define environment variables first
IMAGE_BUCKET = os.environ.get("IMAGE_BUCKET_NAME")
DEVICE_TABLE_NAME = os.environ.get("DEVICE_TABLE_NAME")
JWT_SECRET_ARN = os.environ.get("CLOUDROM_SECRET_ARN")

# Check if required environment variables are set before proceeding
if not all([IMAGE_BUCKET, DEVICE_TABLE_NAME, JWT_SECRET_ARN]):
    app.logger.error("Missing one or more required environment variables: IMAGE_BUCKET_NAME, DEVICE_TABLE_NAME, CLOUDROM_SECRET_ARN")
if not all([IMAGE_BUCKET, DEVICE_TABLE_NAME, JWT_SECRET_ARN]):
    app.logger.error("Missing one or more required environment variables: IMAGE_BUCKET_NAME, DEVICE_TABLE_NAME, CLOUDROM_SECRET_ARN")
    exit(1)
aws_region = os.environ.get("AWS_REGION", "us-east-1")
s3_client = boto3.client('s3', region_name=aws_region)
dynamodb = boto3.resource('dynamodb', region_name=aws_region)
secrets_manager = boto3.client('secretsmanager', region_name=aws_region)
secret_cache = TTLCache(maxsize=1, ttl=300)

@cached(secret_cache)
def get_jwt_secret():
    try:
        response = secrets_manager.get_secret_value(SecretId=JWT_SECRET_ARN)
        return response['SecretString']
    except ClientError as e:
        app.logger.error(f"JWT secret retrieval failure from Secrets Manager: {e}")
        raise

device_table = dynamodb.Table(DEVICE_TABLE_NAME)

def token_required(f):
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
            app.logger.warning(f"Token expired for device: {request.device_id if hasattr(request, 'device_id') else 'unknown'}")
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
    if not path:
        return ''
    normalized = os.path.normpath(path).lstrip(os.sep)
    if normalized.startswith('..') or normalized.startswith('./..'):
        app.logger.warning(f"Attempted path traversal detected: {path}")
        abort(400, "Invalid path parameter.")
    return normalized

@app.route('/device/register', methods=['POST'])
def register():
    data = request.get_json()
    if not data or 'device_id' not in data:
        app.logger.warning("Missing 'device_id' in registration request.")
        abort(400, "Missing device_id parameter.")
    device_id = data['device_id']
    try:
        device_table.put_item(
            Item={'device_id': device_id, 'status': 'registered'},
            ConditionExpression='attribute_not_exists(device_id)'
        )
        app.logger.info(f"Device {device_id} registered successfully.")
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            app.logger.info(f"Device {device_id} already registered. Issuing new token.")
        else:
            app.logger.error(f"DynamoDB error during device registration for {device_id}: {e}")
            abort(500, "Device registration failed.")
    except Exception as e:
        app.logger.error(f"Unexpected error during device registration for {device_id}: {e}")
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
    image_key = "rootfs.img"
    try:
        url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': IMAGE_BUCKET, 'Key': image_key},
            ExpiresIn=3600
        )
        app.logger.info(f"Generated presigned URL for {image_key} for device {request.device_id}.")
        return jsonify({'url': url})
    except ClientError as e:
        app.logger.error(f"Failed to generate presigned URL for {image_key}: {e}")
        abort(500, "Failed to retrieve image URL.")
    except Exception as e:
        app.logger.critical(f"Unexpected error retrieving latest image: {e}")
        abort(500, "Internal server error.")

@app.route('/cloudfs/list', methods=['GET'])
@token_required
def list_dir():
    path = normalize_path(request.args.get('path', '/'))
    if path and not path.endswith('/') and path != '':
        path += '/'
    app.logger.info(f"Listing directory {path} for device {request.device_id}.")
    try:
        resp = s3_client.list_objects_v2(Bucket=IMAGE_BUCKET, Prefix=path, Delimiter='/')
        dirs = [p['Prefix'].rstrip('/').split('/')[-1] for p in resp.get('CommonPrefixes', [])]
        files = [o['Key'].split('/')[-1] for o in resp.get('Contents', []) if o['Key'] != path]
        return jsonify({'dirs': dirs, 'files': files})
    except ClientError as e:
        app.logger.error(f"S3 list directory failed for path {path}: {e}")
        abort(500, "Failed to list directory.")
    except Exception as e:
        app.logger.critical(f"Unexpected error listing directory {path}: {e}")
        abort(500, "Internal server error.")

@app.route('/cloudfs/file', methods=['GET'])
@token_required
def get_file():
    path = normalize_path(request.args.get('path'))
    if not path:
        app.logger.warning("Missing 'path' parameter for file retrieval.")
        abort(400, "Missing 'path' parameter.")
    app.logger.info(f"Retrieving file URL for {path} for device {request.device_id}.")
    try:
        s3_client.head_object(Bucket=IMAGE_BUCKET, Key=path)
        url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': IMAGE_BUCKET, 'Key': path},
            ExpiresIn=300
        )
        return jsonify({'url': url})
    except ClientError as e:
        if e.response['Error']['Code'] == '404' or e.response['Error']['Code'] == 'NoSuchKey':
            app.logger