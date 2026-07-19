"""
Download worker process.

Each download worker runs in its own process and:
1. Sends heartbeats to indicate it's alive
2. Claims download jobs from the queue
3. Downloads files, trying multiple mirrors if needed
4. Reports completion/failure back to the coordinator
"""

import json
import logging
import signal
import sys
from multiprocessing import Event
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from stacks.config.config import Config
from stacks.constants import DOWNLOAD_PATH, PROJECT_ROOT
from stacks.coordinator.queue_ops import QueueOperations
from stacks.downloader.html import DEAD_MIRROR_DOMAINS, JS_SPA_MIRROR_DOMAINS
from stacks.downloader.sources import filter_mirrors_for_policy

logger = logging.getLogger(__name__)


def create_downloader(config: Config, progress_callback=None, status_callback=None):
    """
    Create an AnnaDownloader instance with the given config.

    Args:
        config: Config instance to read settings from
        progress_callback: Optional callback for progress updates
        status_callback: Optional callback for status messages

    Returns:
        AnnaDownloader instance
    """
    from stacks.downloader.downloader import AnnaDownloader

    # Get fast download config
    fast_config = {
        'enabled': config.get('fast_download', 'enabled', default=False),
        'key': config.get('fast_download', 'key'),
        'path_index': 0,
        'domain_index': 0
    }

    # Get FlareSolverr config
    flaresolverr_enabled = config.get('flaresolverr', 'enabled', default=False)
    flaresolverr_url = config.get('flaresolverr', 'url', default='http://localhost:8191')
    flaresolverr_timeout = config.get('flaresolverr', 'timeout', default=60)
    flaresolverr_timeout_ms = flaresolverr_timeout * 1000

    # Get file naming config
    prefer_title_naming = config.get('downloads', 'prefer_title_naming', default=False)
    include_hash = config.get('downloads', 'include_hash', default="none")

    # Get incomplete folder path
    incomplete_folder_path = config.get('downloads', 'incomplete_folder_path', default='/download/incomplete')
    incomplete_dir = PROJECT_ROOT / incomplete_folder_path.lstrip('/')

    # Get proxy config
    proxy_config = {
        'enabled': config.get('proxy', 'enabled', default=False),
        'url': config.get('proxy', 'url'),
        'username': config.get('proxy', 'username'),
        'password': config.get('proxy', 'password')
    }

    allow_external_mirrors = config.get('downloads', 'allow_external_mirrors', default=False)

    return AnnaDownloader(
        output_dir=DOWNLOAD_PATH,
        incomplete_dir=incomplete_dir,
        progress_callback=progress_callback,
        status_callback=status_callback,
        fast_download_config=fast_config,
        flaresolverr_url=flaresolverr_url if flaresolverr_enabled else None,
        flaresolverr_timeout=flaresolverr_timeout_ms,
        prefer_title_naming=prefer_title_naming,
        include_hash=include_hash,
        proxy_config=proxy_config,
        allow_external_mirrors=allow_external_mirrors
    )


def _extract_domain(url: str) -> str:
    """Extract domain from a URL."""
    try:
        parsed = urlparse(url)
        return parsed.netloc or ''
    except Exception:
        return ''


