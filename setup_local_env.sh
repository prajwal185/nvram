#!/bin/bash
set -e

# --- Prerequisites Check ---
echo "Checking for prerequisites..."
if ! command -v docker &> /dev/null
then
    echo "Docker is not installed. Please install Docker to run MinIO."
    exit 1
fi

if ! command -v java &> /dev/null
then
    echo "Java is not installed. Please install Java to run DynamoDB Local."
    exit 1
fi
echo "Prerequisites met."

# --- Start MinIO Container ---
MINIO_CONTAINER_NAME="local-minio"
if [ ! "$(docker ps -q -f name=$MINIO_CONTAINER_NAME)" ]; then
    echo "Starting MinIO container..."
    docker run -p 9000:9000 -p 9001:9001 -d \
        --name $MINIO_CONTAINER_NAME \
        -v "$(pwd)/minio_data:/data" \
        -e "MINIO_ROOT_USER=minioadmin" \
        -e "MINIO_ROOT_PASSWORD=minioadmin" \
        minio/minio server /data --console-address ":9001"
    echo "MinIO is running. Access console at http://127.0.0.1:9001"
fi

# --- Download and Start DynamoDB Local ---
DDB_LOCAL_JAR="DynamoDBLocal.jar"
if [ ! -f "$DDB_LOCAL_JAR" ]; then
    echo "Downloading DynamoDB Local..."
    wget -q https://s3-us-west-2.amazonaws.com/dynamodb-local/dynamodb_local_latest.zip
    unzip -q dynamodb_local_latest.zip
    rm dynamodb_local_latest.zip
    echo "Download complete."
fi

if ! pgrep -f "DynamoDBLocal.jar" > /dev/null
then
    echo "Starting DynamoDB Local in the background..."
    java -Djava.library.path=./DynamoDBLocal_lib -jar $DDB_LOCAL_JAR -sharedDb &
fi

# --- Create .env file ---
ENV_FILE=".env"
if [ ! -f "$ENV_FILE" ]; then
    echo "Creating .env file for local development."
    cat <<EOF > "$ENV_FILE"
# Set this to 'true' to use local services. Set to 'false' to use AWS.
USE_LOCAL_SERVICES=true

# Local MinIO and DynamoDB details
# These are used when USE_LOCAL_SERVICES is true
IMAGE_BUCKET_NAME=my-local-minio-bucket
DEVICE_TABLE_NAME=my-local-devices-table
CLOUDROM_SECRET_ARN=a_very_long_and_random_string_of_characters
EOF
    echo "`.env` file created. Remember to add it to your `.gitignore`."
fi

echo "Setup is complete. You can now run your Python application."