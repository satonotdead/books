#!/usr/bin/env python3
"""
Stacks main entry point.

Starts the multi-process download system:
- Initializes SQLite database
- Spawns coordinator, download workers, and scraper processes
- Starts Gunicorn web server
- Handles graceful shutdown
"""

import os
import sys
import signal
import argparse
import subprocess
import multiprocessing
import logging
from pathlib import Path

from stacks.constants import (
    CONFIG_FILE, PROJECT_ROOT, LOG_PATH, DOWNLOAD_PATH,
    GUNICORN_CONFIG_FILE, DOWNLOAD_WORKERS
)

# ANSI color codes (Dracula theme)
INFO = "\033[38;2;139;233;253m"       # cyan
WARN = "\033[38;2;255;184;108m"       # orange
GOOD = "\033[38;2;80;250;123m"        # green
PINK = "\033[38;2;255;102;217m"       # pink
PURPLE = "\033[38;2;178;102;255m"     # purple
BG = "\033[48;2;40;42;54m"            # black background
PINKBG = "\033[48;2;255;102;217m"     # pink background
RESET = "\033[0m"                     # reset

# Global reference for signal handlers
_process_manager = None
_gunicorn_process = None


def print_logo(version: str):
    """Display the super cool STACKS logo"""
    dashes = '─' * (52 - len(version))

    print(f"{BG}{PURPLE} ┌───────────────────────────────────────────────────────────┐ {RESET}")
    print(f"{BG}{PURPLE} │                                                           {PURPLE}│ {RESET}")
    print(f"{BG}{PURPLE} │{PINK}     ▄████▄ ████████  ▄█▄     ▄████▄  ██    ▄██ ▄████▄     {PURPLE}│ {RESET}")
    print(f"{BG}{PURPLE} │{PINK}    ██▀  ▀██   ██    ▄{PINKBG}{PURPLE}▄{BG}▀{PINKBG}▄{BG}{PINK}▄   ██▀  ▀██ ██  ▄██▀ ██▀  ▀██    {PURPLE}│ {RESET}")
    print(f"{BG}{PURPLE} │{PINK}    ██▄        ██    █{PURPLE}█ █{PINK}█  ██        ██▄██▀   ██▄         {PURPLE}│ {RESET}")
    print(f"{BG}{PURPLE} │{PINK}     ▀████▄    ██   █{PURPLE}█   █{PINK}█ ██        ████      ▀████▄     {PURPLE}│ {RESET}")
    print(f"{BG}{PURPLE} │{PINK}         ▀██   ██   █{PURPLE}█   █{PINK}█ ██        ██▀██▄        ▀██    {PURPLE}│ {RESET}")
    print(f"{BG}{PURPLE} │{PINK}    ██▄  ▄██   ██  █{PURPLE}█     █{PINK}█ ██▄  ▄██ ██  ▀██▄ ██▄  ▄██    {PURPLE}│ {RESET}")
    print(f"{BG}{PURPLE} │{PINK}     ▀████▀    ██  █{PURPLE}▀     ▀{PINK}█  ▀████▀  ██    ▀██ ▀████▀     {PURPLE}│ {RESET}")
    print(f"{BG}{PURPLE} │                                                           {PURPLE}│ {RESET}")
    print(f"{BG}{PURPLE} └{dashes}╢v{version}╟────┘ {RESET}")
    sys.stdout.flush()


def ensure_directories():
    """Ensure essential directories exist."""
    dirs = [
        Path(CONFIG_FILE).parent,
        Path(LOG_PATH),
        Path(DOWNLOAD_PATH),
    ]
    for directory in dirs:
        directory.mkdir(parents=True, exist_ok=True)


def setup_config(config_path):
    """Ensure a config file exists."""
    cfg_path = Path(config_path) if config_path else Path(CONFIG_FILE)

    print("◼ Checking configuration...")
    sys.stdout.flush()

    if not cfg_path.exists():
        print("  No config.yaml found - creating new one.")
        cfg_path.write_text("{}\n")
        cfg_path.chmod(0o600)
    else:
        print(f"  Using config at {cfg_path}")

    return str(cfg_path)


def init_database():
    """Initialize the SQLite database and run migrations."""
    print(f"{INFO}◼ Initializing database...{RESET}")
    sys.stdout.flush()

    from stacks.coordinator.database import init_database as db_init, migrate_from_json, startup_cleanup

    db_init()
    migrate_from_json()
    startup_cleanup()

    print(f"{GOOD}  Database ready{RESET}")
    sys.stdout.flush()


