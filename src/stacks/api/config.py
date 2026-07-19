import logging
import time
from pathlib import Path
from flask import (
    jsonify,
    request,
    current_app,
)
from stacks.constants import KNOWN_MD5, PROJECT_ROOT
from . import api_bp
from stacks.utils.logutils import setup_logging
from stacks.utils.migrationutils import migrate_incomplete_folder
from stacks.utils.domainutils import try_domains_until_success
from stacks.security.auth import (
    require_auth_with_permissions,
    hash_password,
    rate_limit_by_ip,
)

logger = logging.getLogger("api")

@api_bp.route('/api/config/test_flaresolverr', methods=['POST'])
@require_auth_with_permissions(allow_downloader=False)
@rate_limit_by_ip(max_attempts=10, window_seconds=60)
def api_config_test_flaresolverr():
    """Test FlareSolverr connection"""
    data = request.json
    test_url = data.get('url', 'http://localhost:8191')
    timeout = data.get('timeout', 10)

    if not test_url:
        return jsonify({
            'success': False,
            'error': 'No URL provided'
        }), 400

    # Validate URL to prevent SSRF attacks
    if not _is_safe_flaresolverr_url(test_url):
        return jsonify({
            'success': False,
            'error': 'Invalid FlareSolverr URL format'
        }), 400

    # Normalize URL: add http:// if no scheme is present
    if not test_url.startswith(('http://', 'https://')):
        test_url = f"http://{test_url}"

    try:
        import requests

        # Try to connect to FlareSolverr's health endpoint
        response = requests.get(test_url, timeout=timeout)
        
        if response.status_code == 200:
            return jsonify({
                'success': True,
                'message': 'FlareSolverr is online and responding',
                'status_code': response.status_code
            })
        else:
            return jsonify({
                'success': False,
                'error': f'FlareSolverr returned status {response.status_code}'
            }), 400
            
    except requests.exceptions.Timeout:
        return jsonify({
            'success': False,
            'error': f'Connection timeout after {timeout} seconds'
        }), 408
    except requests.exceptions.ConnectionError:
        return jsonify({
            'success': False,
            'error': 'Could not connect to FlareSolverr. Is it running?'
        }), 503
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Connection failed: {str(e)}'
        }), 500


def _is_safe_flaresolverr_url(url):
    """Validate FlareSolverr URL to prevent SSRF attacks"""
    from urllib.parse import urlparse
    import re
    
    # Basic format check
    if not isinstance(url, str) or len(url) > 2048:
        return False
        
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or parsed.netloc.split(':')[0]
        
        # Check for IP addresses and hostnames
        if not hostname:
            return False
            
        # For FlareSolverr, we allow localhost and typical local addresses
        # but block private IP ranges that aren't typically used for FlareSolverr
        if hostname.lower() in ['localhost', '127.0.0.1', '::1']:
            return True  # Allow localhost addresses
            
        # Allow private IPs on common ports for FlareSolverr
        if _is_private_ip_no_loopback(hostname):
            # Allow common FlareSolverr ports
            port = parsed.port or (8191 if parsed.scheme == 'http' else 443 if parsed.scheme == 'https' else None)
            if port in [8191, 8192, 80, 443, 8080, 8443]:
                return True
            return False
            
        # Validate hostname format
        if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9\-_.]*[a-zA-Z0-9]$', hostname) and \
           not re.match(r'^(\d{1,3}\.){3}\d{1,3}$', hostname):
            return False
            
        # Check port range if specified
        if parsed.port and (parsed.port < 1 or parsed.port > 65535):
            return False
            
        # Only allow safe schemes
        return parsed.scheme in ['', 'http', 'https']
        
    except Exception:
        return False


def _is_private_ip_no_loopback(hostname):
    """Check if hostname resolves to a private IP address (excluding loopback)"""
    import socket
    import ipaddress
    
    try:
        # If it's already an IP address
        ip = ipaddress.ip_address(hostname)
        return ip.is_private and not ip.is_loopback
    except ValueError:
        # It's a hostname, try to resolve it
        try:
            addrinfo = socket.getaddrinfo(hostname, None)
            for res in addrinfo:
                ip = ipaddress.ip_address(res[4][0])
                if ip.is_private and not ip.is_loopback:
                    return True
        except socket.gaierror:
            # Can't resolve hostname, assume it's safe
            pass
    return False


