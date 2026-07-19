import json
import logging
from stacks.constants import ANNAS_ARCHIVE_DOMAINS, DOMAIN_STATE_FILE, CONFIG_PATH

logger = logging.getLogger(__name__)


def get_all_domains():
    """Get all Anna's Archive domains: hardcoded list plus any extras found via Wikipedia."""
    from stacks.utils.domainupdater import get_wiki_mirrors
    extras = get_wiki_mirrors()
    return list(dict.fromkeys(ANNAS_ARCHIVE_DOMAINS + [d for d in extras if d not in ANNAS_ARCHIVE_DOMAINS]))


def get_working_domain():
    """Get the last known working Anna's Archive domain, or the first one if none saved."""
    all_domains = get_all_domains()
    try:
        if DOMAIN_STATE_FILE.exists():
            with open(DOMAIN_STATE_FILE, 'r') as f:
                data = json.load(f)
                domain = data.get('last_working_domain')
                if domain and domain in all_domains:
                    logger.debug(f"Using last known working domain: {domain}")
                    return domain
    except Exception as e:
        logger.debug(f"Failed to load working domain: {e}")

    # Default to first domain
    logger.debug(f"Using default domain: {all_domains[0]}")
    return all_domains[0]


def save_working_domain(domain):
    """Save the last known working domain to state file."""
    try:
        CONFIG_PATH.mkdir(parents=True, exist_ok=True)
        with open(DOMAIN_STATE_FILE, 'w') as f:
            json.dump({'last_working_domain': domain}, f)
        logger.info(f"Saved working domain: {domain}")
    except Exception as e:
        logger.debug(f"Failed to save working domain: {e}")


def get_next_domain(current_domain):
    """Get the next domain in the rotation after the current one."""
    all_domains = get_all_domains()
    try:
        current_index = all_domains.index(current_domain)
        next_index = (current_index + 1) % len(all_domains)
        next_domain = all_domains[next_index]
        logger.debug(f"Rotating from {current_domain} to {next_domain}")
        return next_domain
    except (ValueError, IndexError):
        logger.debug(f"Invalid current domain {current_domain}, using default")
        return all_domains[0]


def try_domains_until_success(func, *args, **kwargs):
    """
    Try a function with different Anna's Archive domains until one succeeds.

    The function should accept a 'domain' parameter.
    This will try the last working domain first, then rotate through all others.
    When successful, it saves the working domain for future use.

    Args:
        func: Function to call that accepts a 'domain' parameter
        *args: Positional arguments to pass to func
        **kwargs: Keyword arguments to pass to func (domain will be added/overridden)

    Returns:
        The result of the successful function call

    Raises:
        The last exception encountered if all domains fail
    """
    all_domains = get_all_domains()

    # Start with the last working domain
    current_domain = get_working_domain()
    tried_domains = []
    last_error = None

    # Try all domains
    for _ in range(len(all_domains)):
        if current_domain in tried_domains:
            current_domain = get_next_domain(current_domain)
            continue

        tried_domains.append(current_domain)
        logger.debug(f"Trying domain: {current_domain}")

        try:
            # Call the function with the current domain
            kwargs['domain'] = current_domain
            result = func(*args, **kwargs)

            # Success! Save this domain for future use
            save_working_domain(current_domain)
            logger.info(f"Successfully used domain: {current_domain}")
            return result

        except Exception as e:
            logger.warning(f"Failed with domain {current_domain}: {e}")
            last_error = e
            current_domain = get_next_domain(current_domain)

    # All domains failed
    logger.error(f"All Anna's Archive domains failed. Last error: {last_error}")
    raise last_error if last_error else Exception("All domains failed")
