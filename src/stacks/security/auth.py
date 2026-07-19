import logging
import secrets
import string
from datetime import datetime, timedelta
import bcrypt
from functools import wraps

from flask import session, request, jsonify, redirect, url_for, current_app

logger = logging.getLogger("auth")

# In-memory tracking of attempts and lockouts
login_attempts: dict[str, list[datetime]] = {}
login_lockouts: dict[str, datetime] = {}

# Generic per-IP rate limiting for non-login endpoints (in-memory only)
generic_attempts: dict[str, list[datetime]] = {}

def generate_secret_key():
    """Generate 192bit secret key"""
    alphabet = string.ascii_letters + string.digits + "-_"
    return ''.join(secrets.choice(alphabet) for _ in range(32))

def hash_password(password):
    """Hash a password using bcrypt"""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(password, hashed):
    """Verify a password against a bcrypt hash"""
    try:
        return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))
    except Exception:
        return False

def is_valid_bcrypt_hash(hash_string):
    """Check if a string looks like a valid bcrypt hash"""
    if not hash_string:
        return False
    # Bcrypt hashes start with $2a$, $2b$, or $2y$ and are 60 characters
    return (hash_string.startswith(('$2a$', '$2b$', '$2y$')) and len(hash_string) == 60)

def check_rate_limit(ip):
    """Check if IP is rate limited. Returns (allowed, message)"""
    # Check if locked out
    if ip in login_lockouts:
        lockout_until = login_lockouts[ip]
        if datetime.now() < lockout_until:
            remaining = int((lockout_until - datetime.now()).total_seconds() / 60)
            return False, f"Too many failed attempts. Try again in {remaining} minutes."
        else:
            # Lockout expired
            del login_lockouts[ip]
            if ip in login_attempts:
                del login_attempts[ip]
    
    # Check attempts
    if ip not in login_attempts:
        login_attempts[ip] = []
    
    # Clean old attempts for ALL IPs (older than 10 minutes)
    cutoff = datetime.now() - timedelta(minutes=10)
    expired_ips = []
    for tracked_ip, attempts in login_attempts.items():
        login_attempts[tracked_ip] = [t for t in attempts if t > cutoff]
        if not login_attempts[tracked_ip]:  # No recent attempts
            expired_ips.append(tracked_ip)

    # Remove IPs with no recent attempts
    for tracked_ip in expired_ips:
        del login_attempts[tracked_ip]

    # Also clean expired lockouts
    expired_lockouts = [tracked_ip for tracked_ip, until in login_lockouts.items() if datetime.now() >= until]
    for tracked_ip in expired_lockouts:
        del login_lockouts[tracked_ip]

    # Re-initialize current IP if it was cleaned
    if ip not in login_attempts:
        login_attempts[ip] = []
    
    if len(login_attempts[ip]) >= 5:
        # Lock out for 10 minutes
        login_lockouts[ip] = datetime.now() + timedelta(minutes=10)
        return False, "Too many failed attempts. Try again in 10 minutes."
    
    return True, None

def record_failed_attempt(ip):
    """Record a failed login attempt"""
    if ip not in login_attempts:
        login_attempts[ip] = []
    login_attempts[ip].append(datetime.now())

def clear_attempts(ip):
    """Clear login attempts for IP after successful login"""
    if ip in login_attempts:
        del login_attempts[ip]
    if ip in login_lockouts:
        del login_lockouts[ip]

def require_login(f):
    """Require a logged-in session for HTML pages (index, etc.)."""
    @wraps(f)    
    def wrapper(*args, **kwargs):
        cfg = current_app.stacks_config
        disable_auth = cfg.get("login", "disable")

        if disable_auth:
            return f(*args, **kwargs)
        
        if not session.get("logged_in"):
            # Note: blueprint name is "api", endpoint is "login"
            return redirect(url_for("api.login"))
        return f(*args, **kwargs)
    return wrapper

