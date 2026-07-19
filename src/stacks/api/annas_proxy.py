import logging
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Response, current_app, redirect, request, url_for

from . import api_bp
from stacks.constants import TIMESTAMP
from stacks.downloader.cookies import _load_cached_cookies, _save_cookies_to_cache
from stacks.downloader.flaresolver import solve_with_flaresolverr
from stacks.security.auth import require_login
from stacks.utils.domainutils import get_all_domains, get_working_domain, save_working_domain

logger = logging.getLogger("stacks.annas_proxy")

PROXY_REQUEST_HEADERS = (
    "Accept",
    "Accept-Language",
    "Cache-Control",
    "Content-Type",
    "Origin",
    "Pragma",
    "Range",
    "Sec-CH-UA",
    "Sec-CH-UA-Mobile",
    "Sec-CH-UA-Platform",
    "Sec-Fetch-Dest",
    "Sec-Fetch-Mode",
    "Sec-Fetch-Site",
    "Upgrade-Insecure-Requests",
)

PROXY_RESPONSE_HEADERS = (
    "Accept-Ranges",
    "Cache-Control",
    "Content-Disposition",
    "Content-Language",
    "Content-Range",
    "Content-Type",
    "ETag",
    "Expires",
    "Last-Modified",
    "Vary",
)

HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
PROTECTION_MARKERS = ("ddos-guard", "just a moment", "cf-chl", "cf-browser-verification")
PROXY_DOMAIN_PARAM = "__aa_domain"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class _ProxySessionContext:
    def __init__(self, config):
        self.logger = logger
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

        proxy_enabled = config.get("proxy", "enabled", default=False)
        proxy_url = config.get("proxy", "url", default=None)
        if proxy_enabled and proxy_url:
            username = config.get("proxy", "username", default=None)
            password = config.get("proxy", "password", default=None)
            if username and password:
                parsed = urlparse(proxy_url)
                proxy_url = parsed._replace(netloc=f"{username}:{password}@{parsed.netloc}").geturl()
            self.session.proxies = {"http": proxy_url, "https": proxy_url}

        flaresolverr_enabled = config.get("flaresolverr", "enabled", default=False)
        flaresolverr_url = config.get("flaresolverr", "url", default=None)
        if flaresolverr_url and not flaresolverr_url.startswith(("http://", "https://")):
            flaresolverr_url = f"http://{flaresolverr_url}"

        self.flaresolverr_url = flaresolverr_url if flaresolverr_enabled else None
        self.flaresolverr_timeout = config.get("flaresolverr", "timeout", default=60) * 1000

    def load_cached_cookies(self, domain=None):
        return _load_cached_cookies(self, domain)

    def save_cookies_to_cache(self, cookies_dict, domain=None, user_agent=None):
        return _save_cookies_to_cache(self, cookies_dict, domain, user_agent=user_agent)

    def solve_with_flaresolverr(self, url):
        return solve_with_flaresolverr(self, url)

    def close(self):
        self.session.close()


def _build_target_url(domain, proxy_path):
    base_url = f"https://{domain}/"
    target_url = urljoin(base_url, proxy_path or "")
    query_pairs = [(key, value) for key, value in request.args.items(multi=True) if key != PROXY_DOMAIN_PARAM]
    if query_pairs:
        return f"{target_url}?{urlencode(query_pairs, doseq=True)}"
    return target_url


def _extract_cookie_values(response):
    return {cookie.name: cookie.value for cookie in response.cookies}


def _build_solved_html_response(target_url, html_content):
    response = requests.Response()
    response.status_code = 200
    response.url = target_url
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    response._content = html_content.encode("utf-8")
    response.encoding = "utf-8"
    return response


def _looks_like_protection(response):
    content_type = response.headers.get("Content-Type", "").lower()
    if response.status_code in (403, 429, 503):
        return True
    if not any(kind in content_type for kind in HTML_CONTENT_TYPES):
        return False
    text = response.text[:4000].lower()
    return any(marker in text for marker in PROTECTION_MARKERS)


def _build_forward_headers():
    headers = {}
    for key in PROXY_REQUEST_HEADERS:
        value = request.headers.get(key)
        if value:
            headers[key] = value
    # Let requests negotiate encodings it can transparently decode.
    # Forwarding browser values like "br" / "zstd" can produce undecoded
    # bytes that then get treated as HTML and rendered as gibberish.
    headers.pop("Accept-Encoding", None)
    return headers


def _fetch_remote_response(proxy_path, domain):
    config = current_app.stacks_config
    proxy_context = _ProxySessionContext(config)
    target_url = _build_target_url(domain, proxy_path)

    try:
        proxy_context.load_cached_cookies(domain)

        response = proxy_context.session.request(
            method=request.method,
            url=target_url,
            headers=_build_forward_headers(),
            data=request.get_data() if request.method in {"POST", "PUT", "PATCH"} else None,
            allow_redirects=True,
            timeout=(5, 30),
        )

        if _looks_like_protection(response) and request.method == "GET" and proxy_context.flaresolverr_url:
            solved, cookies, html_content = proxy_context.solve_with_flaresolverr(target_url)
            if solved and html_content:
                response = _build_solved_html_response(target_url, html_content)
            elif solved and cookies:
                response = proxy_context.session.request(
                    method=request.method,
                    url=target_url,
                    headers=_build_forward_headers(),
                    data=request.get_data() if request.method in {"POST", "PUT", "PATCH"} else None,
                    allow_redirects=True,
                    timeout=(5, 30),
                )

        if _looks_like_protection(response):
            raise Exception(f"Blocked by remote protection on {domain}")

        if response.status_code >= 500:
            raise Exception(f"Remote server error {response.status_code} on {domain}")

        cookies = _extract_cookie_values(response)
        if cookies:
            proxy_context.save_cookies_to_cache(cookies, domain=response.url)

        return response, domain
    finally:
        proxy_context.close()