def prime_config(config_path: str):
    """Validate and persist config once before spawning other processes."""
    from stacks.config.config import Config

    Config(config_path)

    # RESET_ADMIN is intended as a one-shot bootstrap action.
    # Clear it after the first config write to avoid every child process
    # re-hashing credentials and racing on config.yaml during startup.
    if os.environ.get("RESET_ADMIN", "").lower() == "true":
        os.environ.pop("RESET_ADMIN", None)


def setup_signal_handlers_multiprocess():
    """Setup graceful shutdown handlers for multi-process mode."""
    def shutdown_handler(signum, frame):
        global _process_manager, _gunicorn_process

        signal_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        print(f"\n{WARN}◼ Received {signal_name}, shutting down gracefully...{RESET}")
        sys.stdout.flush()

        # Stop worker processes
        if _process_manager:
            print(f"{INFO}  Stopping worker processes...{RESET}")
            sys.stdout.flush()
            _process_manager.stop_all(timeout=10)

        # Stop Gunicorn (handles both subprocess.Popen and multiprocessing.Process)
        if _gunicorn_process:
            print(f"{INFO}  Stopping Gunicorn...{RESET}")
            sys.stdout.flush()
            _gunicorn_process.terminate()

            # Wait for process to stop
            if isinstance(_gunicorn_process, multiprocessing.Process):
                _gunicorn_process.join(timeout=10)
                if _gunicorn_process.is_alive():
                    print(f"{WARN}  Force killing Gunicorn...{RESET}")
                    _gunicorn_process.kill()
            else:
                try:
                    _gunicorn_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    print(f"{WARN}  Force killing Gunicorn...{RESET}")
                    _gunicorn_process.kill()

        print(f"{GOOD}◼ Shutdown complete{RESET}")
        sys.stdout.flush()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)


def setup_signal_handlers_debug(app):
    """Setup graceful shutdown handlers for debug mode (single process)."""
    def shutdown_handler(signum, frame):
        signal_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        print(f"\n{WARN}◼ Received {signal_name}, shutting down gracefully...{RESET}")
        sys.stdout.flush()

        # In debug mode, we still have the old worker thread
        if hasattr(app, 'stacks_worker') and app.stacks_worker:
            print(f"{INFO}  Stopping download worker...{RESET}")
            sys.stdout.flush()
            app.stacks_worker.stop()

        if hasattr(app, 'stacks_worker') and app.stacks_worker and hasattr(app.stacks_worker, 'downloader'):
            print(f"{INFO}  Cleaning up downloader...{RESET}")
            sys.stdout.flush()
            app.stacks_worker.downloader.cleanup()

        if hasattr(app, 'stacks_queue') and app.stacks_queue:
            print(f"{INFO}  Saving queue state...{RESET}")
            sys.stdout.flush()
            app.stacks_queue.save()

        print(f"{GOOD}◼ Shutdown complete{RESET}")
        sys.stdout.flush()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)


def start_worker_processes(config_path: str):
    """Start the worker processes (coordinator, download workers, scraper)."""
    global _process_manager

    print(f"{INFO}◼ Starting worker processes ({DOWNLOAD_WORKERS} download workers)...{RESET}")
    sys.stdout.flush()

    from stacks.coordinator.process_manager import ProcessManager

    _process_manager = ProcessManager(Path(config_path))
    _process_manager.start_all()

    print(f"{GOOD}  Worker processes started{RESET}")
    sys.stdout.flush()


def _run_gunicorn_pex():
    """Run gunicorn from within PEX (called in subprocess)."""
    from gunicorn.app.wsgiapp import run
    sys.argv = [
        "gunicorn",
        "--config", "python:stacks.gunicorn_config",
        "stacks.server.webserver:create_app()"
    ]
    run()


def start_gunicorn(config_path: str):
    """Start Gunicorn as a subprocess."""
    global _gunicorn_process

    print(f"{INFO}◼ Starting Gunicorn web server...{RESET}")
    sys.stdout.flush()

    # Set config path as environment variable for gunicorn workers
    os.environ["STACKS_CONFIG_PATH"] = config_path

    # Check if running from source or PEX
    if GUNICORN_CONFIG_FILE.exists():
        # Running from source - use subprocess with gunicorn command
        env = os.environ.copy()
        src_path = str(PROJECT_ROOT / "src")
        current_pythonpath = env.get("PYTHONPATH", "")
        if current_pythonpath:
            env["PYTHONPATH"] = f"{src_path}:{current_pythonpath}"
        else:
            env["PYTHONPATH"] = src_path

        gunicorn_cmd = [
            "gunicorn",
            "--config", str(GUNICORN_CONFIG_FILE),
            "stacks.server.webserver:create_app()"
        ]
        _gunicorn_process = subprocess.Popen(gunicorn_cmd, env=env)
        print(f"{GOOD}  Gunicorn started (PID: {_gunicorn_process.pid}){RESET}")
    else:
        # Running from PEX - use multiprocessing to run bundled gunicorn
        _gunicorn_process = multiprocessing.Process(target=_run_gunicorn_pex)
        _gunicorn_process.start()
        print(f"{GOOD}  Gunicorn started (PID: {_gunicorn_process.pid}){RESET}")

    sys.stdout.flush()
    return _gunicorn_process


