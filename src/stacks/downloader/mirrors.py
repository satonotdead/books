import re
import time
from urllib.parse import urljoin

def download_from_mirror(d, mirror_url, mirror_type, md5, title=None, resume_attempts=3, subfolder=None):
    """
    Download from any mirror with stale cookie handling.

    Logic:
    - slow_download: Use pre-warmed cookies with direct HTTP requests
    - external_mirror: Try direct, use FlareSolverr on 403 (with cookie refresh)

    Args:
        subfolder: Subfolder path to save file to (optional)
    """
    try:
        if mirror_type == 'slow_download':
            d.logger.debug("Accessing slow download (via cookies)")

            # Try to load cached cookies for this domain (uses current working domain)
            d.load_cached_cookies()

            if hasattr(d, 'status_callback'):
                d.status_callback("Accessing slow download page...")

            try:
                # Try to fetch the slow_download page with cookies
                response = d.session.get(mirror_url, timeout=30)

                # If we get a challenge page (403/503), solve it with FlareSolverr
                if response.status_code in [403, 503]:
                    if not d.flaresolverr_url:
                        d.logger.warning(f"Got {response.status_code} but no FlareSolverr configured")
                        return None

                    d.logger.warning(f"Got {response.status_code}, solving challenge with FlareSolverr...")

                    if hasattr(d, 'status_callback'):
                        d.status_callback("Solving CAPTCHA with FlareSolverr...")

                    # Solve challenge for THIS specific URL
                    success, cookies, html_content = d.solve_with_flaresolverr(mirror_url)

                    if not success:
                        d.logger.error("FlareSolverr failed")
                        return None

                    # FlareSolverr solved the DDoS-Guard challenge, but the slow_download
                    # page may still show a countdown timer before the actual download link
                    # appears. Handle countdown and extract the link.
                    download_link = _handle_slow_download_page(d, html_content, md5, mirror_url)
                    if download_link:
                        if hasattr(d, 'status_callback'):
                            d.status_callback("Downloading file...")
                        d.logger.info(f"Found download URL from slow_download page, downloading...")
                        return d.download_direct(download_link, title=title, resume_attempts=resume_attempts, md5=md5, subfolder=subfolder)

                    d.logger.warning("Could not find download link on slow_download page")
                    return None

                response.raise_for_status()

                # Handle countdown / extract link from the page
                download_link = _handle_slow_download_page(d, response.text, md5, mirror_url)
                if download_link:
                    if hasattr(d, 'status_callback'):
                        d.status_callback("Downloading file...")
                    d.logger.info(f"Found download URL from slow_download page, downloading...")
                    return d.download_direct(download_link, title=title, resume_attempts=resume_attempts, md5=md5, subfolder=subfolder)

                d.logger.warning("Could not find download link on slow_download page")
                return None

            except Exception as e:
                d.logger.error(f"Error accessing slow_download page: {e}")
                return None

        else:  # external_mirror
            d.logger.debug(f"Accessing external mirror: {mirror_url}")

            # Try to load cached cookies for this mirror
            d.load_cached_cookies(domain=mirror_url)

            try:
                response = d.session.get(mirror_url, timeout=30)

                # If 403, refresh cookies and retry
                if response.status_code == 403:
                    if d.flaresolverr_url:
                        d.logger.warning("Got 403 - trying to refresh cookies")

                        # Try to pre-warm new cookies
                        if d.prewarm_cookies():
                            d.logger.info("Retrying with fresh cookies...")
                            # Retry once with fresh cookies
                            response = d.session.get(mirror_url, timeout=30)

                            if response.status_code == 403:
                                d.logger.warning("Still got 403 after cookie refresh, using FlareSolverr for full solve")
                            else:
                                # Success with fresh cookies, continue to parse
                                response.raise_for_status()

                                if hasattr(d, 'status_callback'):
                                    d.status_callback("Extracting download link...")

                                download_link = d.parse_download_link_from_html(response.text, md5, mirror_url)
                                if not download_link:
                                    d.logger.warning("Could not find download link")
                                    return None

                                if hasattr(d, 'status_callback'):
                                    d.status_callback("Downloading file...")

                                return d.download_direct(download_link, title=title, resume_attempts=resume_attempts, md5=md5, subfolder=subfolder)

                        # If cookie refresh failed or still got 403, use FlareSolverr
                        if hasattr(d, 'status_callback'):
                            d.status_callback("Solving CAPTCHA with FlareSolverr...")
                        success, cookies, html_content = d.solve_with_flaresolverr(mirror_url)

                        if success:
                            if hasattr(d, 'status_callback'):
                                d.status_callback("Extracting download link...")
                            download_link = d.parse_download_link_from_html(html_content, md5, mirror_url)
                            if download_link:
                                if hasattr(d, 'status_callback'):
                                    d.status_callback("Downloading file...")
                                d.logger.info("Found download URL via FlareSolverr, downloading...")
                                return d.download_direct(download_link, title=title, resume_attempts=resume_attempts, md5=md5, subfolder=subfolder)
                        return None
                    else:
                        d.logger.warning("Got 403 but FlareSolverr not configured")
                        return None

                response.raise_for_status()

                if hasattr(d, 'status_callback'):
                    d.status_callback("Extracting download link...")

                download_link = d.parse_download_link_from_html(response.text, md5, mirror_url)
                if not download_link:
                    d.logger.warning("Could not find download link")
                    return None

                if hasattr(d, 'status_callback'):
                    d.status_callback("Downloading file...")

                return d.download_direct(download_link, title=title, resume_attempts=resume_attempts, md5=md5, subfolder=subfolder)

            except Exception as e:
                d.logger.error(f"Error accessing external mirror: {e}")
                return None

    except Exception as e:
        d.logger.error(f"Error downloading from mirror: {e}")
        return None