def _rewrite_proxy_url(raw_url, current_domain, known_domains):
    if not raw_url:
        return raw_url

    stripped = raw_url.strip()
    if not stripped or stripped.startswith(("#", "data:", "javascript:", "mailto:", "tel:")):
        return raw_url

    resolved = urljoin(f"https://{current_domain}/", stripped)
    parsed = urlparse(resolved)

    if parsed.scheme not in ("http", "https"):
        return raw_url

    if parsed.netloc not in known_domains:
        return raw_url

    params = list(parse_qsl(parsed.query, keep_blank_values=True))
    params.append((PROXY_DOMAIN_PARAM, current_domain))
    path = parsed.path.lstrip("/")
    proxy_url = url_for("api.annas_proxy_path", proxy_path=path) if path else url_for("api.annas_proxy_root")
    if params:
        proxy_url = f"{proxy_url}?{urlencode(params, doseq=True)}"
    if parsed.fragment:
        proxy_url = f"{proxy_url}#{parsed.fragment}"
    return proxy_url


def _rewrite_srcset(value, current_domain, known_domains):
    rewritten_parts = []
    for part in value.split(","):
        candidate = part.strip()
        if not candidate:
            continue
        segments = candidate.split()
        segments[0] = _rewrite_proxy_url(segments[0], current_domain, known_domains)
        rewritten_parts.append(" ".join(segments))
    return ", ".join(rewritten_parts)


def _inject_proxy_bridge(html, current_domain, remote_url):
    soup = BeautifulSoup(html, "html.parser")
    known_domains = set(get_all_domains())

    for meta in soup.find_all("meta"):
        if meta.get("http-equiv", "").lower() == "content-security-policy":
            meta.decompose()

    for tag in soup.find_all(True):
        for attr in ("href", "src", "action", "poster"):
            if tag.has_attr(attr):
                tag[attr] = _rewrite_proxy_url(tag.get(attr), current_domain, known_domains)
        if tag.has_attr("srcset"):
            tag["srcset"] = _rewrite_srcset(tag["srcset"], current_domain, known_domains)

    head = soup.head or soup.new_tag("head")
    if soup.head is None:
        soup.insert(0, head)

    script_tag = soup.new_tag(
        "script",
        src=url_for("static", filename="script/annas-proxy.js", v=TIMESTAMP),
    )
    script_tag.attrs["defer"] = ""
    head.append(script_tag)

    title = soup.title
    if title and "Stacks" not in title.get_text():
        title.string = f"{title.get_text()} | Stacks"

    body = soup.body
    if body:
        parsed = urlparse(remote_url)
        body["data-stacks-proxy-domain"] = current_domain
        body["data-stacks-proxy-path"] = parsed.path or "/"
        body["data-stacks-proxy-url"] = remote_url

    return str(soup)


def _build_flask_response(remote_response, current_domain):
    content_type = remote_response.headers.get("Content-Type", "")
    body = remote_response.content
    is_html = any(kind in content_type.lower() for kind in HTML_CONTENT_TYPES)

    if is_html:
        body = _inject_proxy_bridge(remote_response.text, current_domain, remote_response.url).encode(
            remote_response.encoding or "utf-8",
            errors="replace",
        )

    response = Response(body, status=remote_response.status_code)
    for header in PROXY_RESPONSE_HEADERS:
        if is_html and header in {"ETag", "Last-Modified", "Accept-Ranges", "Content-Range", "Content-Disposition"}:
            continue
        value = remote_response.headers.get(header)
        if value:
            response.headers[header] = value

    response.headers["X-Stacks-Annas-Proxy"] = current_domain
    response.headers.pop("Content-Security-Policy", None)
    return response


def _fetch_with_domain_rotation(proxy_path):
    preferred_domain = request.args.get(PROXY_DOMAIN_PARAM) or get_working_domain()
    all_domains = get_all_domains()

    ordered_domains = []
    if preferred_domain:
        ordered_domains.append(preferred_domain)
    ordered_domains.extend(domain for domain in all_domains if domain not in ordered_domains)

    last_error = None
    for domain in ordered_domains:
        try:
            remote_response, used_domain = _fetch_remote_response(proxy_path, domain)
            save_working_domain(used_domain)
            logger.info(f"Successfully used domain: {used_domain}")
            return remote_response, used_domain
        except Exception as exc:
            last_error = exc
            logger.warning(f"Failed with domain {domain}: {exc}")

    raise last_error if last_error else Exception("All Anna's Archive domains failed")


@api_bp.route("/aa")
@require_login
def annas_proxy_home():
    return redirect(url_for("api.annas_proxy_root"))


@api_bp.route("/aa/", methods=["GET", "POST"])
@require_login
def annas_proxy_root():
    remote_response, domain = _fetch_with_domain_rotation("")
    return _build_flask_response(remote_response, domain)


@api_bp.route("/aa/<path:proxy_path>", methods=["GET", "POST"])
@require_login
def annas_proxy_path(proxy_path):
    remote_response, domain = _fetch_with_domain_rotation(proxy_path)
    return _build_flask_response(remote_response, domain)
