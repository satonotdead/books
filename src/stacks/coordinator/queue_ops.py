"""
Queue operations using SQLite.

Provides thread-safe and process-safe queue management via SQLite.
All operations are atomic and handle concurrent access from multiple processes.
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from stacks.constants import WORKER_HEARTBEAT_INTERVAL
from stacks.coordinator.database import (
    get_connection,
    get_heartbeat_connection,
    row_to_dict,
    rows_to_list,
)

logger = logging.getLogger(__name__)


class QueueOperations:
    """
    Thread-safe and process-safe queue operations via SQLite.

    Each method gets its own database connection to ensure process safety.
    """

    def __init__(self):
        # Heartbeats are best-effort liveness signals. Throttling here makes
        # every caller honor the configured interval, including idle polling
        # loops that run more frequently.
        self._last_heartbeat_at = {}
        self._last_heartbeat_state = {}

    # -------------------------------------------------------------------------
    # Adding downloads
    # -------------------------------------------------------------------------

    def add_download(
        self,
        md5: str,
        source: str = None,
        subfolder: str = None,
        title: str = None
    ) -> tuple[bool, str]:
        """
        Add a new download to the queue.

        Args:
            md5: The MD5 hash of the file to download
            source: Optional source identifier (e.g., 'annas-archive', 'browser')
            subfolder: Optional subfolder within the download directory
            title: Optional title for display

        Returns:
            Tuple of (success: bool, message: str)
        """
        conn = get_connection()
        try:
            # Check if already exists (any status except completed/failed)
            cursor = conn.execute(
                "SELECT status FROM downloads WHERE md5 = ?",
                (md5,)
            )
            existing = cursor.fetchone()

            if existing:
                status = existing['status']
                if status in ('pending_scrape', 'scraping', 'queued', 'downloading'):
                    return False, f"Already in queue (status: {status})"
                elif status == 'completed':
                    return False, "Already downloaded successfully"
                # If failed, allow re-adding (will update the existing row)

            # Insert or update
            conn.execute("""
                INSERT INTO downloads (md5, source, subfolder, title, status, added_at)
                VALUES (?, ?, ?, ?, 'pending_scrape', ?)
                ON CONFLICT(md5) DO UPDATE SET
                    source = excluded.source,
                    subfolder = excluded.subfolder,
                    title = excluded.title,
                    status = 'pending_scrape',
                    added_at = excluded.added_at,
                    completed_at = NULL,
                    error = NULL,
                    success = NULL,
                    assigned_worker = NULL,
                    assigned_mirror = NULL
            """, (md5, source, subfolder, title, datetime.now().isoformat()))

            conn.commit()
            logger.info(f"Added to queue: {md5}{f' (subfolder: {subfolder})' if subfolder else ''}")
            return True, "Added to queue"

        except Exception as e:
            logger.error(f"Failed to add download: {e}")
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()

    def remove_download(self, md5: str) -> bool:
        """
        Remove a download from the queue.

        Only removes items that are queued or pending_scrape.
        Does not remove items that are currently downloading.

        Args:
            md5: The MD5 hash of the download to remove

        Returns:
            True if removed, False if not found or not removable
        """
        conn = get_connection()
        try:
            cursor = conn.execute("""
                DELETE FROM downloads
                WHERE md5 = ? AND status IN ('pending_scrape', 'scraping', 'queued')
                RETURNING md5
            """, (md5,))

            removed = cursor.fetchone() is not None
            conn.commit()

            if removed:
                logger.info(f"Removed from queue: {md5}")
            return removed

        except Exception as e:
            logger.error(f"Failed to remove download: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def clear_queue(self) -> int:
        """
        Clear all queued items (not currently downloading).

        Returns:
            Number of items removed
        """
        conn = get_connection()
        try:
            cursor = conn.execute("""
                DELETE FROM downloads
                WHERE status IN ('pending_scrape', 'scraping', 'queued')
            """)
            count = cursor.rowcount
            conn.commit()
            logger.info(f"Cleared queue: {count} items removed")
            return count
        except Exception as e:
            logger.error(f"Failed to clear queue: {e}")
            conn.rollback()
            return 0
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # Status queries
    # -------------------------------------------------------------------------

    def get_status(self, history_limit: int = 10) -> dict:
        """
        Get current queue status.

        Returns:
            Dictionary with:
            - active: list of currently downloading items
            - queued: list of items waiting to download
            - history: list of recently completed/failed items
            - workers: list of worker heartbeat info
            - busy_mirrors: list of currently busy mirror domains
        """
        conn = get_connection()
        try:
            # Get active downloads
            cursor = conn.execute("""
                SELECT * FROM downloads
                WHERE status = 'downloading'
                ORDER BY added_at
            """)
            active = rows_to_list(cursor.fetchall())

            # Parse JSON fields for active downloads
            for item in active:
                if item.get('assigned_mirror'):
                    try:
                        item['assigned_mirror'] = json.loads(item['assigned_mirror'])
                    except json.JSONDecodeError:
                        pass
                if item.get('progress'):
                    try:
                        item['progress'] = json.loads(item['progress'])
                    except json.JSONDecodeError:
                        item['progress'] = None

            # Get queued items (including pending_scrape and scraping)
            cursor = conn.execute("""
                SELECT * FROM downloads
                WHERE status IN ('pending_scrape', 'scraping', 'queued')
                ORDER BY added_at
            """)
            queued = rows_to_list(cursor.fetchall())

            # Get recent history
            cursor = conn.execute("""
                SELECT * FROM downloads
                WHERE status IN ('completed', 'failed')
                ORDER BY completed_at DESC
                LIMIT ?
            """, (history_limit,))
            history = rows_to_list(cursor.fetchall())

            # Get busy mirrors
            cursor = conn.execute("SELECT domain, worker_id FROM busy_mirrors")
            busy_mirrors = rows_to_list(cursor.fetchall())

            # Get pause state
            cursor = conn.execute(
                "SELECT value FROM system_flags WHERE key = 'paused'"
            )
            row = cursor.fetchone()
            paused = bool(row and row['value'] == '1')

            heartbeat_conn = get_heartbeat_connection()
            try:
                cursor = heartbeat_conn.execute("SELECT * FROM worker_heartbeats")
                workers = rows_to_list(cursor.fetchall())
            finally:
                heartbeat_conn.close()

            return {
                'active': active,
                'queued': queued,
                'queue_size': len(queued),
                'history': history,
                'workers': workers,
                'busy_mirrors': busy_mirrors,
                'paused': paused
            }

        finally:
            conn.close()

    def get_download_by_md5(self, md5: str) -> Optional[dict]:
        """Get a download by its MD5 hash."""
        conn = get_connection()
        try:
            cursor = conn.execute(
                "SELECT * FROM downloads WHERE md5 = ?",
                (md5,)
            )
            row = cursor.fetchone()
            return row_to_dict(row)
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # Job dispatch - Scraper
    # -------------------------------------------------------------------------

    def claim_scrape_job(self, worker_id: str) -> Optional[dict]:
        """
        Claim a scrape job (item needing metadata fetch).

        Atomically finds an item with status='pending_scrape' and marks it
        as 'scraping' with the given worker_id.

        Args:
            worker_id: Identifier for the scraper worker

        Returns:
            The claimed job as a dict, or None if no jobs available
        """
        conn = get_connection()
        try:
            # Atomically claim a job
            cursor = conn.execute("""
                UPDATE downloads
                SET status = 'scraping', assigned_worker = ?
                WHERE id = (
                    SELECT id FROM downloads
                    WHERE status = 'pending_scrape'
                    ORDER BY added_at
                    LIMIT 1
                )
                RETURNING *
            """, (worker_id,))

            row = cursor.fetchone()
            conn.commit()

            if row:
                logger.debug(f"Scraper {worker_id} claimed job: {row['md5']}")
                return row_to_dict(row)
            return None

        except Exception as e:
            logger.error(f"Failed to claim scrape job: {e}")
            conn.rollback()
            return None
        finally:
            conn.close()

    def complete_scrape(
        self,
        md5: str,
        filename: Optional[str],
        mirrors: list,
        error: Optional[str] = None
    ) -> bool:
        """
        Complete a scrape job with the fetched metadata.

        Args:
            md5: The MD5 hash of the download
            filename: The filename from metadata
            mirrors: List of mirror objects with {url, domain, type}
            error: Optional error message if scraping failed

        Returns:
            True if successful
        """
        conn = get_connection()
        try:
            if error or not mirrors:
                # Scraping failed - mark as failed
                conn.execute("""
                    UPDATE downloads
                    SET status = 'failed',
                        error = ?,
                        completed_at = ?,
                        assigned_worker = NULL
                    WHERE md5 = ? AND status = 'scraping'
                """, (error or 'No mirrors found', datetime.now().isoformat(), md5))
            else:
                # Scraping succeeded - move to queued
                # Set title to filename if not already set
                conn.execute("""
                    UPDATE downloads
                    SET status = 'queued',
                        filename = ?,
                        title = COALESCE(title, ?),
                        mirrors = ?,
                        assigned_worker = NULL
                    WHERE md5 = ? AND status = 'scraping'
                """, (filename, filename, json.dumps(mirrors), md5))

            conn.commit()
            logger.debug(f"Scrape completed for {md5}: {len(mirrors)} mirrors")
            return True

        except Exception as e:
            logger.error(f"Failed to complete scrape: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # Job dispatch - Download workers
    # -------------------------------------------------------------------------

    def claim_download_job(self, worker_id: str) -> Optional[dict]:
        """
        Find and claim a download job with an available mirror.

        This is the core of the mirror coordination logic:
        1. Get all queued downloads
        2. For each, check if any of its mirrors are not busy
        3. Try to claim the first available mirror
        4. If successful, claim the download job

        The mirror claim uses INSERT which fails (IntegrityError) if
        the mirror is already claimed, providing atomic coordination.

        Args:
            worker_id: Identifier for the download worker

        Returns:
            The claimed job with 'assigned_mirror' populated, or None
        """
        conn = get_connection()
        try:
            # Get all queued downloads ordered by added_at
            cursor = conn.execute("""
                SELECT * FROM downloads
                WHERE status = 'queued'
                ORDER BY added_at
            """)
            # IMPORTANT: Fetch all rows before iterating - SQLite cursor gets
            # invalidated if we execute other queries while iterating
            queued_rows = cursor.fetchall()

            logger.debug(f"Found {len(queued_rows)} queued downloads to check")

            for row in queued_rows:
                mirrors_json = row['mirrors']
                if not mirrors_json:
                    continue

                try:
                    mirrors = json.loads(mirrors_json)
                except json.JSONDecodeError:
                    logger.warning(f"Invalid mirrors JSON for {row['md5']}")
                    continue

                # Try to claim a mirror for this download
                logger.debug(f"Trying {len(mirrors)} mirrors for {row['md5']}")
                for mirror in mirrors:
                    domain = self._extract_domain(mirror.get('url', ''))
                    if not domain:
                        domain = mirror.get('domain', '')
                    if not domain:
                        logger.debug(f"Skipping mirror with no domain: {mirror}")
                        continue

                    logger.debug(f"Attempting to claim mirror: {domain}")
                    try:
                        # Try to claim this mirror (atomic via UNIQUE constraint)
                        conn.execute("""
                            INSERT INTO busy_mirrors (domain, worker_id, claimed_at)
                            VALUES (?, ?, ?)
                        """, (domain, worker_id, datetime.now().isoformat()))

                        # Mirror claimed! Now claim the download job
                        conn.execute("""
                            UPDATE downloads
                            SET status = 'downloading',
                                assigned_worker = ?,
                                assigned_mirror = ?
                            WHERE id = ?
                        """, (worker_id, json.dumps(mirror), row['id']))

                        conn.commit()

                        result = row_to_dict(row)
                        result['assigned_mirror'] = mirror
                        logger.info(f"Worker {worker_id} claimed {row['md5']} from {domain}")
                        return result

                    except sqlite3.IntegrityError:
                        # Mirror already claimed by another worker, try next mirror
                        continue

            # No jobs with available mirrors
            return None

        except Exception as e:
            logger.error(f"Failed to claim download job: {e}")
            conn.rollback()
            return None
        finally:
            conn.close()

    def complete_download(
        self,
        worker_id: str,
        md5: str,
        success: bool,
        filepath: Optional[str] = None,
        error: Optional[str] = None,
        used_fast_download: bool = False
    ) -> bool:
        """
        Mark a download as complete and release the mirror.

        Args:
            worker_id: The worker that completed the download
            md5: The MD5 hash of the download
            success: Whether the download succeeded
            filepath: Path to the downloaded file (if successful)
            error: Error message (if failed)
            used_fast_download: Whether fast download was used

        Returns:
            True if successful
        """
        conn = get_connection()
        try:
            resolved_filename = Path(filepath).name if filepath else None

            # Get the current download info (to get assigned_mirror)
            cursor = conn.execute(
                "SELECT assigned_mirror FROM downloads WHERE md5 = ?",
                (md5,)
            )
            row = cursor.fetchone()

            # Release the mirror
            if row and row['assigned_mirror']:
                try:
                    mirror = json.loads(row['assigned_mirror'])
                    domain = self._extract_domain(mirror.get('url', ''))
                    if not domain:
                        domain = mirror.get('domain', '')
                    if domain:
                        conn.execute(
                            "DELETE FROM busy_mirrors WHERE domain = ?",
                            (domain,)
                        )
                except (json.JSONDecodeError, KeyError):
                    pass

            # Update download status
            status = 'completed' if success else 'failed'
            conn.execute("""
                UPDATE downloads
                SET status = ?,
                    success = ?,
                    filename = COALESCE(?, filename),
                    filepath = ?,
                    error = ?,
                    used_fast_download = ?,
                    completed_at = ?,
                    assigned_worker = NULL,
                    assigned_mirror = NULL
                WHERE md5 = ? AND assigned_worker = ?
            """, (
                status,
                1 if success else 0,
                resolved_filename,
                filepath,
                error,
                1 if used_fast_download else 0,
                datetime.now().isoformat(),
                md5,
                worker_id
            ))

            conn.commit()

            if success:
                logger.info(f"Download complete: {md5}")
            else:
                logger.warning(f"Download failed: {md5} - {error}")

            return True

        except Exception as e:
            logger.error(f"Failed to complete download: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # Mirror management
    # -------------------------------------------------------------------------

    def claim_mirror(self, domain: str, worker_id: str) -> bool:
        """
        Manually claim a mirror domain.

        Args:
            domain: The mirror domain to claim
            worker_id: The worker claiming it

        Returns:
            True if claimed, False if already claimed
        """
        conn = get_connection()
        try:
            conn.execute("""
                INSERT INTO busy_mirrors (domain, worker_id, claimed_at)
                VALUES (?, ?, ?)
            """, (domain, worker_id, datetime.now().isoformat()))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()

    def release_mirror(self, domain: str) -> bool:
        """
        Release a claimed mirror domain.

        Args:
            domain: The mirror domain to release

        Returns:
            True if released, False if not found
        """
        conn = get_connection()
        try:
            cursor = conn.execute(
                "DELETE FROM busy_mirrors WHERE domain = ? RETURNING domain",
                (domain,)
            )
            released = cursor.fetchone() is not None
            conn.commit()
            return released
        finally:
            conn.close()

    def release_worker_mirrors(self, worker_id: str) -> int:
        """
        Release all mirrors claimed by a worker.

        Used when a worker dies or is stopped.

        Args:
            worker_id: The worker whose mirrors to release

        Returns:
            Number of mirrors released
        """
        conn = get_connection()
        try:
            cursor = conn.execute(
                "DELETE FROM busy_mirrors WHERE worker_id = ?",
                (worker_id,)
            )
            count = cursor.rowcount
            conn.commit()
            if count > 0:
                logger.info(f"Released {count} mirrors from worker {worker_id}")
            return count
        finally:
            conn.close()

    def get_busy_mirrors(self) -> list[str]:
        """Get list of currently busy mirror domains."""
        conn = get_connection()
        try:
            cursor = conn.execute("SELECT domain FROM busy_mirrors")
            return [row['domain'] for row in cursor.fetchall()]
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # Worker health
    # -------------------------------------------------------------------------

    def heartbeat(
        self,
        worker_id: str,
        worker_type: str,
        download_id: Optional[int] = None
    ) -> None:
        """
        Record a worker heartbeat.

        Args:
            worker_id: Identifier for the worker
            worker_type: Type of worker ('download', 'scraper', 'coordinator')
            download_id: Optional ID of current download
        """
        now = time.monotonic()
        state = (worker_type, download_id)
        if (
            self._last_heartbeat_state.get(worker_id) == state
            and now - self._last_heartbeat_at.get(worker_id, 0) < WORKER_HEARTBEAT_INTERVAL
        ):
            return

        conn = get_heartbeat_connection()
        try:
            conn.execute("""
                INSERT INTO worker_heartbeats (worker_id, worker_type, last_seen, current_download_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    current_download_id = excluded.current_download_id
            """, (worker_id, worker_type, datetime.now().isoformat(), download_id))
            conn.commit()
            self._last_heartbeat_at[worker_id] = now
            self._last_heartbeat_state[worker_id] = state
        except Exception as e:
            logger.error(f"Failed to record heartbeat: {e}")
        finally:
            conn.close()

    def get_stale_workers(self, timeout_seconds: int = 30) -> list[str]:
        """
        Get workers that haven't sent a heartbeat recently.

        Args:
            timeout_seconds: How many seconds without heartbeat is "stale"

        Returns:
            List of stale worker IDs
        """
        conn = get_heartbeat_connection()
        try:
            cutoff = (datetime.now() - timedelta(seconds=timeout_seconds)).isoformat()
            cursor = conn.execute("""
                SELECT worker_id FROM worker_heartbeats
                WHERE last_seen < ?
            """, (cutoff,))
            return [row['worker_id'] for row in cursor.fetchall()]
        finally:
            conn.close()

    def cleanup_dead_worker(self, worker_id: str) -> None:
        """
        Clean up resources held by a dead worker.

        - Releases all mirrors claimed by the worker
        - Requeues any download the worker was processing
        - Removes the worker's heartbeat record

        Args:
            worker_id: The worker to clean up
        """
        conn = get_connection()
        cleanup_succeeded = False
        try:
            # Release mirrors
            conn.execute(
                "DELETE FROM busy_mirrors WHERE worker_id = ?",
                (worker_id,)
            )

            # Honor cancel_remove commands before requeuing the rest
            conn.execute("""
                DELETE FROM downloads
                WHERE assigned_worker = ? AND status = 'downloading'
                  AND command = 'cancel_remove'
            """, (worker_id,))

            # Requeue remaining downloads (set back to 'queued' status)
            conn.execute("""
                UPDATE downloads
                SET status = 'queued',
                    assigned_worker = NULL,
                    assigned_mirror = NULL,
                    command = NULL
                WHERE assigned_worker = ? AND status = 'downloading'
            """, (worker_id,))

            # Also requeue scraping jobs
            conn.execute("""
                UPDATE downloads
                SET status = 'pending_scrape',
                    assigned_worker = NULL
                WHERE assigned_worker = ? AND status = 'scraping'
            """, (worker_id,))

            conn.commit()
            cleanup_succeeded = True
            logger.info(f"Cleaned up dead worker: {worker_id}")

        except Exception as e:
            logger.error(f"Failed to cleanup dead worker: {e}")
            conn.rollback()
        finally:
            conn.close()

        # Only discard the liveness signal after durable cleanup succeeds. If
        # the queue database is temporarily unavailable, the coordinator can
        # retry on its next pass.
        if cleanup_succeeded:
            self.remove_worker_heartbeat(worker_id)

    def remove_worker_heartbeat(self, worker_id: str) -> None:
        """Remove a worker's heartbeat record (for clean shutdown)."""
        conn = get_heartbeat_connection()
        try:
            conn.execute(
                "DELETE FROM worker_heartbeats WHERE worker_id = ?",
                (worker_id,)
            )
            conn.commit()
        finally:
            conn.close()
        self._last_heartbeat_at.pop(worker_id, None)
        self._last_heartbeat_state.pop(worker_id, None)

    # -------------------------------------------------------------------------
    # History
    # -------------------------------------------------------------------------

    def get_history(self, limit: int = 100) -> list[dict]:
        """
        Get download history (completed and failed).

        Args:
            limit: Maximum number of items to return

        Returns:
            List of history items, most recent first
        """
        conn = get_connection()
        try:
            cursor = conn.execute("""
                SELECT * FROM downloads
                WHERE status IN ('completed', 'failed')
                ORDER BY completed_at DESC
                LIMIT ?
            """, (limit,))
            return rows_to_list(cursor.fetchall())
        finally:
            conn.close()

    def retry_failed(self, md5: str) -> tuple[bool, str]:
        """
        Retry a failed download.

        Args:
            md5: The MD5 hash of the download to retry

        Returns:
            Tuple of (success: bool, message: str)
        """
        conn = get_connection()
        try:
            # Check if the download exists and is failed
            cursor = conn.execute(
                "SELECT status FROM downloads WHERE md5 = ?",
                (md5,)
            )
            row = cursor.fetchone()

            if not row:
                return False, "Download not found"
            if row['status'] != 'failed':
                return False, f"Download is not failed (status: {row['status']})"

            # Reset to pending_scrape
            conn.execute("""
                UPDATE downloads
                SET status = 'pending_scrape',
                    error = NULL,
                    completed_at = NULL,
                    success = NULL,
                    assigned_worker = NULL,
                    assigned_mirror = NULL,
                    added_at = ?
                WHERE md5 = ?
            """, (datetime.now().isoformat(), md5))

            conn.commit()
            logger.info(f"Retrying failed download: {md5}")
            return True, "Added to queue for retry"

        except Exception as e:
            logger.error(f"Failed to retry download: {e}")
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()

    def clear_history(self) -> int:
        """
        Clear all completed/failed downloads from history.

        Returns:
            Number of items removed
        """
        conn = get_connection()
        try:
            cursor = conn.execute("""
                DELETE FROM downloads
                WHERE status IN ('completed', 'failed')
            """)
            count = cursor.rowcount
            conn.commit()
            logger.info(f"Cleared history: {count} items removed")
            return count
        except Exception as e:
            logger.error(f"Failed to clear history: {e}")
            conn.rollback()
            return 0
        finally:
            conn.close()

    def remove_history_item(self, md5: str) -> bool:
        """
        Remove a single completed or failed download from history.

        Args:
            md5: The MD5 hash of the history item to remove

        Returns:
            True if removed, False if not found or not a history item
        """
        conn = get_connection()
        try:
            cursor = conn.execute("""
                DELETE FROM downloads
                WHERE md5 = ? AND status IN ('completed', 'failed')
                RETURNING md5
            """, (md5,))
            removed = cursor.fetchone() is not None
            conn.commit()

            if removed:
                logger.info(f"Removed history item: {md5}")
            return removed
        except Exception as e:
            logger.error(f"Failed to remove history item: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # Pause / cancel controls
    # -------------------------------------------------------------------------

    def is_paused(self) -> bool:
        """Return True if downloads are currently paused."""
        conn = get_connection()
        try:
            cursor = conn.execute(
                "SELECT value FROM system_flags WHERE key = 'paused'"
            )
            row = cursor.fetchone()
            return bool(row and row['value'] == '1')
        finally:
            conn.close()

    def set_paused(self, paused: bool) -> None:
        """Set the global pause state."""
        conn = get_connection()
        try:
            conn.execute("""
                INSERT INTO system_flags (key, value, updated_at)
                VALUES ('paused', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
            """, ('1' if paused else '0', datetime.now().isoformat()))
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to set pause state: {e}")
            conn.rollback()
        finally:
            conn.close()

    def command_active_downloads(self, command: str) -> int:
        """
        Set a command on all currently active (downloading) items.

        Args:
            command: 'cancel_requeue' or 'cancel_remove'

        Returns:
            Number of downloads that received the command
        """
        conn = get_connection()
        try:
            cursor = conn.execute(
                "UPDATE downloads SET command = ? WHERE status = 'downloading'",
                (command,)
            )
            count = cursor.rowcount
            conn.commit()
            logger.info(f"Sent '{command}' to {count} active download(s)")
            return count
        except Exception as e:
            logger.error(f"Failed to command active downloads: {e}")
            conn.rollback()
            return 0
        finally:
            conn.close()

    def update_download_progress(
        self,
        md5: str,
        worker_id: str,
        progress: dict
    ) -> None:
        """
        Write live progress data for an active download.

        Called frequently by the worker; uses a short busy_timeout to
        avoid blocking when the status reader holds the write lock.

        Args:
            md5: The MD5 hash of the download
            worker_id: The worker reporting progress (safety check)
            progress: Dict with percent, downloaded, total_size, speed
        """
        conn = get_connection()
        try:
            conn.execute("""
                UPDATE downloads
                SET progress = ?
                WHERE md5 = ? AND assigned_worker = ? AND status = 'downloading'
            """, (json.dumps(progress), md5, worker_id))
            conn.commit()
        except Exception:
            pass  # Non-fatal; progress updates are best-effort
        finally:
            conn.close()

    def get_download_command(self, md5: str) -> Optional[str]:
        """Get the pending command for a download, or None."""
        conn = get_connection()
        try:
            cursor = conn.execute(
                "SELECT command FROM downloads WHERE md5 = ?",
                (md5,)
            )
            row = cursor.fetchone()
            return row['command'] if row else None
        finally:
            conn.close()

    def requeue_download(self, md5: str, worker_id: str) -> bool:
        """
        Cancel an active download and put it back in the queue.

        Releases the claimed mirror and resets status to 'queued'.

        Args:
            md5: The MD5 hash of the download
            worker_id: The worker that was processing it

        Returns:
            True if successful
        """
        conn = get_connection()
        try:
            cursor = conn.execute(
                "SELECT assigned_mirror FROM downloads WHERE md5 = ?",
                (md5,)
            )
            row = cursor.fetchone()

            if row and row['assigned_mirror']:
                try:
                    mirror = json.loads(row['assigned_mirror'])
                    domain = self._extract_domain(mirror.get('url', ''))
                    if not domain:
                        domain = mirror.get('domain', '')
                    if domain:
                        conn.execute(
                            "DELETE FROM busy_mirrors WHERE domain = ?",
                            (domain,)
                        )
                except (json.JSONDecodeError, KeyError):
                    pass

            conn.execute("""
                UPDATE downloads
                SET status = 'queued',
                    assigned_worker = NULL,
                    assigned_mirror = NULL,
                    command = NULL,
                    progress = NULL
                WHERE md5 = ? AND assigned_worker = ?
            """, (md5, worker_id))

            conn.commit()
            logger.info(f"Requeued download: {md5}")
            return True

        except Exception as e:
            logger.error(f"Failed to requeue download: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def remove_active_download(self, md5: str, worker_id: str) -> bool:
        """
        Cancel an active download and delete it entirely.

        Releases the claimed mirror and removes the download record.

        Args:
            md5: The MD5 hash of the download
            worker_id: The worker that was processing it

        Returns:
            True if successful
        """
        conn = get_connection()
        try:
            cursor = conn.execute(
                "SELECT assigned_mirror FROM downloads WHERE md5 = ?",
                (md5,)
            )
            row = cursor.fetchone()

            if row and row['assigned_mirror']:
                try:
                    mirror = json.loads(row['assigned_mirror'])
                    domain = self._extract_domain(mirror.get('url', ''))
                    if not domain:
                        domain = mirror.get('domain', '')
                    if domain:
                        conn.execute(
                            "DELETE FROM busy_mirrors WHERE domain = ?",
                            (domain,)
                        )
                except (json.JSONDecodeError, KeyError):
                    pass

            conn.execute(
                "DELETE FROM downloads WHERE md5 = ? AND assigned_worker = ?",
                (md5, worker_id)
            )

            conn.commit()
            logger.info(f"Removed active download: {md5}")
            return True

        except Exception as e:
            logger.error(f"Failed to remove active download: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # Utilities
    # -------------------------------------------------------------------------

    def _extract_domain(self, url: str) -> str:
        """Extract domain from a URL."""
        try:
            parsed = urlparse(url)
            return parsed.netloc or ''
        except Exception:
            return ''


# Module-level singleton for convenience
_queue_ops = None


def get_queue_ops() -> QueueOperations:
    """Get or create the global QueueOperations instance."""
    global _queue_ops
    if _queue_ops is None:
        _queue_ops = QueueOperations()
    return _queue_ops