@api_bp.route('/api/config/test_proxy', methods=['POST'])
@require_auth_with_permissions(allow_downloader=False)
@rate_limit_by_ip(max_attempts=10, window_seconds=60)
def api_config_test_proxy():
    """Test proxy connection"""
    data = request.json
    proxy_url = data.get('url')
    username = data.get('username')
    password = data.get('password')

    if not proxy_url:
        return jsonify({
            'success': False,
            'error': 'No proxy URL provided'
        }), 400

    # Validate proxy URL to prevent SSRF attacks
    if not _is_safe_proxy_url(proxy_url):
        return jsonify({
            'success': False,
            'error': 'Invalid proxy URL format'
        }), 400

    # Normalize URL: add http:// if no scheme is present
    if not proxy_url.startswith(('http://', 'https://', 'socks5://')):
        proxy_url = f"http://{proxy_url}"

    # Validate username and password to prevent injection
    if username and not _is_valid_proxy_auth(username):
        return jsonify({
            'success': False,
            'error': 'Invalid proxy username format'
        }), 400
        
    if password and not _is_valid_proxy_auth(password):
        return jsonify({
            'success': False,
            'error': 'Invalid proxy password format'
        }), 400

    # Build proxy URL with authentication if provided
    if username and password:
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(proxy_url)
        proxy_url = urlunparse((
            parsed.scheme,
            f"{username}:{password}@{parsed.netloc}",
            parsed.path, parsed.params, parsed.query, parsed.fragment
        ))

    try:
        import requests

        proxies = {
            'http': proxy_url,
            'https': proxy_url
        }

        # Test by making a request to Anna's Archive through the proxy.
        # This avoids leaking the proxy's external IP to a third-party service.
        response = requests.get(
            'https://annas-archive.gl',
            proxies=proxies,
            timeout=10
        )

        if response.status_code == 200:
            return jsonify({
                'success': True,
                'message': 'Proxy is working and can reach Anna\'s Archive'
            })
        else:
            return jsonify({
                'success': False,
                'error': f'Proxy returned status {response.status_code}'
            }), 400

    except requests.exceptions.Timeout:
        return jsonify({
            'success': False,
            'error': 'Connection timeout after 10 seconds'
        }), 408
    except requests.exceptions.ProxyError as e:
        return jsonify({
            'success': False,
            'error': f'Proxy error: {str(e)}'
        }), 503
    except requests.exceptions.ConnectionError:
        return jsonify({
            'success': False,
            'error': 'Could not connect through proxy. Check the URL and credentials.'
        }), 503
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Connection failed: {str(e)}'
        }), 500


def _is_safe_proxy_url(url):
    """Validate proxy URL to prevent SSRF attacks"""
    from urllib.parse import urlparse
    import re
    
    # Basic format check
    if not isinstance(url, str) or len(url) > 2048:
        return False
        
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or parsed.netloc.split(':')[0]
        
        # Check for IP addresses and hostnames
        if not hostname:
            return False
            
        # Block private IP ranges to prevent SSRF
        if _is_private_ip(hostname):
            return False
            
        # Validate hostname format
        if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9\-_.]*[a-zA-Z0-9]$', hostname) and \
           not re.match(r'^(\d{1,3}\.){3}\d{1,3}$', hostname):
            return False
            
        # Check port range if specified
        if parsed.port and (parsed.port < 1 or parsed.port > 65535):
            return False
            
        # Only allow safe schemes
        return parsed.scheme in ['', 'http', 'https', 'socks5', 'socks5h']
        
    except Exception:
        return False