def download_worker_process(
    worker_id: str,
    config_path: Path,
    stop_event: Event
) -> None:
    """
    Download worker process entry point.

    This function runs in a separate process and handles downloading files.
    It will try multiple mirrors if the first one fails.

    Args:
        worker_id: Unique identifier for this worker (e.g., "download-0")
        config_path: Path to the config file
        stop_event: Event to signal the worker to stop
    """
    # Setup logging - write to stdout AND the shared log file so /api/logs shows worker output
    from stacks.utils.logutils import get_log_file_path, LOG_FORMAT, LOG_DATE_FORMAT
    _log_file = get_log_file_path()
    _log_file.parent.mkdir(parents=True, exist_ok=True)
    _formatter = logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT)
    _root = logging.getLogger()
    _root.setLevel(logging.INFO)
    for _h in _root.handlers[:]:
        _root.removeHandler(_h)
        _h.close()
    _ch = logging.StreamHandler(sys.stdout)
    _ch.setFormatter(_formatter)
    _root.addHandler(_ch)
    _fh = logging.FileHandler(_log_file, encoding='utf-8')
    _fh.setFormatter(_formatter)
    _root.addHandler(_fh)
    worker_logger = logging.getLogger(f'worker.{worker_id}')
    worker_logger.info(f"Download worker {worker_id} starting")

    # Handle signals gracefully
    def signal_handler(signum, frame):
        worker_logger.info(f"Worker {worker_id} received signal {signum}, stopping")
        stop_event.set()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Initialize
    queue_ops = QueueOperations()
    config = Config(config_path)
    downloader = None
    cancel_flag = None  # None, 'cancel_requeue', or 'cancel_remove'

    # Track last heartbeat / command-check / progress-write times (mutable for closure)
    import time as _time
    _last_heartbeat = [0.0]
    _last_command_check = [0.0]
    _last_progress_write = [0.0]
    _current_md5 = [None]  # updated when a job is claimed

    # Progress callback that checks for cancellation and sends periodic heartbeats
    def progress_callback(progress):
        nonlocal cancel_flag
        if stop_event.is_set() or cancel_flag:
            return False  # Signal to cancel
        now = _time.monotonic()
        # Write progress to DB roughly every 1 second so the UI can display it
        if _current_md5[0] and now - _last_progress_write[0] >= 1.0:
            _last_progress_write[0] = now
            try:
                queue_ops.update_download_progress(_current_md5[0], worker_id, progress)
            except Exception:
                pass
        # Check DB for cancel commands every 5 seconds
        if _current_md5[0] and now - _last_command_check[0] >= 5:
            _last_command_check[0] = now
            try:
                cmd = queue_ops.get_download_command(_current_md5[0])
                if cmd in ('cancel_requeue', 'cancel_remove'):
                    cancel_flag = cmd
                    return False
            except Exception:
                pass
        # Send a heartbeat roughly every 10 seconds while downloading
        if now - _last_heartbeat[0] >= 10:
            try:
                queue_ops.heartbeat(worker_id, 'download')
            except Exception:
                pass
            _last_heartbeat[0] = now
        return True

    def status_callback(status_message):
        worker_logger.debug(f"Status: {status_message}")

    # Main worker loop
    while not stop_event.is_set():
        try:
            # Send heartbeat
            queue_ops.heartbeat(worker_id, 'download')

            # Respect pause state before picking up a new job
            if queue_ops.is_paused():
                worker_logger.debug("Worker paused, waiting...")
                stop_event.wait(timeout=5)
                continue

            # Try to claim a job
            job = queue_ops.claim_download_job(worker_id)

            if not job:
                # No jobs available, wait and retry
                worker_logger.debug("No jobs available, waiting...")
                stop_event.wait(timeout=2)
                continue

            worker_logger.info(f"Got job: {job.get('md5')} with mirror: {job.get('assigned_mirror')}")

            md5 = job['md5']
            _current_md5[0] = md5
            filename = job.get('filename')
            title = job.get('title') or filename
            subfolder = job.get('subfolder')
            assigned_mirror = job.get('assigned_mirror')

            # Parse all available mirrors
            mirrors_json = job.get('mirrors', '[]')
            try:
                all_mirrors = json.loads(mirrors_json) if isinstance(mirrors_json, str) else mirrors_json
            except json.JSONDecodeError:
                all_mirrors = []

            all_mirrors = filter_mirrors_for_policy(
                all_mirrors,
                config.get('downloads', 'allow_external_mirrors', default=False)
            )
            assigned_mirror_claimed = assigned_mirror is not None
            if assigned_mirror and not filter_mirrors_for_policy(
                [assigned_mirror],
                config.get('downloads', 'allow_external_mirrors', default=False)
            ):
                assigned_mirror = None
                assigned_mirror_claimed = False

            worker_logger.info(f"Claimed job: {md5} ({len(all_mirrors)} mirrors available)")

            # Initialize downloader lazily
            if downloader is None:
                worker_logger.info("Initializing downloader")
                config.load()  # Reload config
                downloader = create_downloader(config, progress_callback, status_callback)

            # Reset per-job state
            cancel_flag = None
            _last_command_check[0] = 0.0
            _last_progress_write[0] = 0.0

            # Perform download
            success = False
            filepath = None
            error = None
            used_fast_download = False

            try:
                # Get config values
                resume_attempts = config.get('downloads', 'resume_attempts', default=3)

                # Try fast download first if enabled
                if downloader.fast_download_enabled and downloader.fast_download_key:
                    worker_logger.info("Trying fast download")
                    fd_success, fd_result = downloader.try_fast_download(md5)

                    if fd_success:
                        worker_logger.info("Using fast download")
                        filepath = downloader.download_direct(
                            fd_result,
                            title=title,
                            resume_attempts=resume_attempts,
                            md5=md5,
                            subfolder=subfolder
                        )
                        if filepath:
                            success = True
                            used_fast_download = True
                        elif stop_event.is_set():
                            worker_logger.info("Download cancelled during fast download")
                            continue

                # If fast download didn't work, try mirrors
                if not success and not stop_event.is_set():
                    # Build list of mirrors to try (assigned first, then others)
                    mirrors_to_try = []
                    tried_domains = set()

                    # Add assigned mirror first
                    if assigned_mirror:
                        mirrors_to_try.append(assigned_mirror)
                        domain = _extract_domain(assigned_mirror.get('url', ''))
                        if domain:
                            tried_domains.add(domain)

                    # Add remaining mirrors
                    for mirror in all_mirrors:
                        domain = _extract_domain(mirror.get('url', ''))

                        # Skip known dead mirrors (permanently 5xx or DNS failure)
                        if domain and any(domain == dead or domain.endswith('.' + dead) for dead in DEAD_MIRROR_DOMAINS):
                            worker_logger.debug(f"Skipping known dead mirror: {domain}")
                            continue

                        # Skip known JS SPA mirrors (no static download links)
                        if domain and any(domain == spa or domain.endswith('.' + spa) for spa in JS_SPA_MIRROR_DOMAINS):
                            worker_logger.debug(f"Skipping JS SPA mirror (no static links): {domain}")
                            continue

                        if domain and domain not in tried_domains:
                            mirrors_to_try.append(mirror)
                            tried_domains.add(domain)

                    # Try each mirror
                    last_error = None
                    for i, mirror in enumerate(mirrors_to_try):
                        if stop_event.is_set():
                            break

                        domain = _extract_domain(mirror.get('url', '')) or mirror.get('domain', 'unknown')
                        worker_logger.info(f"Trying mirror {i+1}/{len(mirrors_to_try)}: {domain}")

                        # For mirrors after the first, we need to claim them
                        secondary_mirror_claimed = False
                        if i > 0 or not assigned_mirror_claimed:
                            secondary_mirror_claimed = queue_ops.claim_mirror(domain, worker_id)
                            if not secondary_mirror_claimed:
                                worker_logger.info(f"Mirror {domain} is busy, skipping")
                                continue

                        try:
                            filepath = downloader.download_from_mirror(
                                mirror['url'],
                                mirror.get('type', 'external_mirror'),
                                md5,
                                title=title,
                                resume_attempts=resume_attempts,
                                subfolder=subfolder
                            )

                            if filepath:
                                success = True
                                worker_logger.info(f"Download successful from {domain}")
                                break
                            else:
                                last_error = f"Mirror {domain} failed"
                                worker_logger.warning(last_error)

                        except Exception as e:
                            last_error = f"Mirror {domain} error: {e}"
                            worker_logger.warning(last_error)

                        finally:
                            # Only release mirrors this worker actually claimed
                            if secondary_mirror_claimed:
                                queue_ops.release_mirror(domain)

                    if not success and last_error:
                        error = last_error

                if not success and not error:
                    error = "All mirrors failed"

            except Exception as e:
                worker_logger.error(f"Download error: {e}", exc_info=True)
                error = str(e)
                success = False

            # Check if we should stop (global stop_event)
            if stop_event.is_set():
                # Don't report completion, let coordinator handle cleanup
                worker_logger.info(f"Worker stopping, leaving job {md5} for cleanup")
                break

            # Handle cancel actions before reporting normal completion
            if cancel_flag == 'cancel_requeue':
                worker_logger.info(f"Requeuing cancelled download: {md5}")
                queue_ops.requeue_download(md5, worker_id)
                _current_md5[0] = None
                cancel_flag = None
                continue

            if cancel_flag == 'cancel_remove':
                worker_logger.info(f"Removing cancelled download: {md5}")
                queue_ops.remove_active_download(md5, worker_id)
                _current_md5[0] = None
                cancel_flag = None
                continue

            # Report normal completion (also releases the initially assigned mirror)
            queue_ops.complete_download(
                worker_id=worker_id,
                md5=md5,
                success=success,
                filepath=str(filepath) if filepath else None,
                error=error,
                used_fast_download=used_fast_download
            )

            _current_md5[0] = None

            if success:
                worker_logger.info(f"Download complete: {title or md5}")
            else:
                worker_logger.warning(f"Download failed: {title or md5} - {error}")

            # Rate limiting between downloads
            delay = config.get('downloads', 'delay', default=2)
            if delay > 0 and not stop_event.is_set():
                stop_event.wait(timeout=delay)

        except Exception as e:
            worker_logger.error(f"Worker loop error: {e}", exc_info=True)
            # Wait before retrying to avoid tight error loops
            stop_event.wait(timeout=5)

    # Cleanup
    worker_logger.info(f"Download worker {worker_id} stopping")
    queue_ops.remove_worker_heartbeat(worker_id)

    if downloader:
        try:
            downloader.cleanup()
        except Exception as e:
            worker_logger.error(f"Error cleaning up downloader: {e}")

    worker_logger.info(f"Download worker {worker_id} stopped")
