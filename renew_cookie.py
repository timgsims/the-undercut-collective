#!/usr/bin/env python3
"""Renew secrets/f1_cookie.txt without a browser, so a pasted cookie lasts
~30 days instead of 96 hours.

How (found 2026-10-22, same trick as the Home Assistant f1_sensor F1TV
renewal, github.com/Nicxe/f1_sensor PR #698):

  The `login-session` cookie holds a 4-day subscriptionToken JWT. Inside it is
  a `SessionId` claim -- itself a JWT for the underlying F1 account session,
  valid ~30 days from login. While that is alive:

  1. POST api.formula1.com .../Subscriber/RetrieveSubscriber with
     `cd-sessionid: <SessionId>` -> a fresh 4-day subscriptionToken.
     (Not behind the Imperva block that kills password login.)
  2. Put it in `login-session` and POST fantasy.formula1.com
     /services/session/login -> a fresh F1_FANTASY_007 (also 4 days), which
     is the cookie the fantasy API actually checks.

  When SessionId itself expires, a human has to paste a fresh cookie
  (cookie_watch.py warns ahead of race weekends, as before).

Runs from run_pipeline.sh before every fetch, but only renews when
F1_FANTASY_007 has under RENEW_BELOW_HOURS left or the last fetch failed.
The new cookie is checked against the fantasy API before it replaces the old
one; the previous file is kept as f1_cookie.txt.prev.

Writes secrets/renew_status.txt: "OK <iso>" / "SKIP <iso>" / "FAILED <iso> <why>"
/ "EXPIRED <iso>" -- cookie_watch.py reads it.

Exit 0 = cookie is fine (renewed or didn't need it), 1 = transient failure,
2 = SessionId dead, needs a fresh cookie pasted.

  --force   renew even if the cookie isn't near expiry
  --status  print expiries only, change nothing

This file is in a PUBLIC repo. The apikey/system id below are F1's public web
client constants (they ship in every browser); nothing secret goes here.
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fetch_f1_data as F

BASE_DIR = Path(__file__).resolve().parent
COOKIE_PATH = BASE_DIR / "secrets" / "f1_cookie.txt"
PREV_PATH = BASE_DIR / "secrets" / "f1_cookie.txt.prev"
RENEW_STATUS_PATH = BASE_DIR / "secrets" / "renew_status.txt"
FETCH_STATUS_PATH = BASE_DIR / "secrets" / "last_run_status.txt"

RETRIEVE_SUBSCRIBER_URL = "https://api.formula1.com/v1/account/Subscriber/RetrieveSubscriber"
FANTASY_LOGIN_URL = "https://fantasy.formula1.com/services/session/login"
F1_API_KEY = "fCUCjWrKPu9ylJwRAv8BpGLEgiAuThx7"
F1_SYSTEM_ID = "60a9ad84-e93d-480f-80d6-af37494f2e22"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36")
FANTASY_LOGIN_BODY = {"optType": 1, "platformId": 1, "platformVersion": "1",
                      "platformCategory": "web", "clientId": 1}

# Renewing every hourly run would be pointless churn. Under 48h left means the
# cookie always has at least ~2 days in hand, so one failed attempt (F1 API
# blip) never costs any data.
RENEW_BELOW_HOURS = 48


# ---------------------------------------------------------------- parsing

def jwt_claims(token):
    parts = token.split(".")
    if len(parts) != 3:
        return None
    s = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(s))
    except Exception:
        return None


def jwt_exp(token):
    claims = jwt_claims(token) or {}
    exp = claims.get("exp")
    return datetime.fromtimestamp(exp, timezone.utc) if isinstance(exp, (int, float)) else None


def parse_cookie(raw):
    """Ordered [(name, value)] -- order kept so the rewritten file diffs cleanly."""
    pairs = []
    for frag in raw.split(";"):
        frag = frag.strip()
        if "=" in frag:
            name, value = frag.split("=", 1)
            pairs.append((name.strip(), value.strip()))
    return pairs


def subscription_token(pairs):
    """login-session is url-encoded JSON {"data":{"subscriptionToken": <jwt>}}."""
    value = dict(pairs).get("login-session")
    if not value:
        return None
    try:
        # unquote twice: harmless if it was only encoded once
        return json.loads(urllib.parse.unquote(urllib.parse.unquote(value)))["data"]["subscriptionToken"]
    except (ValueError, KeyError, TypeError):
        return None


def session_id(pairs):
    tok = subscription_token(pairs)
    claims = jwt_claims(tok) if tok else None
    sid = (claims or {}).get("SessionId")
    return sid if isinstance(sid, str) and sid.strip() else None


def expiries(pairs):
    """(F1_FANTASY_007 exp, SessionId exp) -- either may be None."""
    sid = session_id(pairs)
    return jwt_exp(dict(pairs).get("F1_FANTASY_007", "")), (jwt_exp(sid) if sid else None)


# ---------------------------------------------------------------- network

def retrieve_subscription_token(sid):
    req = urllib.request.Request(
        RETRIEVE_SUBSCRIBER_URL, data=b"{}", method="POST",
        headers={"User-Agent": UA, "Content-Type": "application/json",
                 "apikey": F1_API_KEY, "CD-SystemId": F1_SYSTEM_ID,
                 "cd-sessionid": sid, "orderSubmitted": "true"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            reply = json.loads(r.read())
        data = reply.get("data") or {}
        # F1 can end the session before the SessionId's own exp: a 200 with
        # {"Fault": {"Code": 109, "Message": "Session expired"}} (seen 2026-10-25,
        # exp said 11-09). Same as a 401: only a fresh pasted cookie fixes it.
        fault = reply.get("Fault") or {}
        if not data and (fault.get("Code") == 109 or "expired" in str(fault.get("Message", "")).lower()):
            return None, "session_rejected"
    except urllib.error.HTTPError as e:
        # 401/403 = the account session itself is dead or revoked
        return None, ("session_rejected" if e.code in (401, 403) else f"HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return None, f"{type(e).__name__}: {e}"
    tok = data.get("subscriptionToken")
    if not isinstance(tok, str) or jwt_exp(tok) is None:
        return None, "no subscriptionToken in response"
    return tok, None


def fantasy_login(pairs):
    """Returns ({cookie name: value} from Set-Cookie, error)."""
    cookie = "; ".join(f"{n}={v}" for n, v in pairs)
    req = urllib.request.Request(
        FANTASY_LOGIN_URL, data=json.dumps(FANTASY_LOGIN_BODY).encode(), method="POST",
        headers={"User-Agent": UA, "Content-Type": "application/json",
                 "Origin": "https://fantasy.formula1.com",
                 "Referer": "https://fantasy.formula1.com/en/", "Cookie": cookie})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            headers = r.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as e:
        return None, f"session/login HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError) as e:
        return None, f"session/login {type(e).__name__}: {e}"
    got = {}
    for h in headers:
        name, _, rest = h.partition("=")
        value = rest.split(";", 1)[0]
        if value and "expires=thu, 01 jan 1970" not in h.lower():
            got[name.strip()] = value
    if "F1_FANTASY_007" not in got:
        return None, "session/login returned no F1_FANTASY_007"
    return got, None


def check_cookie(cookie):
    """Same cheap authenticated call the fetch makes first."""
    me = next(m for m in F.MANAGERS if m[4])
    _, err = F.fetch_json(F.gameday_status_url(f"{me[3]}-0-{me[2]}"), cookie)
    return err


# ---------------------------------------------------------------- main

def write_status(word, detail=""):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    RENEW_STATUS_PATH.write_text(f"{word} {now} {detail}".rstrip() + "\n", encoding="utf-8")
    os.chmod(RENEW_STATUS_PATH, 0o600)


def write_cookie(text):
    tmp = COOKIE_PATH.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")       # no BOM, no trailing newline
    os.chmod(tmp, 0o600)
    if COOKIE_PATH.exists():
        PREV_PATH.write_bytes(COOKIE_PATH.read_bytes())
        os.chmod(PREV_PATH, 0o600)
    os.replace(tmp, COOKIE_PATH)


def fmt(dt):
    return dt.astimezone().strftime("%Y-%m-%d %H:%M %Z") if dt else "unknown"


def main():
    if not COOKIE_PATH.exists():
        print("[renew] no cookie file -- nothing to renew")
        write_status("EXPIRED", "no cookie file")
        return 2
    pairs = parse_cookie(COOKIE_PATH.read_text(encoding="utf-8-sig"))
    now = datetime.now(timezone.utc)
    fantasy_exp, session_exp = expiries(pairs)
    print(f"[renew] F1_FANTASY_007 expires {fmt(fantasy_exp)}; "
          f"account session expires {fmt(session_exp)}")
    if "--status" in sys.argv:
        return 0

    if session_exp is None or session_exp <= now:
        print("[renew] account session is dead -- a fresh cookie must be pasted")
        write_status("EXPIRED", "account session expired")
        return 2

    last_fetch_failed = FETCH_STATUS_PATH.exists() and \
        FETCH_STATUS_PATH.read_text(encoding="utf-8", errors="replace").startswith("FAILED")
    due = (fantasy_exp is None or fantasy_exp - now < timedelta(hours=RENEW_BELOW_HOURS))
    if not (due or last_fetch_failed or "--force" in sys.argv):
        print("[renew] not due")
        write_status("SKIP")
        return 0

    tok, err = retrieve_subscription_token(session_id(pairs))
    if err:
        print(f"[renew] RetrieveSubscriber failed: {err}")
        if err == "session_rejected":
            write_status("EXPIRED", "account session rejected")
            return 2
        write_status("FAILED", err)
        return 1

    login_session = urllib.parse.quote(
        json.dumps({"data": {"subscriptionToken": tok}}, separators=(",", ":")), safe="")
    if dict(pairs)["login-session"].startswith("%25"):    # browser double-encoded it
        login_session = urllib.parse.quote(login_session, safe="")
    pairs = [(n, login_session if n == "login-session" else v) for n, v in pairs]

    got, err = fantasy_login(pairs)
    if err:
        print(f"[renew] {err}")
        write_status("FAILED", err)
        return 1
    names = {n for n, _ in pairs}
    pairs = [(n, got.get(n, v)) for n, v in pairs] + \
            [(n, v) for n, v in got.items() if n not in names]
    new_cookie = "; ".join(f"{n}={v}" for n, v in pairs)

    err = check_cookie(new_cookie)
    if err:
        print(f"[renew] renewed cookie failed the API check ({err}) -- old one kept")
        write_status("FAILED", f"check: {err}")
        return 1

    write_cookie(new_cookie)
    new_exp, _ = expiries(pairs)
    print(f"[renew] renewed: F1_FANTASY_007 now expires {fmt(new_exp)}")
    write_status("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
