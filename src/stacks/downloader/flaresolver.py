from urllib.parse import urlparse
from requests import Timeout


def _cookies_for_domain(session, domain):
    cookies = []
    for cookie in session.cookies:
        cookie_domain = cookie.domain.lstrip('.') if cookie.domain else ''
        if cookie_domain and not (domain == cookie_domain or domain.endswith(f".{cookie_domain}")):
            continue
        cookies.append({"name": cookie.name, "value": cookie.value})
    return cookies

def solve_with_flaresolverr(d, url):
    """Use FlareSolverr to bypass DDoS-Guard/Cloudflare protection."""
    if not d.flaresolverr_url:
        return False, {}, None

    d.logger.info("Using FlareSolverr to solve protection challenge...")

    try:
        actual_domain = urlparse(url).netloc.split(':')[0]
        payload = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": d.flaresolverr_timeout,
            "waitInSeconds": 5,
        }
        cookies = _cookies_for_domain(d.session, actual_domain)
        if cookies:
            payload["cookies"] = cookies

        response = d.session.post(
            f"{d.flaresolverr_url}/v1",
            json=payload,
            timeout=d.flaresolverr_timeout / 1000 + 10
        )
        try:
            data = response.json()
        except ValueError:
            data = {}

        if not response.ok:
            error_msg = data.get('message') or response.text or response.reason
            d.logger.error(f"FlareSolverr HTTP {response.status_code}: {error_msg}")
            return False, {}, None
        
        if data.get('status') == 'ok':
            solution = data.get('solution', {})
            cookies_list = solution.get('cookies', [])
            cookies_dict = {cookie['name']: cookie['value'] for cookie in cookies_list}
            html_content = solution.get('response')
            user_agent = solution.get('userAgent')
            
            d.logger.info(f"FlareSolverr: Success - got {len(cookies_dict)} cookies")

            # Apply cookies to session with proper domain
            for name, value in cookies_dict.items():
                d.session.cookies.set(name, value, domain=actual_domain)

            if user_agent:
                d.session.headers.update({'User-Agent': user_agent})
                d.logger.debug("Using FlareSolverr User-Agent for solved cookies")

            # Cache cookies for this domain (for reuse on retry/future downloads)
            d.save_cookies_to_cache(cookies_dict, domain=url, user_agent=user_agent)

            return True, cookies_dict, html_content
        else:
            error_msg = data.get('message', 'Unknown error')
            d.logger.error(f"FlareSolverr failed: {error_msg}")
            return False, {}, None
            
    except Timeout:
        d.logger.error("FlareSolverr timeout")
        return False, {}, None
    except Exception as e:
        d.logger.error(f"FlareSolverr error: {e}")
        return False, {}, None
