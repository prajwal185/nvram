# ----------- Stage 1: Build dependencies --------------
# Use a specific, slim Python base image for consistency and smaller size
FROM python:3.9.18-slim-bullseye AS builder

# Set the working directory inside the builder container
WORKDIR /app

# Copy only the requirements file first to leverage Docker cache
COPY requirements.txt .

# Install build dependencies and pip packages locally under /install
# This optimizes for smaller final image by removing build tools later
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && pip install --upgrade pip \
    && pip install --prefix=/install -r requirements.txt \
    && apt-get purge -y build-essential \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# ----------- Stage 2: Final runtime image --------------
# Use the same slim Python base image for the final runtime
FROM python:3.9.18-slim-bullseye

# Set the working directory inside the final container
WORKDIR /app

# Copy installed python packages from the builder stage to the final image's /usr/local
COPY --from=builder /install /usr/local

# Copy the main application file
COPY main.py .

# Add /usr/local/bin to PATH to ensure installed executables are found
ENV PATH=/usr/local/bin:$PATH

# Expose the port where the Flask application will listen
EXPOSE 8080

# Create a non-root user for better security practices
RUN useradd -m cloudromuser
USER cloudromuser

# Command to run the Gunicorn server.
# --bind 0.0.0.0:8080: Binds the server to all network interfaces on port 8080.
# --workers 4: Configures 4 worker processes for handling requests.
# main:app: Specifies that Gunicorn should run the 'app' Flask application from 'main.py'.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "4", "main:app"]
