# Configuration

## Main Configuration File

Located at `/opt/stacks/config/config.yaml` (or your mounted config directory):

```yaml
server:
  host: "0.0.0.0"
  port: 7788

login:
  username: null # Auto-generated on first run
  password: null # Auto-generated on first run (bcrypt hashed)
  disable: false # Disable all security requirements (API keys and login/password)

api:
  key: null # Auto-generated on first run
  session_secret: null # Auto-generated on first run

downloads:
  delay: 2 # Delay in seconds
  retry_count: 3
  resume_attempts: 3

fast_download:
  enabled: false
  key: null

flaresolverr:
  enabled: false # Enables or disables the use of FlareSolverr
  url: null # The URL and port of your FlareSolverr instance (e.g., http://flaresolverr:8191)
  timeout: 60 # How long to wait for FlareSolverr to return a result (10-300 seconds)

queue:
  max_history: 100

logging:
  level: "INFO" # DEBUG, INFO, WARN, ERROR
```

All settings can be modified through the web interface Settings tab or by editing the config file directly. Changes through the web interface take effect immediately without requiring a restart. Editing the file requires a server restart for the changes to take hold. Deleting the file will create a new one upon next server start.

## Environment Variables

Set in `docker-compose.yml` or via `.env` file:

```bash
# Create a .env file in the same directory as docker-compose.yml
USERNAME=admin
PASSWORD=your_secure_password
SOLVERR_URL=flaresolverr:8191
TZ=UTC
```

```yaml
environment:
  - TZ=${TZ:-UTC} # Timezone for logs and timestamps
  - USERNAME=${USERNAME:-admin} # Initial username (seeds config on first run)
  - PASSWORD=${PASSWORD} # Initial password (seeds config on first run)
  - SOLVERR_URL=${SOLVERR_URL:-flaresolverr:8191} # Enables FlareSolverr and embeds the URL/port on first run
  - RESET_ADMIN=true # Force password reset
  - FLASK_DEBUG=true # Sets Flask into Debug mode on startup

```

**Note:** `USERNAME` and `PASSWORD` variables only seed the initial configuration. Once the config file exists, environment variables are ignored unless the password hash is invalid or `RESET_ADMIN=true` is set. In other words, once you have persistent volumes and a valid config it is safe to remove them from the compose file.

**Best Practice:** Create a `.env` file to store sensitive values separately from the `docker-compose.yml` file. This keeps secrets out of version control and makes configuration easier to manage.

**Security:** Always use strong, randomly generated passwords. Generate a secure password using:
```bash
openssl rand -base64 32
```
Never use default or predictable passwords in production.

## Network Access

To access Stacks from other devices on your network, the default configuration already exposes the port. Simply access it using your server's IP address:

```
http://192.168.1.100:7788
```

Make sure to update the Tampermonkey script settings on each device to point to your server's IP.

### User access rights

By default, Stacks runs as  `root` inside the container. This is normal fo rmany Docker images, but means that any files created or mounted volumes will also belong to `root` on the host. 

If your other pass can't access the downloaded files, or you prefer stricter permission control, you can tell Docker to run Stacks as a different user.

**Set a specific user in Docker Compose**
```yaml
 services:
   stacks:
     # Use previous config and add:
     user: 1000:1000 # Replace with the UID:GID you want Stacks to use
```
**Set a specific user in Docker CLI**
```bash
docker run -d \
  --user 1000:1000 \
  ...
  zelest/stacks:latest
``` 

If Stacks already have created files as `root`, you may need to update ownership nefore switching users:
```bash
sudo chown -R 1000:1000 /path/to/config
sudo chown -R 1000:1000 /path/to/download
sudo chown -R 1000:1000 /path/to/logs
```
Replace the UID/GID and paths to match your setup.
