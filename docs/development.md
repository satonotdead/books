# Build your own image

## Prerequisites

- Docker and Docker Compose installed

## Building your image

1. **Clone the repository**
   ```bash
   git clone https://github.com/zelestcarlyone/stacks-docker.git
   cd stacks-docker
   ```
2. **Configure volumes**

   Edit `docker-compose.yml` and update the volume paths to match your system:

   ```yaml
   volumes:
     - /path/to/config:/opt/stacks/config # Configuration files
     - /path/to/downloads:/opt/stacks/download # Downloaded files
     - /path/to/logs:/opt/stacks/logs # Log files
   ```

   If you already got a service on port `7788`, update the ports:

   ```yaml
   ports:
     - "1234:7788"
   ```

3. **Build and start**

   ```bash
   sudo chmod +x build-and-launch.sh
   ./build-and-launch.sh
   ```

   The build-and-launch script will:

   - Stop any existing container
   - Remove old containers and images
   - Build a fresh image
   - Start the service
   - Attach to logs
