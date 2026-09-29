import random
import uuid

_API_HOST = "beta-api.crunchyroll.com"

class Miscellaneous:
    @staticmethod
    def _normalize_proxy(line: str):
        if not line:
            return None
        line = line.strip()
        if not line:
            return None
        scheme = "http"
        if "://" in line:
            scheme, _, line = line.partition("://")
            scheme = scheme.lower() or "http"
        if "@" in line:
            return f"{scheme}://{line}"
        parts = line.split(":")
        if len(parts) == 4:
            user, pwd, host, port = parts
            return f"{scheme}://{user}:{pwd}@{host}:{port}"
        if len(parts) == 2:
            host, port = parts
            return f"{scheme}://{host}:{port}"
        return None

    @staticmethod
    def load_all_proxies_from(path, proxyless: bool = False):
        if proxyless:
            return []
        try:
            with open(path, encoding="utf-8") as f:
                raw = [line.strip() for line in f if line.strip()]
        except FileNotFoundError:
            return []
        seen, out = set(), []
        for line in raw:
            p = Miscellaneous._normalize_proxy(line)
            if p and p not in seen:
                seen.add(p)
                out.append(p)
        return out

    @staticmethod
    def randomize_user_agent(ua_version: str) -> str:
        android_version = str(random.randint(13, 16))
        return (
            f"Crunchyroll/ANDROIDTV/{ua_version} "
            f"(Android {android_version}; en-US; sdk_gphone64_x86_64)"
        )

    @staticmethod
    def build_headers(basic_auth: str, ua_version: str):
        return {
            "authorization": basic_auth,
            "connection": "Keep-Alive",
            "content-type": "application/x-www-form-urlencoded",
            "etp-anonymous-id": str(uuid.uuid4()),
            "host": _API_HOST,
            "user-agent": Miscellaneous.randomize_user_agent(ua_version),
            "x-datadog-sampling-priority": "0",
        }