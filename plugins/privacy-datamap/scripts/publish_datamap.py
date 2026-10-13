"""Publish a complete Fideslang YAML map using Noru's documented REST API."""
import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request

from fides_document import load_document


def read_manifest(path):
    manifest = load_document(Path(path).read_text())
    if not isinstance(manifest, dict):
        raise ValueError("Manifest must be a YAML object")
    if not any(manifest.get(kind) for kind in ("dataset", "system")):
        raise ValueError("Refusing an empty map: publication could archive existing records")
    for kind in ("dataset", "system"):
        entries = manifest.get(kind, [])
        if not isinstance(entries, list):
            raise ValueError(f"{kind} must be a list")
        keys = set()
        for entry in entries:
            key = entry.get("fides_key") if isinstance(entry, dict) else None
            if not isinstance(key, str) or not key.strip() or key in keys:
                raise ValueError(f"{kind} entries require unique, nonempty fides_key strings")
            keys.add(key)
    # Reject YAML dates, nonfinite numbers, aliases with cycles, and other non-JSON values.
    json.dumps(manifest, allow_nan=False)
    return manifest


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def publish(payload, base_url, token):
    url = urllib.parse.urlsplit(base_url)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("NORU_API_BASE_URL must be an HTTPS base URL without credentials, query or fragment")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/privacy/datamaps",
        data=json.dumps(payload, allow_nan=False).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    # Never forward the API key through redirects or automatically replay uncertain writes.
    with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
        result = json.load(response)
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("unchanged"), bool):
        raise ValueError("Unexpected API response; check Noru before retrying")
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", default=".fides/datamap.yml")
    parser.add_argument("--publish", action="store_true", help="Replace the map for NORU_SOURCE_SLUG")
    parser.add_argument("--branch", default=os.environ.get("NORU_SOURCE_BRANCH"))
    parser.add_argument("--commit-sha", default=os.environ.get("NORU_SOURCE_COMMIT_SHA"))
    args = parser.parse_args(argv)
    try:
        manifest = read_manifest(args.file)
        if not args.publish:
            print("Local YAML/JSON shape checks passed. Taxonomy and full Fides schema are not validated by this check.")
            return 0
        required = ("NORU_API_BASE_URL", "NORU_API_KEY", "NORU_SOURCE_SLUG")
        if any(not os.environ.get(key, "").strip() for key in required):
            raise ValueError("Set NORU_API_BASE_URL, NORU_API_KEY and NORU_SOURCE_SLUG before publishing")
        payload = {"slug": os.environ["NORU_SOURCE_SLUG"], "manifest": manifest}
        for field, value in (("branch", args.branch), ("commitSha", args.commit_sha),
                             ("name", os.environ.get("NORU_SOURCE_NAME"))):
            if value:
                payload[field] = value
        data = publish(payload, os.environ["NORU_API_BASE_URL"], os.environ["NORU_API_KEY"])
        print("Map unchanged." if data["unchanged"] else "Map published.")
        if data.get("warnings"):
            print("Publication returned taxonomy warnings. The write already occurred; inspect the source in Noru.", file=sys.stderr)
            return 1
        return 0
    except urllib.error.HTTPError as error:
        print(f"Noru returned HTTP {error.code}. Response body omitted to protect customer data.", file=sys.stderr)
    except (OSError, ValueError, TypeError, RecursionError):
        print("Failed: check file shape, environment settings and connectivity. If publication started, verify Noru before retrying.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
