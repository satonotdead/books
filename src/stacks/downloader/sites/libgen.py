"""Libgen (libgen.li) specific scraper.

Libgen.li has a specific HTML structure for download pages:
- The download page at ads.php?md5=<md5> contains a link like:
  <a href="get.php?md5=<md5>&key=<key>"><h2>GET</h2></a>
- The get.php URL is a RELATIVE URL that redirects (307) to a CDN URL
- The key parameter is dynamic and changes per session

We also handle libgen.li/edition/<md5> pages which currently show
"Record ID not specified or incorrect" (they need a different ID format).
"""

from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup


def parse_libgen_download_link(d, html_content, mirror_url):
    """
    Parse libgen.li HTML to extract download link.

    Structure on ads.php?md5= pages:
    - <a href="get.php?md5=<md5>&key=<key>"><h2>GET</h2></a>

    The get.php URL is relative and redirects to a CDN.

    Args:
        d: Downloader instance
        html_content: HTML content from the libgen page
        mirror_url: The original mirror URL

    Returns:
        Download URL or None
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    parsed_url = urlparse(mirror_url)
    base_domain = f"{parsed_url.scheme}://{parsed_url.netloc}"

    md5 = None
    # Extract MD5 from mirror_url query params
    if 'md5=' in mirror_url:
        from urllib.parse import parse_qs, urlparse as _urlparse
        query = parse_qs(_urlparse(mirror_url).query)
        md5 = query.get('md5', [None])[0]

    # Method 1: Look for the "GET" link with get.php in href
    # The classic libgen.li structure: <a href="get.php?md5=...&key=..."><h2>GET</h2></a>
    for a in soup.find_all('a', href=True):
        href = a['href']
        if 'get.php' in href:
            # Resolve relative URL
            full_url = urljoin(base_domain, href)
            d.logger.debug(f"Found libgen get.php download link: {full_url}")
            return full_url

    # Method 2: Look for <h2>GET</h2> inside a link
    for h2 in soup.find_all('h2'):
        if h2.get_text().strip().upper() == 'GET':
            parent_a = h2.find_parent('a', href=True)
            if parent_a:
                href = parent_a['href']
                full_url = urljoin(base_domain, href)
                d.logger.debug(f"Found libgen GET link via h2: {full_url}")
                return full_url

    # Method 3: Look for any link containing the MD5 hash (if we know it)
    if md5:
        for a in soup.find_all('a', href=True):
            href = a['href']
            if md5 in href and ('get.php' in href or 'download' in href.lower()):
                full_url = urljoin(base_domain, href)
                d.logger.debug(f"Found libgen download link via MD5 match: {full_url}")
                return full_url

    # Method 4: Look for links with download-related text
    for a in soup.find_all('a', href=True):
        href = a['href']
        text = a.get_text().strip().lower()
        if text in ['get', 'download'] and ('.php' in href or '/d/' in href):
            full_url = urljoin(base_domain, href)
            d.logger.debug(f"Found libgen download link via text match: {full_url}")
            return full_url

    d.logger.warning("Could not find libgen download link in HTML")
    return None


def is_libgen_domain(url):
    """Check if URL is a libgen domain."""
    libgen_domains = [
        'libgen.li',
        'libgen.rs',
        'libgen.lc',
        'libgen.st',
        'library.lol',
        'lib.rus.ec',
        'gen.lib.rus.ec',
    ]

    parsed = urlparse(url.lower())
    domain = parsed.netloc

    for libgen_domain in libgen_domains:
        if domain == libgen_domain or domain.endswith(f'.{libgen_domain}'):
            return True

    return False
