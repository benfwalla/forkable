"""Low-level Forkable GraphQL client.

Auth model (reverse engineered from the web app at https://forkable.com/mc/):
  * The session lives in the Rails cookie `_easyorder_session` (HttpOnly, ~6 week
    rolling expiry). That cookie alone is enough for queries.
  * Mutations additionally send `x-csrf-token`, fetched once from GET /api/v2/csrf_token
    using the same cookie.
  * Login: `requestOneTimeCode(email)` (public endpoint) emails a 6-digit code, then
    `createSession(email, otcCode)` returns a session. Password login also exists.
  * GraphQL introspection is disabled server-side; see docs/SCHEMA.md for what is known.

Session lookup order: explicit argument > $FORKABLE_SESSION > ./.env > ~/.config/forkable/session.json
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Dict, Optional

BASE_URL = "https://forkable.com"
GRAPHQL_URL = f"{BASE_URL}/api/v2/graphql"
PUBLIC_GRAPHQL_URL = f"{BASE_URL}/api/v2/public/graphql"
CSRF_URL = f"{BASE_URL}/api/v2/csrf_token"
SESSION_COOKIE = "_easyorder_session"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "forkable"
SESSION_FILE = CONFIG_DIR / "session.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)


class ForkableError(Exception):
    """Raised for transport errors, GraphQL errors, or mutation `errors` payloads."""

    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.details = details


class NotLoggedIn(ForkableError):
    pass


class SsoRequired(ForkableError):
    """The account signs in through SSO; Forkable won't email it a login code."""

    def __init__(self, email: str, provider: Optional[str] = None, login_url: Optional[str] = None):
        super().__init__(f"{email} signs in with {(provider or 'SSO').title()}, "
                         "so Forkable won't email you a login code.")
        self.provider, self.login_url = provider, login_url


def normalize_email(email: str) -> str:
    return email.strip().lower()


def normalize_session(value: Optional[str]) -> Optional[str]:
    """Accept a bare cookie value or a pasted `_easyorder_session=...; ...` string."""
    if not value:
        return value
    if f"{SESSION_COOKIE}=" in value:
        c = SimpleCookie()
        c.load(value.strip())
        if SESSION_COOKIE in c:
            return c[SESSION_COOKIE].value
    return value.strip().strip(";")


def load_dotenv() -> None:
    """Minimal .env loader (no dependency on python-dotenv)."""
    path = Path.cwd() / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def saved_session() -> Optional[str]:
    try:
        return json.loads(SESSION_FILE.read_text()).get("session")
    except (OSError, ValueError):
        return None


def save_session(session: str, email: Optional[str] = None) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.touch(mode=0o600, exist_ok=True)
    SESSION_FILE.chmod(0o600)
    SESSION_FILE.write_text(json.dumps({"session": session, "email": email}))
    return SESSION_FILE


def clear_session() -> None:
    try:
        SESSION_FILE.unlink()
    except FileNotFoundError:
        pass