def main():
    global _process_manager, _gunicorn_process

    # Use a clean interpreter for workers instead of inheriting the
    # parent's process state. This avoids hard-to-debug child crashes
    # on some container/runtime combinations.
    if multiprocessing.get_start_method(allow_none=True) != "spawn":
        multiprocessing.set_start_method("spawn", force=True)

    parser = argparse.ArgumentParser(description="Start the Stacks server.")
    parser.add_argument(
        "-c", "--config",
        help="Path to an alternative config.yaml file"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable Flask debug mode for development (auto-reload on file changes)"
    )
    args = parser.parse_args()

    # Set UTF-8 encoding
    os.environ.setdefault("LANG", "C.UTF-8")

    # Read version
    version_file = PROJECT_ROOT / "VERSION"
    version = version_file.read_text().strip() if version_file.exists() else "unknown"
    print_logo(version)

    # Ensure directories exist
    ensure_directories()

    # Load or create config.yaml
    config_path = setup_config(args.config)

    # Detect password reset request
    if os.environ.get("RESET_ADMIN", "false").lower() == "true":
        print("! RESET_ADMIN=true detected - admin password will be reset!\n")
        sys.stdout.flush()

    # Switch working dir
    os.chdir(PROJECT_ROOT)

    # Determine debug mode from CLI arg or environment variable
    debug_mode = args.debug or os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true")

    # Normalize config once in the parent process before any child processes start.
    prime_config(config_path)

    if debug_mode:
        # Debug mode: use old single-process architecture with Flask dev server
        print(f"{WARN}◼ Debug mode enabled - using Flask development server{RESET}")
        print(f"{WARN}  (Multi-process workers disabled in debug mode){RESET}")
        sys.stdout.flush()

        from stacks.server.webserver import create_app

        print("◼ Starting Stacks...")
        sys.stdout.flush()

        app = create_app(config_path, debug_mode=debug_mode)

        # Setup graceful shutdown handlers
        setup_signal_handlers_debug(app)

        host = app.stacks_host
        port = app.stacks_port

        app.run(host, port, debug=debug_mode, use_reloader=False)
    else:
        # Production mode: multi-process architecture
        print(f"{INFO}◼ Starting Stacks in multi-process mode...{RESET}")
        sys.stdout.flush()

        # Initialize database first
        init_database()

        # Setup signal handlers before starting processes
        setup_signal_handlers_multiprocess()

        # Start worker processes
        start_worker_processes(config_path)

        # Start Gunicorn
        gunicorn_proc = start_gunicorn(config_path)

        print(f"{GOOD}◼ Stacks is running!{RESET}")
        sys.stdout.flush()

        # Monitor loop: restart dead workers and wait for Gunicorn to exit
        try:
            while True:
                # Check if Gunicorn is still running
                if isinstance(gunicorn_proc, multiprocessing.Process):
                    if not gunicorn_proc.is_alive():
                        exit_code = gunicorn_proc.exitcode
                        print(f"{INFO}◼ Gunicorn exited with code {exit_code}{RESET}")
                        break
                else:
                    ret = gunicorn_proc.poll()
                    if ret is not None:
                        print(f"{INFO}◼ Gunicorn exited with code {ret}{RESET}")
                        break

                # Restart any worker processes that have died unexpectedly
                if _process_manager:
                    restarted = _process_manager.restart_dead_processes()
                    if restarted:
                        print(f"{WARN}  Restarted {restarted} dead worker process(es){RESET}")
                        sys.stdout.flush()

                import time
                time.sleep(5)

            # If Gunicorn exits, stop workers too
            if _process_manager:
                print(f"{INFO}  Stopping worker processes...{RESET}")
                _process_manager.stop_all()

        except KeyboardInterrupt:
            # Signal handler will take care of cleanup
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nError during startup: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
