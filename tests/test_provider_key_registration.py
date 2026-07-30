"""A provider key is registered in five places, and four of the five fail quietly.

Adding a BYOK provider means touching `shared.py` (three separate collections),
`api_key_pool.py`, `agent_secrets_cloud.py`, `enrich.py` and `SECURITY.md`. Miss
one and nothing raises:

  * `_POOL_API_KEY_BASES` -- the key works, only its `__1`/`__2` rotation slots
    stop being recognised;
  * `_PORTAL_ONLY_KEYS` -- the key can be set by hand in a way the portal then
    cannot manage;
  * `CATALOG_ENV_KEYS` -- `sync-secrets` never pulls it from the portal;
  * `SECURITY.md` -- the published outbound-host table becomes wrong, which is
    the one failure a user can see and we cannot.

So this file checks the registrations agree with each other rather than
checking any single one, and it is parameterised over every provider rather
than hardcoding the newest -- the next provider added inherits the coverage.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "outreachmagic" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agent_secrets_cloud  # noqa: E402
import api_key_pool  # noqa: E402
import shared  # noqa: E402

PROVIDER_ENV_KEYS = [p["env_key"] for p in api_key_pool.API_KEY_PROVIDERS]

# Where each provider's traffic actually goes, for the SECURITY.md check below.
PROVIDER_HOSTS = {
    "SERPER_API_KEY": "google.serper.dev",
    "TRYKITT_API_KEY": "api.trykitt.ai",
    "ICYPEAS_API_KEY": "app.icypeas.com",
    "MILLIONVERIFIER_API_KEY": "api.millionverifier.com",
    "SCRUBBY_API_KEY": "api.scrubby.io",
    "FIRECRAWL_API_KEY": "api.firecrawl.dev",
}


@pytest.mark.parametrize("env_key", PROVIDER_ENV_KEYS)
def test_a_pooled_provider_is_in_all_three_shared_collections(env_key):
    assert env_key in shared._API_KEY_VARS
    assert env_key in shared._POOL_API_KEY_BASES
    assert env_key in shared._PORTAL_ONLY_KEYS


@pytest.mark.parametrize("env_key", PROVIDER_ENV_KEYS)
def test_rotation_slots_are_recognised(env_key):
    """The specific symptom of a missing _POOL_API_KEY_BASES entry."""
    assert shared._is_pooled_api_key_var(f"{env_key}__1")
    assert shared._is_pooled_api_key_var(f"{env_key}__2")


@pytest.mark.parametrize("env_key", PROVIDER_ENV_KEYS)
def test_sync_secrets_pulls_every_provider_from_the_portal(env_key):
    assert env_key in agent_secrets_cloud.CATALOG_ENV_KEYS


def test_provider_ids_are_unique():
    ids = [p["provider"] for p in api_key_pool.API_KEY_PROVIDERS]
    assert len(ids) == len(set(ids))


def test_firecrawl_provider_id_matches_the_portal():
    """Cross-repo contract: wbhk-app's ENV_KEY_TO_RUNTIME_PROVIDER maps
    FIRECRAWL_API_KEY to this string. On a mismatch runtimeSlotForEnvKey()
    returns null and the health chip renders nothing -- no error anywhere."""
    entry = next(p for p in api_key_pool.API_KEY_PROVIDERS
                 if p["env_key"] == "FIRECRAWL_API_KEY")
    assert entry["provider"] == "firecrawl"


@pytest.mark.parametrize("env_key", PROVIDER_ENV_KEYS)
def test_every_provider_host_is_published_in_security_md(env_key):
    """SECURITY.md publishes an enumerable outbound-host table. A network
    destination the code reaches and the document omits makes it wrong."""
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    host = PROVIDER_HOSTS[env_key]
    assert f"`{host}`" in text, f"{host} is missing from the SECURITY.md host table"


def test_the_host_map_here_covers_every_registered_provider():
    """Guards this file against going stale when a provider is added."""
    assert set(PROVIDER_ENV_KEYS) <= set(PROVIDER_HOSTS)


def test_the_firecrawl_endpoint_default_is_allowlisted():
    import enrich

    cfg = enrich.load_config()
    assert cfg["firecrawl_endpoint"] == "https://api.firecrawl.dev/v1/scrape"


@pytest.mark.parametrize("bad", [
    "https://api.evil.test/v1/scrape",
    "http://169.254.169.254/latest/meta-data",
    "https://api.firecrawl.dev.evil.test/v1/scrape",
])
def test_a_firecrawl_endpoint_override_off_the_allowlist_is_rejected(bad):
    """The override is the SSRF path -- the default is a constant and trusted."""
    import shared as cc

    with pytest.raises(ValueError):
        cc.validate_endpoint_url(bad, allowed_host_suffixes=["firecrawl.dev"])


def test_a_firecrawl_subdomain_override_is_allowed():
    import shared as cc

    url = "https://eu.api.firecrawl.dev/v1/scrape"
    assert cc.validate_endpoint_url(url, allowed_host_suffixes=["firecrawl.dev"]) == url