def _is_private_ip(hostname):
    """Check if hostname resolves to a private IP address"""
    import socket
    import ipaddress
    
    try:
        # If it's already an IP address
        ip = ipaddress.ip_address(hostname)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        # It's a hostname, try to resolve it
        try:
            addrinfo = socket.getaddrinfo(hostname, None)
            for res in addrinfo:
                ip = ipaddress.ip_address(res[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local:
                    return True
        except socket.gaierror:
            # Can't resolve hostname, assume it's safe
            pass
    return False


def _is_valid_proxy_auth(auth_str):
    """Validate proxy username/password format to prevent injection"""
    if not isinstance(auth_str, str):
        return False
    # Check length and disallow control characters
    if len(auth_str) > 255 or not auth_str or any(ord(c) < 32 or ord(c) == 127 for c in auth_str):
        return False
    # Check for URL special characters that could cause injection
    import re
    return not re.search(r'[^\w\-_.@]', auth_str)


def _test_key_single_domain(test_key, domain):
    """Test fast download key with a specific domain."""
    import requests

    api_url = f'https://{domain}/dyn/api/fast_download.json'

    response = requests.get(
        api_url,
        params={
            'md5': KNOWN_MD5,
            'key': test_key
        },
        timeout=10
    )

    if response.status_code == 200:
        data = response.json()
        if data.get('download_url'):
            info = data.get('account_fast_download_info', {})
            return {
                'success': True,
                'message': 'Key is valid',
                'downloads_left': info.get('downloads_left'),
                'downloads_per_day': info.get('downloads_per_day'),
                'account_info': info
            }
        else:
            raise Exception('No download URL in response')
    elif response.status_code == 401:
        raise Exception('Invalid secret key')
    elif response.status_code == 403:
        raise Exception('Not a member')
    else:
        raise Exception(f'API returned status {response.status_code}')


@api_bp.route('/api/config/test_key', methods=['POST'])
@require_auth_with_permissions(allow_downloader=False)
@rate_limit_by_ip(max_attempts=10, window_seconds=60)
def api_config_test_key():
    """Test fast download key and update cached info"""
    data = request.json
    test_key = data.get('key')

    if not test_key:
        return jsonify({
            'success': False,
            'error': 'No key provided'
        }), 400

    # Validate key format to prevent injection
    if not _is_valid_secret_key_format(test_key):
        return jsonify({
            'success': False,
            'error': 'Invalid key format'
        }), 400

    try:
        # Use domain rotation to test the key
        result = try_domains_until_success(_test_key_single_domain, test_key)

        # Update the worker's cached info with timestamp
        worker = current_app.stacks_worker
        if worker.downloader.fast_download_key == test_key:
            worker.downloader.fast_download_info.update({
                'available': True,
                'downloads_left': result['downloads_left'],
                'downloads_per_day': result['downloads_per_day'],
                'last_refresh': time.time()
            })

        return jsonify({
            'success': True,
            'message': result['message'],
            'downloads_left': result['downloads_left'],
            'downloads_per_day': result['downloads_per_day']
        })

    except Exception as e:
        error_msg = str(e)

        # Return appropriate status codes
        if 'Invalid secret key' in error_msg:
            return jsonify({
                'success': False,
                'error': error_msg
            }), 401
        elif 'Not a member' in error_msg:
            return jsonify({
                'success': False,
                'error': error_msg
            }), 403
        else:
            return jsonify({
                'success': False,
                'error': f'Connection failed: {error_msg}'
            }), 500


def _is_valid_secret_key_format(key):
    """Validate secret key format to prevent injection"""
    import re
    if not isinstance(key, str):
        return False
    # Check length and format - should match the expected secret key format
    return re.match(r'^[A-Za-z0-9_-]{32,128}$', key) is not None
    
@api_bp.route('/api/config', methods=['POST'])
@require_auth_with_permissions(allow_downloader=False)
def api_config_update():
    """
    Update configuration using schema validation.
    """
    data = request.json
    logger = logging.getLogger('api')
    config = current_app.stacks_config
    worker = current_app.stacks_worker

    try:
        # Check if incomplete_folder_path is being changed
        old_incomplete_path = config.get('downloads', 'incomplete_folder_path', default='/download/incomplete')
        new_incomplete_path = None
        if 'downloads' in data and 'incomplete_folder_path' in data['downloads']:
            new_incomplete_path = data['downloads']['incomplete_folder_path']

        # Apply all config changes
        for section, values in data.items():
            if isinstance(values, dict):
                for key, new_value in values.items():
                    # Reject keys not defined in the schema to prevent config injection
                    if section not in config.schema or key not in config.schema[section]:
                        return jsonify({
                            "success": False,
                            "error": f"Invalid config key: {section}.{key}"
                        }), 400

                    # Special handling for password updates
                    if section == 'login' and key == 'new_password':
                        if new_value:  # Only update if new password is provided
                            hashed_password = hash_password(new_value)
                            config.set(section, 'password', value=hashed_password)
                            logger.info("Password updated successfully")
                    else:
                        config.set(section, key, value=new_value)

        # Validate config (this will normalize the path)
        config.data = config.validate(config.data, config.schema)
        config.ensure_login_credentials()

        # Get the validated/normalized new path
        if new_incomplete_path is not None:
            new_incomplete_path = config.get('downloads', 'incomplete_folder_path', default='/download/incomplete')

        # Handle incomplete folder migration if path changed
        migration_occurred = False
        if new_incomplete_path and new_incomplete_path != old_incomplete_path:
            logger.info(f"Incomplete folder path changed from {old_incomplete_path} to {new_incomplete_path}")

            # Stop active downloads and wait for them to finish (debug/single-process mode only)
            if worker is not None and worker.queue.current_download:
                logger.info("Cancelling active download for migration")
                worker.pause()  # Pause queue to prevent new downloads
                worker.cancel_and_requeue_current()  # Cancel current download

                # Wait for download to actually stop
                if not worker.wait_for_current_download_to_stop(timeout=10):
                    logger.warning("Current download did not stop within timeout")
                    return jsonify({
                        "success": False,
                        "error": "Could not stop current download for migration"
                    }), 500

            # Perform migration
            old_path = PROJECT_ROOT / old_incomplete_path.lstrip('/')
            new_path = PROJECT_ROOT / new_incomplete_path.lstrip('/')

            logger.info(f"Starting migration from {old_path} to {new_path}")
            success, message, stats = migrate_incomplete_folder(old_path, new_path)

            if not success:
                logger.error(f"Migration failed: {message}")
                logger.error(f"Migration stats: {stats}")
                # Don't save config if migration failed
                return jsonify({
                    "success": False,
                    "error": "Failed to change incomplete folder, see the logfile for details"
                }), 500

            logger.info(f"Migration completed: {message}")
            logger.info(f"Migration stats: {stats}")
            migration_occurred = True

        # Save config
        config.save()

        # Recreate downloader with new config (debug/single-process mode only)
        if worker is not None:
            worker.update_config()
        setup_logging(config)

        # Resume worker if we paused it
        if migration_occurred and worker is not None and worker.paused:
            worker.resume()

        import copy
        cfg = copy.deepcopy(config.get_all())
        if "api" in cfg and "key" in cfg["api"]:
            cfg["api"]["key"] = "***MASKED***"
        if "login" in cfg and "password" in cfg["login"]:
            cfg["login"]["password"] = "***MASKED***"

        response_message = "Configuration updated"
        if migration_occurred:
            response_message = f"Configuration updated. {message}"

        return jsonify({
            "success": True,
            "message": response_message,
            "config": cfg
        })

    except Exception as e:
        logger.error(f"Failed to update config: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "error": "Failed to change incomplete folder, see the logfile for details"
        }), 500

    
@api_bp.route('/api/config', methods=['GET'])
@require_auth_with_permissions(allow_downloader=False)
def api_config_get():
    """Get current configuration"""
    import copy
    config = current_app.stacks_config
    config_data = copy.deepcopy(config.get_all())
    # Mask sensitive data
    if 'api' in config_data and 'key' in config_data['api']:
        config_data['api']['key'] = '***MASKED***'
    if 'login' in config_data and 'password' in config_data['login']:
        config_data['login']['password'] = '***MASKED***'
    return jsonify(config_data)

@api_bp.route('/api/subdirs', methods=['GET'])
@require_auth_with_permissions(allow_downloader=True)
def api_subdirs_get():
    """Get list of available subdirectories"""
    config = current_app.stacks_config
    subdirs = config.get('downloads', 'subdirectories', default=None)

    # Return empty list if None or not a list
    if not subdirs or not isinstance(subdirs, list):
        subdirs = []

    return jsonify({
        'success': True,
        'subdirectories': subdirs
    })