#!/usr/bin/env python3
"""
fetch-store-versions.py — Read the latest Android (Play internal track) and
iOS (TestFlight) versions for a project we don't build ourselves.

Used by external-notify.yml: an external studio uploads the build, then we
trigger the changelog workflow manually; this script tells the rest of the
pipeline what version was just shipped.

Validates both fetched versions match the canonical X.Y.Z format and fails
the workflow if either doesn't — we never want a malformed version flowing
into the changelog/tag pipeline.

DEPS:
  pip install "pyjwt[crypto]"

INPUTS (env):
  ANDROID_PACKAGE_NAME       e.g. com.colorwood.associations
  ANDROID_TRACK              default "internal"
  GOOGLE_PLAY_JSON_KEY_PATH  path to decoded service-account JSON

  IOS_BUNDLE_ID              e.g. com.colorwood.associations
  APPLE_API_KEY_ID           App Store Connect key id (10 chars)
  APPLE_API_ISSUER_ID        App Store Connect issuer id (UUID)
  APPLE_API_KEY_PATH         path to decoded .p8 key

OUTPUTS (GITHUB_OUTPUT):
  android_version_full       e.g. "1.22.19791"
  android_version_code       e.g. "19791"
  ios_version_full           e.g. "1.22.19791"
  ios_version_code           e.g. "19791"
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import jwt  # PyJWT — provides RS256 (Google) and ES256 (Apple) signing

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
PLAY_API = "https://androidpublisher.googleapis.com/androidpublisher/v3"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/androidpublisher"

ASC_API = "https://api.appstoreconnect.apple.com/v1"
ASC_AUDIENCE = "appstoreconnect-v1"


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helper — stdlib only so we don't need `requests` in the workflow.
# ─────────────────────────────────────────────────────────────────────────────


def http(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    data: dict[str, str] | bytes | None = None,
    timeout: int = 60,
) -> tuple[int, str]:
    h = {"Accept": "application/json"}
    if headers:
        h.update(headers)
    body: bytes | None = None
    if isinstance(data, dict):
        body = urllib.parse.urlencode(data).encode("utf-8")
        h.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif isinstance(data, (bytes, bytearray)):
        body = bytes(data)
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


# ─────────────────────────────────────────────────────────────────────────────
# Google Play
# ─────────────────────────────────────────────────────────────────────────────


def google_access_token(json_key_path: str) -> str:
    with open(json_key_path) as f:
        sa = json.load(f)
    now = int(time.time())
    assertion = jwt.encode(
        {
            "iss": sa["client_email"],
            "scope": GOOGLE_SCOPE,
            "aud": GOOGLE_TOKEN_URL,
            "iat": now,
            "exp": now + 3600,
        },
        sa["private_key"],
        algorithm="RS256",
    )
    status, body = http(
        "POST",
        GOOGLE_TOKEN_URL,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
    )
    if status != 200:
        raise SystemExit(f"Google token exchange failed ({status}): {body}")
    return json.loads(body)["access_token"]


def fetch_android_track(token: str, package: str, track: str) -> dict[str, Any]:
    """Read all releases on a track. Requires creating an Edit first."""
    headers = {"Authorization": f"Bearer {token}"}
    status, body = http(
        "POST", f"{PLAY_API}/applications/{package}/edits", headers=headers
    )
    if status not in (200, 201):
        raise SystemExit(f"Play edit creation failed ({status}): {body}")
    edit_id = json.loads(body)["id"]

    track_url = f"{PLAY_API}/applications/{package}/edits/{edit_id}/tracks/{track}"
    status, body = http("GET", track_url, headers=headers)
    # Best-effort cleanup so we don't leave dangling draft edits.
    http(
        "DELETE",
        f"{PLAY_API}/applications/{package}/edits/{edit_id}",
        headers=headers,
    )
    if status != 200:
        raise SystemExit(f"Play track read failed ({status}): {body}")
    return json.loads(body)


def pick_latest_android(track_data: dict[str, Any]) -> tuple[str, str]:
    """Return (versionName, versionCode) of the release with the highest code."""
    best: tuple[str, int] | None = None
    for r in track_data.get("releases") or []:
        for c in r.get("versionCodes") or []:
            ci = int(c)
            if best is None or ci > best[1]:
                best = (r.get("name") or "", ci)
    if best is None:
        raise SystemExit(f"No releases found on track. Raw payload: {track_data}")
    return best[0], str(best[1])


# ─────────────────────────────────────────────────────────────────────────────
# App Store Connect
# ─────────────────────────────────────────────────────────────────────────────


def asc_token(key_id: str, issuer_id: str, key_path: str) -> str:
    with open(key_path) as f:
        private_key = f.read()
    now = int(time.time())
    return jwt.encode(
        {
            "iss": issuer_id,
            "iat": now,
            "exp": now + 1200,  # 20 min — the max ASC accepts
            "aud": ASC_AUDIENCE,
        },
        private_key,
        algorithm="ES256",
        headers={"kid": key_id},
    )


def fetch_ios_latest(token: str, bundle_id: str) -> tuple[str, str]:
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Resolve app id from bundle id
    qs = urllib.parse.urlencode({"filter[bundleId]": bundle_id})
    status, body = http("GET", f"{ASC_API}/apps?{qs}", headers=headers)
    if status != 200:
        raise SystemExit(f"ASC app lookup failed ({status}): {body}")
    apps = json.loads(body).get("data", [])
    if not apps:
        raise SystemExit(f"No App Store Connect app found for bundle id {bundle_id}")
    app_id = apps[0]["id"]

    # 2. Latest build, include the preReleaseVersion so we get the marketing version.
    qs = urllib.parse.urlencode(
        {
            "filter[app]": app_id,
            "sort": "-uploadedDate",
            "limit": "1",
            "include": "preReleaseVersion",
        }
    )
    status, body = http("GET", f"{ASC_API}/builds?{qs}", headers=headers)
    if status != 200:
        raise SystemExit(f"ASC builds lookup failed ({status}): {body}")
    payload = json.loads(body)
    builds = payload.get("data") or []
    if not builds:
        raise SystemExit(f"No TestFlight builds found for {bundle_id}")
    build = builds[0]
    build_number = build["attributes"]["version"]  # e.g. "19791"

    prv_ref = (
        build.get("relationships", {}).get("preReleaseVersion", {}).get("data") or {}
    )
    prv_id = prv_ref.get("id")
    pre_release_version: str | None = None
    for inc in payload.get("included") or []:
        if inc.get("type") == "preReleaseVersions" and inc.get("id") == prv_id:
            pre_release_version = inc["attributes"]["version"]
            break
    if pre_release_version is None:
        raise SystemExit(
            f"Couldn't resolve preReleaseVersion for build {build['id']}. "
            f"Full payload: {payload}"
        )
    return f"{pre_release_version}.{build_number}", build_number


# ─────────────────────────────────────────────────────────────────────────────
# Outputs / env
# ─────────────────────────────────────────────────────────────────────────────


def gh_output(**values: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        for k, v in values.items():
            print(f"{k}={v}")
        return
    with open(path, "a", encoding="utf-8") as f:
        for k, v in values.items():
            f.write(f"{k}={v}\n")


def require(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        sys.stderr.write(f"ERROR: env {name} required\n")
        raise SystemExit(2)
    return v


def main() -> int:
    package = require("ANDROID_PACKAGE_NAME")
    track = os.environ.get("ANDROID_TRACK", "internal").strip() or "internal"
    google_key = require("GOOGLE_PLAY_JSON_KEY_PATH")
    bundle_id = require("IOS_BUNDLE_ID")
    apple_kid = require("APPLE_API_KEY_ID")
    apple_iss = require("APPLE_API_ISSUER_ID")
    apple_key = require("APPLE_API_KEY_PATH")

    sys.stderr.write(f"Fetching Android: package={package} track={track}\n")
    g_tok = google_access_token(google_key)
    track_data = fetch_android_track(g_tok, package, track)
    a_name, a_code = pick_latest_android(track_data)
    sys.stderr.write(f"  → name={a_name} code={a_code}\n")

    sys.stderr.write(f"Fetching iOS: bundle_id={bundle_id}\n")
    i_tok = asc_token(apple_kid, apple_iss, apple_key)
    i_name, i_code = fetch_ios_latest(i_tok, bundle_id)
    sys.stderr.write(f"  → name={i_name} code={i_code}\n")

    # Strict format check — fail fast rather than push a weird tag downstream.
    errors: list[str] = []
    if not VERSION_RE.match(a_name):
        errors.append(
            f"Android Play release name '{a_name}' doesn't match X.Y.Z. "
            f"The external CI must set bundleVersion correctly."
        )
    if not VERSION_RE.match(i_name):
        errors.append(
            f"iOS composed version '{i_name}' doesn't match X.Y.Z. "
            f"TestFlight preReleaseVersion='{i_name.rsplit('.', 1)[0]}', "
            f"buildNumber='{i_code}'."
        )
    if errors:
        for e in errors:
            sys.stderr.write(f"ERROR: {e}\n")
        return 1

    gh_output(
        android_version_full=a_name,
        android_version_code=a_code,
        ios_version_full=i_name,
        ios_version_code=i_code,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