class GraphQLClient:
    def __init__(self, session_cookie: Optional[str] = None, timeout: float = 30.0):
        self.session_cookie = normalize_session(session_cookie)
        self.timeout = timeout
        self._csrf: Optional[str] = None

    @classmethod
    def from_env(cls) -> "GraphQLClient":
        load_dotenv()
        session = os.environ.get("FORKABLE_SESSION") or saved_session()
        if not session:
            raise NotLoggedIn("Not logged in. Run `forkable login` (or set FORKABLE_SESSION).")
        return cls(session)

    # -- transport -----------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        h = {
            "accept": "application/json",
            "content-type": "application/json",
            "origin": BASE_URL,
            "referer": f"{BASE_URL}/mc/",
            "forkable-referrer": "mc",
            "user-agent": USER_AGENT,
        }
        if self.session_cookie:
            h["cookie"] = f"{SESSION_COOKIE}={self.session_cookie}"
        if self._csrf:
            h["x-csrf-token"] = self._csrf
        return h

    def _request(self, url: str, body: Optional[dict] = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                self._absorb_cookies(resp.headers.get_all("set-cookie") or [])
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode()[:1000]
            if e.code == 401:
                raise NotLoggedIn("Session expired or invalid. Run `forkable login` again.", body_text) from e
            raise ForkableError(f"HTTP {e.code} from {url}", body_text) from e

    def _absorb_cookies(self, set_cookies) -> None:
        # The server issues a session cookie to anonymous visitors and replaces it on login.
        for raw in set_cookies:
            c = SimpleCookie()
            c.load(raw)
            if SESSION_COOKIE in c:
                self.session_cookie = c[SESSION_COOKIE].value

    def csrf_token(self) -> str:
        if not self._csrf:
            self._csrf = self._request(CSRF_URL)["token"]
        return self._csrf

    # -- GraphQL -------------------------------------------------------------------

    def raw(self, query: str, variables: Optional[dict] = None, public: bool = False, csrf: bool = False) -> dict:
        """POST a GraphQL document and return the whole {data, errors} envelope without raising on errors."""
        if csrf:
            self.csrf_token()
        return self._request(PUBLIC_GRAPHQL_URL if public else GRAPHQL_URL,
                             {"query": query, "variables": variables or {}})

    def execute(self, query: str, variables: Optional[dict] = None, public: bool = False,
                csrf: bool = False) -> dict:
        """Run a query/mutation and return `data`, raising on GraphQL errors."""
        result = self.raw(query, variables, public, csrf)
        if result.get("errors"):
            msgs = "; ".join(e.get("message", str(e)) for e in result["errors"])
            raise ForkableError(f"GraphQL error: {msgs}", result["errors"])
        return result["data"]

    def mutate(self, name: str, input: dict, selection: str = "errors", public: bool = False) -> dict:
        """Call `name(input: $input) { selection }` the same way the web app's $api.mutate does.

        Input type names follow the graphql-ruby convention: addPiece -> AddPieceInput.
        Raises ForkableError if the payload reports `errors` or `errorAttributes` (field errors,
        details on the exception).
        """
        input_type = name[0].upper() + name[1:] + "Input"
        query = f"mutation ($input: {input_type}!) {{ {name}(input: $input) {{ {selection} }} }}"
        payload = self.execute(query, {"input": input}, public=public, csrf=True)[name] or {}
        attrs = payload.get("errorAttributes")
        if isinstance(attrs, str):
            attrs = json.loads(attrs) if attrs.strip() else None
        errs = payload.get("errors")
        if errs or attrs:
            msg = "; ".join(errs) if isinstance(errs, list) else str(errs or attrs)
            raise ForkableError(f"{name} failed: {msg}", attrs or payload.get("errorDetails") or errs)
        return payload

    # -- login ---------------------------------------------------------------------

    def login_methods(self, email: str) -> dict:
        """What Forkable's login page offers this email: {"methods": [...], "sso": [{provider, loginUrl}]}.

        Seen values: "sso", "password". Email codes aren't listed here, and SSO accounts don't
        get them even though requestOneTimeCode reports success.
        """
        q = ("query ($e: String!) { loginMethods(email: $e) { availableMethods } "
             "identities(email: $e) { integration { type provider loginUrl } } }")
        data = self.execute(q, {"e": normalize_email(email)}, public=True)
        sso = [i["integration"] for i in data.get("identities") or [] if (i.get("integration") or {}).get("loginUrl")]
        return {"methods": (data.get("loginMethods") or {}).get("availableMethods") or [], "sso": sso}

    def request_login_code(self, email: str) -> None:
        """Email a 6-digit one-time login code (public endpoint). Raises SsoRequired for SSO accounts."""
        info = self.login_methods(email)
        if "sso" in info["methods"]:
            sso = info["sso"][0] if info["sso"] else {}
            raise SsoRequired(email, sso.get("provider"), sso.get("loginUrl"))
        self.mutate("requestOneTimeCode", {"email": normalize_email(email)},
                    "errors errorAttributes errorDetails", public=True)

    def login_with_code(self, email: str, code: str) -> str:
        """Finish one-time-code login. Returns the new session cookie."""
        return self._create_session({"email": normalize_email(email), "otcCode": code.strip()})

    def login_with_password(self, email: str, password: str, mfa_code: Optional[str] = None) -> str:
        """Email/password login (accounts that have a password). Returns the session cookie."""
        inp: Dict[str, Any] = {"email": normalize_email(email), "password": password}
        if mfa_code:
            inp["mfaCode"] = mfa_code
        return self._create_session(inp)

    def _create_session(self, inp: dict) -> str:
        try:
            payload = self.mutate("createSession", inp, "errors errorAttributes errorDetails user { id email }")
        except ForkableError as e:
            attrs = e.details if isinstance(e.details, dict) else {}
            if attrs.get("mfaCode") or attrs.get("mfa_code"):
                raise ForkableError("Two-factor code required (pass mfa_code / --mfa)", attrs) from e
            raise
        if not payload.get("user") or not self.session_cookie:
            raise ForkableError("Login did not return a session", payload)
        self._csrf = None  # CSRF tokens are bound to the session
        return self.session_cookie
