#!/usr/bin/env python3
"""The credential a CI job sends to Noru: an API key, or a token from Noru's CI token exchange.

Standard library only. scripts/ci_check.py calls resolve() once per job, the first time a step
needs Noru, and hands the result to the same child processes NORU_API_KEY reaches today.

Two sources, in this order:

  NORU_TRUST_ID  the id of a CI trust record in Noru (Settings -> Developer -> CI trust). The job
                 asks its CI provider for an OIDC ID token for the Noru API's origin, and exchanges
                 it at <origin>/v1/oidc/token (RFC 8693) for a token that lasts 15 minutes and
                 carries only the scopes the piece declares. When this is set it wins over
                 NORU_API_KEY, and the report says the key went unused.
  NORU_API_KEY   a long-lived API key, as before.

The ID token comes from:

  GitHub Actions  ACTIONS_ID_TOKEN_REQUEST_URL and ACTIONS_ID_TOKEN_REQUEST_TOKEN, which the runner
                  sets when the job has `permissions: id-token: write`.
  GitLab CI       NORU_ID_TOKEN, from an `id_tokens` entry whose `aud` is the Noru API's origin.

A job without an ID token (a fork pull request has none) has no credential, exactly as a job
without NORU_API_KEY: the steps that need Noru are skipped, not failed. Each ID token can be
exchanged once, and GitLab mints one per job, so the exchange happens once per job.

Nothing here prints a token. The ID token, the request token and the issued token are kept in
SECRETS so the caller can scrub them from anything it reports.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

TRUST_ID_ENV = "NORU_TRUST_ID"
API_KEY_ENV = "NORU_API_KEY"
API_URL_ENV = "NORU_API_URL"
ID_TOKEN_ENV = "NORU_ID_TOKEN"
GITHUB_REQUEST_URL_ENV = "ACTIONS_ID_TOKEN_REQUEST_URL"
GITHUB_REQUEST_TOKEN_ENV = "ACTIONS_ID_TOKEN_REQUEST_TOKEN"

# Variables that let a process obtain an ID token. No child of ci_check needs one.
ID_TOKEN_SOURCES = (ID_TOKEN_ENV, GITHUB_REQUEST_URL_ENV, GITHUB_REQUEST_TOKEN_ENV)

DEFAULT_BASE_URL = "https://api.noru.tech"
EXCHANGE_PATH = "/v1/oidc/token"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
SUBJECT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:id_token"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 64 * 1024

SECRETS = []


class Unavailable(Exception):
    """This job has no credential. Expected on a fork pull request; the caller skips."""


class Refused(Exception):
    """A credential was configured and could not be obtained. The caller reports an error."""


class Credential:
    def __init__(self, source, token, scopes=None, trust_id=None, api_key_unused=False):
        self.source = source
        self.token = token
        self.scopes = scopes
        self.trust_id = trust_id
        self.api_key_unused = api_key_unused

    def describe(self):
        """What the report records. Never the token."""
        if self.source == "api_key":
            return {"source": "api_key"}
        return {
            "source": "ci_token_exchange",
            "trust_id": self.trust_id,
            "scopes": self.scopes,
            "api_key_unused": self.api_key_unused,
        }


def _remember(secret):
    if secret and len(secret) >= 8 and secret not in SECRETS:
        SECRETS.append(secret)


def api_origin(env=None):
    """The Noru API origin, validated the way scripts/mcp_exec.mjs validates NORU_API_URL."""
    env = os.environ if env is None else env
    raw = (env.get(API_URL_ENV) or DEFAULT_BASE_URL).strip()
    parsed = urllib.parse.urlsplit(raw)
    if not parsed.scheme or not parsed.hostname:
        raise Refused(f"{API_URL_ENV}='{raw}' is not a URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise Refused(f"{API_URL_ENV} must be an origin such as {DEFAULT_BASE_URL}, with no path")
    loopback = parsed.hostname in LOOPBACK_HOSTS
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise Refused(f"{API_URL_ENV} must use https (plain http is accepted only for a loopback host)")
    return f"{parsed.scheme}://{parsed.netloc}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A token request is never followed somewhere else: the credential would follow it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirect)


def _read_json(response):
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("response too large")
    return json.loads(body.decode("utf-8"))


def _github_id_token(env, audience):
    url = env.get(GITHUB_REQUEST_URL_ENV)
    request_token = env.get(GITHUB_REQUEST_TOKEN_ENV)
    if not (url and request_token):
        return None
    _remember(request_token)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS):
        raise Refused(f"{GITHUB_REQUEST_URL_ENV} must use https")
    separator = "&" if parsed.query else "?"
    request = urllib.request.Request(
        f"{url}{separator}audience={urllib.parse.quote(audience, safe='')}",
        headers={"Authorization": f"bearer {request_token}", "Accept": "application/json"},
    )
    try:
        with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
            value = _read_json(response).get("value")
    except urllib.error.HTTPError as exc:
        raise Refused(f"GitHub did not issue an ID token (HTTP {exc.code})") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise Refused(f"could not request an ID token from GitHub ({type(exc).__name__})") from None
    if not isinstance(value, str) or not value:
        raise Refused("GitHub's ID token response had no token")
    return value


def _exchange(origin, trust_id, id_token, scopes):
    form = {
        "grant_type": GRANT_TYPE,
        "subject_token": id_token,
        "subject_token_type": SUBJECT_TOKEN_TYPE,
        "audience": trust_id,
    }
    if scopes:
        form["scope"] = " ".join(scopes)
    request = urllib.request.Request(
        f"{origin}{EXCHANGE_PATH}",
        data=urllib.parse.urlencode(form).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
            body = _read_json(response)
    except urllib.error.HTTPError as exc:
        try:
            refusal = _read_json(exc)
        except (OSError, ValueError):
            refusal = {}
        code = refusal.get("error") if isinstance(refusal.get("error"), str) else f"HTTP {exc.code}"
        description = refusal.get("error_description")
        detail = f"{code}: {description}" if isinstance(description, str) and description else code
        raise Refused(f"Noru refused the CI token exchange ({detail})") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise Refused(f"could not reach Noru's CI token exchange ({type(exc).__name__})") from None
    token = body.get("access_token")
    if not isinstance(token, str) or not token or str(body.get("token_type", "")).lower() != "bearer":
        raise Refused("Noru's CI token exchange answered without a bearer token")
    _remember(token)
    granted = body.get("scope")
    return token, granted.split() if isinstance(granted, str) and granted else list(scopes)


def resolve(scopes, env=None):
    """The job's credential. Raises Unavailable when it has none, Refused when one fails."""
    env = os.environ if env is None else env
    trust_id = (env.get(TRUST_ID_ENV) or "").strip()
    api_key = env.get(API_KEY_ENV)
    if not trust_id:
        if api_key:
            return Credential("api_key", api_key)
        raise Unavailable(
            f"neither {TRUST_ID_ENV} nor {API_KEY_ENV} is set, so this job cannot reach Noru"
        )
    origin = api_origin(env)
    id_token = env.get(ID_TOKEN_ENV) or _github_id_token(env, origin)
    if not id_token:
        raise Unavailable(
            f"{TRUST_ID_ENV} is set but this job has no OIDC ID token (GitHub Actions: "
            f"`permissions: id-token: write`; GitLab CI: an `id_tokens` entry {ID_TOKEN_ENV} with "
            f"`aud: {origin}`)"
        )
    _remember(id_token)
    token, granted = _exchange(origin, trust_id, id_token, sorted(set(scopes)))
    return Credential(
        "ci_token_exchange", token, scopes=granted, trust_id=trust_id, api_key_unused=bool(api_key)
    )