def _handle_slow_download_page(d, html_content, md5, mirror_url):
    """
    Handle a slow_download page from Anna's Archive.

    The slow_download page can be in one of several states:
    1. Direct download link is already visible (no countdown)
    2. A countdown timer is running (need to wait, then re-fetch)
    3. A "Download now" button/link is visible (countdown just finished)

    This function tries all extraction strategies and handles the countdown
    if one is found.

    Returns:
        Download URL or None
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_content, 'html.parser')
    md5_prefix = md5[:12]

    # --- Strategy 1: Look for the direct link in <span class="bg-gray-200 break-all"> ---
    # This is the primary location where Anna's Archive puts the download URL
    # after the countdown has completed (or immediately for no-countdown servers).
    text_span = soup.find('span', class_='bg-gray-200')
    if text_span:
        # Also check for 'break-all' class (Anna's Archive uses both classes together)
        span_text = text_span.get_text(strip=True)
        if span_text and span_text.startswith('http'):
            d.logger.debug(f"Found download URL in bg-gray-200 span: {span_text}")
            return span_text

    # --- Strategy 2: Look for "Download now" link ---
    # After countdown finishes, a link with text "📚 Download now" appears.
    for a in soup.find_all('a', href=True):
        link_text = a.get_text(strip=True)
        if 'download now' in link_text.lower():
            href = a['href']
            # Resolve relative URLs
            if not href.startswith('http'):
                href = urljoin(mirror_url, href)
            d.logger.debug(f"Found 'Download now' link: {href}")
            return href

    # --- Strategy 3: Try the generic parser ---
    download_link = d.parse_download_link_from_html(html_content, md5, mirror_url)
    if download_link:
        return download_link

    # --- Strategy 4: Try the download panel parser ---
    download_link = _parse_slow_download_panel(d, html_content, md5, mirror_url)
    if download_link:
        return download_link

    # --- Strategy 5: Handle countdown timer ---
    # If there's a countdown, we need to wait for it to expire, then re-fetch the page.
    countdown_el = soup.find(class_='js-partner-countdown')
    if countdown_el:
        countdown_text = countdown_el.get_text(strip=True)
        # Parse the countdown seconds
        try:
            seconds_left = int(re.search(r'\d+', countdown_text).group())
        except (AttributeError, ValueError):
            # If we can't parse the countdown, default to 30 seconds
            seconds_left = 30

        d.logger.info(f"Slow download countdown detected: {seconds_left}s remaining. Waiting...")
        if hasattr(d, 'status_callback'):
            d.status_callback(f"Waiting for download timer: {seconds_left}s...")

        # Wait for the countdown + a small buffer
        wait_time = seconds_left + 3
        time.sleep(wait_time)

        # Re-fetch the page now that the countdown should have expired
        if hasattr(d, 'status_callback'):
            d.status_callback("Fetching download link...")

        try:
            response = d.session.get(mirror_url, timeout=30)
            if response.status_code in [403, 503]:
                d.logger.warning(f"Got {response.status_code} after waiting for countdown, giving up on this mirror")
                return None

            response.raise_for_status()

            # Now try to extract the link from the refreshed page
            fresh_soup = BeautifulSoup(response.text, 'html.parser')

            # Try the bg-gray-200 span again
            text_span = fresh_soup.find('span', class_='bg-gray-200')
            if text_span:
                span_text = text_span.get_text(strip=True)
                if span_text and span_text.startswith('http'):
                    d.logger.debug(f"Found download URL after countdown: {span_text}")
                    return span_text

            # Try "Download now" link
            for a in fresh_soup.find_all('a', href=True):
                link_text = a.get_text(strip=True)
                if 'download now' in link_text.lower():
                    href = a['href']
                    if not href.startswith('http'):
                        href = urljoin(mirror_url, href)
                    d.logger.debug(f"Found 'Download now' link after countdown: {href}")
                    return href

            # Try generic parser on the fresh page
            download_link = d.parse_download_link_from_html(response.text, md5, mirror_url)
            if download_link:
                return download_link

            # Try panel parser on the fresh page
            download_link = _parse_slow_download_panel(d, response.text, md5, mirror_url)
            if download_link:
                return download_link

            d.logger.warning("Download link still not found after waiting for countdown")

        except Exception as e:
            d.logger.error(f"Error re-fetching slow_download page after countdown: {e}")

    # --- Strategy 6: Last resort — look for any href with the MD5 prefix ---
    # that we might have missed
    for a in soup.find_all('a', href=True):
        href = a['href']
        if md5_prefix in href.lower():
            # Skip navigation links
            if '/md5/' in href.lower() or 'slow_download' in href.lower() or 'fast_download' in href.lower():
                continue
            if not href.startswith('http'):
                href = urljoin(mirror_url, href)
            d.logger.debug(f"Found download link via MD5 prefix fallback: {href}")
            return href

    return None


def _parse_slow_download_panel(d, html_content, md5, mirror_url):
    """
    Parse a slow_download page that contains a download panel (like the md5 page).

    Anna's Archive changed its structure: the slow_download page now shows the same
    download panel as the /md5/ page instead of directly serving the file. This parser
    looks for download links inside that panel structure.

    It tries multiple strategies:
    1. Find links with the MD5 prefix (direct CDN links)
    2. Find clipboard buttons or spans with download URLs
    3. Find js-download-link elements that point to actual file downloads
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_content, 'html.parser')
    md5_prefix = md5[:12]

    # Strategy 1: Look for links in the downloads panel
    panel = soup.find('div', id='md5-panel-downloads')
    if panel:
        # Look for links containing the MD5 that are NOT slow_download or fast_download
        for a in panel.find_all('a', href=True):
            href = a['href']
            if md5_prefix in href.lower():
                # Skip slow_download and fast_download pages — we need the actual file
                if 'slow_download' in href.lower() or 'fast_download' in href.lower():
                    continue
                # Skip navigation links
                if '/md5/' in href.lower():
                    continue

                # Resolve relative URLs
                if not href.startswith('http'):
                    href = urljoin(mirror_url, href)

                d.logger.debug(f"Found download link in panel: {href}")
                return href

    # Strategy 2: Look for js-download-link elements with actual file URLs
    # (not slow_download/fast_download navigation links)
    for a in soup.find_all('a', class_='js-download-link', href=True):
        href = a['href']
        if 'slow_download' in href or 'fast_download' in href:
            continue
        if md5_prefix in href.lower():
            if not href.startswith('http'):
                href = urljoin(mirror_url, href)
            d.logger.debug(f"Found js-download-link: {href}")
            return href

    # Strategy 3: Look for download URLs in any element (clipboard buttons, spans, etc.)
    for btn in soup.find_all('button', onclick=True):
        onclick = btn['onclick']
        match = re.search(r"writeText\('([^']+)'", onclick)
        if match:
            url = match.group(1)
            if md5_prefix in url:
                d.logger.debug(f"Found clipboard URL in panel page: {url}")
                return url

    for span in soup.find_all('span'):
        text = span.get_text(strip=True)
        if text.startswith("http") and md5_prefix in text:
            d.logger.debug(f"Found raw URL in span on panel page: {text}")
            return text

    # Strategy 4: Look for any link with a file extension that might be a direct download
    from stacks.constants import LEGAL_FILES
    for a in soup.find_all('a', href=True):
        href = a['href']
        if any(ext in href.lower() for ext in LEGAL_FILES):
            if not href.startswith('http'):
                href = urljoin(mirror_url, href)
            # Verify it's not just a navigation link
            if md5_prefix in href.lower() or 'cdn' in href.lower():
                d.logger.debug(f"Found file link on panel page: {href}")
                return href

    return None