def validate_api_key(provided_key):
    """
    Validate an API key and return its type.
    Returns: (is_valid: bool, key_type: str | None)
    - "admin": Full access admin key
    - "downloader": Limited downloader key
    - None: Invalid key
    """
    if not provided_key:
        return False, None

    cfg = current_app.stacks_config
    admin_key = cfg.get("api", "key")
    downloader_key = cfg.get("api", "downloader_key", default=None)

    if provided_key == admin_key:
        return True, "admin"
    elif downloader_key and provided_key == downloader_key:
        return True, "downloader"
    else:
        return False, None

def require_auth(f):
    """
    Require EITHER:
    - A logged-in session (web UI), OR
    - A valid X-API-Key / ?api_key=... token (external tools).
    - The authentication to be disabled in the config

    Accepts both admin and downloader keys.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        # Allow for disabled auth altogether
        cfg = current_app.stacks_config
        disable_auth = cfg.get("login", "disable")

        if disable_auth:
            return f(*args, **kwargs)
        # Web session takes precedence
        if session.get("logged_in"):
            return f(*args, **kwargs)

        # API key path
        provided_key = (
            request.headers.get("X-API-Key")
            or request.args.get("api_key")
        )

        is_valid, key_type = validate_api_key(provided_key)
        if is_valid:
            # Store key type in request context for permission checking
            request.key_type = key_type
            return f(*args, **kwargs)

        return (
            jsonify({"success": False, "error": "Authentication required"}),
            401,
        )
    return wrapper

def require_auth_with_permissions(allow_downloader=False):
    """
    Decorator that checks authentication and permissions.

    Args:
        allow_downloader: If True, downloader keys are allowed. If False, only admin keys/sessions.

    Usage:
        @require_auth_with_permissions(allow_downloader=True)
        def endpoint_that_downloader_can_access():
            ...

        @require_auth_with_permissions(allow_downloader=False)
        def admin_only_endpoint():
            ...
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            # Allow for disabled auth altogether
            cfg = current_app.stacks_config
            disable_auth = cfg.get("login", "disable")

            if disable_auth:
                return f(*args, **kwargs)

            # Web session takes precedence (always has admin rights)
            if session.get("logged_in"):
                return f(*args, **kwargs)

            # API key path
            provided_key = (
                request.headers.get("X-API-Key")
                or request.args.get("api_key")
            )

            is_valid, key_type = validate_api_key(provided_key)

            if not is_valid:
                return (
                    jsonify({"success": False, "error": "Authentication required"}),
                    401,
                )

            # Check permissions
            if key_type == "admin":
                # Admin always has access
                return f(*args, **kwargs)
            elif key_type == "downloader":
                if allow_downloader:
                    return f(*args, **kwargs)
                else:
                    return (
                        jsonify({"success": False, "error": "Insufficient permissions. Admin access required."}),
                        403,
                    )

            # Shouldn't reach here, but just in case
            return (
                jsonify({"success": False, "error": "Authentication required"}),
                401,
            )
        return wrapper
    return decorator

def require_session_only(f):
    """Require *only* a logged-in session (UI-only endpoints)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        # Allow for disabled auth altogether
        cfg = current_app.stacks_config
        disable_auth = cfg.get("login", "disable")

        if disable_auth:
            return f(*args, **kwargs)
        
        if session.get("logged_in"):
            return f(*args, **kwargs)
        return (
            jsonify(
                {"success": False, "error": "Session authentication required"}
            ),
            401,
        )
    return wrapper


def rate_limit_by_ip(max_attempts=10, window_seconds=60):
    """
    Simple in-memory rate limiter for non-login endpoints.
    Limits each IP to `max_attempts` requests within `window_seconds`.
    Note: this is process-local and will not share state across workers.
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            ip = request.remote_addr or "unknown"
            now = datetime.now()
            cutoff = now - timedelta(seconds=window_seconds)

            # Keep only attempts within the window
            attempts = generic_attempts.get(ip, [])
            attempts = [t for t in attempts if t > cutoff]
            generic_attempts[ip] = attempts

            if len(attempts) >= max_attempts:
                return jsonify({
                    "success": False,
                    "error": f"Too many requests. Try again in {window_seconds} seconds."
                }), 429

            generic_attempts[ip].append(now)
            return f(*args, **kwargs)
        return wrapper
    return decorator