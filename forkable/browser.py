"""Import the Forkable session cookie from a browser on macOS.

Chromium browsers (Chrome, Brave, Edge, Arc, Chromium):
  * SQLite DB at ~/Library/Application Support/<browser>/<profile>/Cookies
  * Values are AES-128-CBC encrypted ("v10" prefix). Key = PBKDF2-HMAC-SHA1(password,
    salt="saltysalt", 1003 iterations, 16 bytes), IV = 16 spaces. The password is the
    "<Browser> Safe Storage" Keychain item, so macOS asks you to allow access once.
  * From DB schema version 24 the plaintext starts with SHA256(host_key) (32 bytes).
  * AES isn't in the standard library, so decryption shells out to macOS's built-in `openssl`.

Firefox: plain SQLite (~/Library/Application Support/Firefox/Profiles/*/cookies.sqlite),
  values unencrypted, so no Keychain prompt.

Safari: Cookies.binarycookies inside Safari's sandbox container, unencrypted, but macOS only lets
  apps with Full Disk Access read it. Without it, reading fails with SafariNoAccess (no prompt).

Finding cookies never touches the Keychain; only decrypt() on a Chromium cookie does. Only the
forkable.com session cookie is read.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from operator import attrgetter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .client import SESSION_COOKIE, ForkableError

SUPPORT = Path.home() / "Library" / "Application Support"
HOSTS = ("forkable.com", ".forkable.com", "www.forkable.com")

# name -> (user data dir, Keychain service, Keychain account)
CHROMIUM = {
    "chrome": (SUPPORT / "Google" / "Chrome", "Chrome Safe Storage", "Chrome"),
    "brave": (SUPPORT / "BraveSoftware" / "Brave-Browser", "Brave Safe Storage", "Brave"),
    "edge": (SUPPORT / "Microsoft Edge", "Microsoft Edge Safe Storage", "Microsoft Edge"),
    "arc": (SUPPORT / "Arc" / "User Data", "Arc Safe Storage", "Arc"),
    "chromium": (SUPPORT / "Chromium", "Chromium Safe Storage", "Chromium"),
}
FIREFOX = SUPPORT / "Firefox"
SAFARI_COOKIES = [
    Path.home() / "Library" / "Containers" / "com.apple.Safari" / "Data" / "Library" / "Cookies"
    / "Cookies.binarycookies",
    Path.home() / "Library" / "Cookies" / "Cookies.binarycookies",  # Safari before 13
]
BROWSERS = [*CHROMIUM, "firefox", "safari"]
DISPLAY = {"chrome": "Chrome", "edge": "Microsoft Edge", "arc": "Arc", "brave": "Brave", "chromium": "Chromium",
           "firefox": "Firefox", "safari": "Safari"}

CHROME_EPOCH = 11644473600  # seconds from 1601-01-01 to 1970-01-01
MAC_EPOCH = 978307200       # seconds from 1970-01-01 to 2001-01-01


class SafariNoAccess(ForkableError):
    """macOS blocks reading Safari's cookies unless the terminal has Full Disk Access."""


@dataclass
class FoundCookie:
    browser: str
    profile_name: str
    last_access: float  # Unix seconds (Safari: creation time)
    value: Optional[str] = None  # None until decrypt() for Chromium browsers
    encrypted: Optional[Tuple[bytes, str, int]] = field(default=None, repr=False)  # (blob, host, db version)

    @property
    def needs_keychain(self) -> bool:
        return self.value is None


def keychain_item(browser: str) -> str:
    return CHROMIUM[browser][1]


def installed_browsers() -> List[str]:
    """Supported browsers present on this Mac. Safari is always listed."""
    if sys.platform != "darwin":
        return []
    found = [name for name, (path, _, _) in CHROMIUM.items() if path.exists()]
    if (FIREFOX / "profiles.ini").exists() or (FIREFOX / "Profiles").exists():
        found.append("firefox")
    return [*found, "safari"]


def find_session_cookies(browser: str, profile: Optional[str] = None) -> List[FoundCookie]:
    """forkable.com session cookies in one browser (every profile, or one), most recently used first.

    Never prompts. Chromium values stay encrypted until decrypt(). Raises SafariNoAccess for Safari
    without Full Disk Access.
    """
    if sys.platform != "darwin":
        raise ForkableError("Browser import currently supports macOS only; use `forkable set-session`.")
    if browser not in BROWSERS:
        raise ForkableError(f"Unknown browser {browser!r}; choose from {', '.join(BROWSERS)}")
    if browser == "safari":
        found = _safari_cookies()
    else:
        profiles = _firefox_profiles() if browser == "firefox" else _chromium_profiles(CHROMIUM[browser][0])
        if profile:
            profiles = [p for p in profiles if profile in p[:2]]
        if not profiles:
            raise ForkableError(f"No {DISPLAY[browser]} profile{f' {profile!r}' if profile else 's'} found")
        read = _firefox_cookies if browser == "firefox" else _chromium_cookies
        found = [c for d, name, db in profiles for c in read(browser, name, db)]
    return sorted(found, key=attrgetter("last_access"), reverse=True)


_keys: Dict[str, bytes] = {}


def decrypt(cookie: FoundCookie) -> str:
    """Fill in a Chromium cookie's value. The first call per browser asks the Keychain."""
    if cookie.value is None:
        if cookie.browser not in _keys:
            _, service, account = CHROMIUM[cookie.browser]
            _keys[cookie.browser] = hashlib.pbkdf2_hmac("sha1", _keychain_password(service, account),
                                                        b"saltysalt", 1003, 16)
        cookie.value = _decrypt(*cookie.encrypted, key=_keys[cookie.browser])
    return cookie.value


# --- Chromium ---

def _chromium_profiles(user_data: Path) -> List[tuple]:
    """[(dir name, display name, cookie DB path)] for profiles that have a cookie DB."""
    names = {}
    try:
        state = json.loads((user_data / "Local State").read_text())
        names = {k: v.get("name") or k for k, v in state["profile"]["info_cache"].items()}
    except (OSError, ValueError, KeyError):
        pass
    dirs = sorted(p for p in user_data.iterdir() if p.is_dir()) if user_data.exists() else []
    found = []
    for p in dirs:
        db = next((p / rel for rel in ("Cookies", "Network/Cookies") if (p / rel).exists()), None)
        if db:
            found.append((p.name, names.get(p.name, p.name), db))
    return found


def _chromium_cookies(browser: str, profile_name: str, src: Path) -> List[FoundCookie]:
    with _copied(src) as db:
        con = sqlite3.connect(db)
        try:
            version = int((con.execute("select value from meta where key='version'").fetchone() or [0])[0])
            rows = con.execute("select host_key, encrypted_value, last_access_utc from cookies "
                               f"where name = ? and host_key in {HOSTS}", (SESSION_COOKIE,)).fetchall()
        finally:
            con.close()
    return [FoundCookie(browser, profile_name, last / 1e6 - CHROME_EPOCH, encrypted=(blob, host, version))
            for host, blob, last in rows]


def _keychain_password(service: str, account: str) -> bytes:
    try:
        out = subprocess.run(["security", "find-generic-password", "-w", "-s", service, "-a", account],
                             capture_output=True, check=True, timeout=120)
    except subprocess.CalledProcessError as e:
        raise ForkableError(f"Couldn't read '{service}' from the Keychain (access denied or not found)",
                            e.stderr.decode()) from e
    except subprocess.TimeoutExpired as e:
        raise ForkableError("Timed out waiting for Keychain access") from e
    return out.stdout.strip()


def _decrypt(blob: bytes, host_key: str, db_version: int, key: bytes) -> str:
    if blob[:3] != b"v10":
        raise ForkableError(f"Unsupported cookie encryption ({blob[:3]!r})")
    proc = subprocess.run(["openssl", "enc", "-d", "-aes-128-cbc", "-K", key.hex(), "-iv", (b" " * 16).hex()],
                          input=blob[3:], capture_output=True)
    if proc.returncode != 0:
        raise ForkableError("Cookie decryption failed (wrong Keychain key?)", proc.stderr.decode())
    plain = proc.stdout
    if db_version >= 24 and plain[:32] == hashlib.sha256(host_key.encode()).digest():
        plain = plain[32:]
    return plain.decode()


# --- Firefox ---

def _firefox_profiles() -> List[tuple]:
    """[(dir name, profile name, cookies.sqlite path)] from profiles.ini, else every Profiles/* dir."""
    ini = configparser.ConfigParser()
    ini.read(FIREFOX / "profiles.ini")
    found = []
    for sec in ini.sections():
        if sec.startswith("Profile") and ini.has_option(sec, "Path"):
            path = ini[sec]["Path"]
            p = FIREFOX / path if ini[sec].get("IsRelative", "1") == "1" else Path(path)
            found.append((p.name, ini[sec].get("Name", p.name), p / "cookies.sqlite"))
    if not found and (FIREFOX / "Profiles").exists():
        found = [(p.name, p.name, p / "cookies.sqlite") for p in sorted((FIREFOX / "Profiles").iterdir())]
    return [f for f in found if f[2].exists()]


def _firefox_cookies(browser: str, profile_name: str, src: Path) -> List[FoundCookie]:
    with _copied(src, "-wal") as db:  # recent writes may still be in the write-ahead log
        con = sqlite3.connect(db)
        try:
            rows = con.execute(f"select value, lastAccessed from moz_cookies where name = ? and host in {HOSTS}",
                               (SESSION_COOKIE,)).fetchall()
        finally:
            con.close()
    return [FoundCookie(browser, profile_name, last / 1e6, value=value) for value, last in rows]


# --- Safari ---

def _safari_cookies() -> List[FoundCookie]:
    for path in SAFARI_COOKIES:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            continue
        except PermissionError as e:
            raise SafariNoAccess("macOS doesn't let this terminal read Safari's cookies "
                                 "(it needs Full Disk Access).") from e
        return [FoundCookie("safari", "Safari", created, value=value)
                for host, name, value, created in parse_binarycookies(data)
                if name == SESSION_COOKIE and host in HOSTS]
    return []


def parse_binarycookies(data: bytes):
    """Yield (domain, name, value, created Unix time) from a Safari Cookies.binarycookies file."""
    if data[:4] != b"cook":
        raise ForkableError("Not a Safari cookies file")
    (pages,) = struct.unpack(">I", data[4:8])
    sizes = struct.unpack(f">{pages}I", data[8:8 + 4 * pages])
    pos = 8 + 4 * pages
    for size in sizes:
        page, pos = data[pos:pos + size], pos + size
        (count,) = struct.unpack("<I", page[4:8])
        for off in struct.unpack(f"<{count}I", page[8:8 + 4 * count]):
            url_o, name_o, _path_o, value_o = struct.unpack("<4I", page[off + 16:off + 32])
            (created,) = struct.unpack("<d", page[off + 48:off + 56])
            url, name, value = (_cstr(page, off + o) for o in (url_o, name_o, value_o))
            yield url, name, value, created + MAC_EPOCH


def _cstr(buf: bytes, start: int) -> str:
    return buf[start:buf.index(b"\0", start)].decode()


# --- helpers ---

@contextmanager
def _copied(src: Path, *sidecars: str):
    """Copy a (possibly locked, in-use) SQLite DB and its sidecar files to a temp dir."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "cookies.db"
        shutil.copy2(src, db)
        for ext in sidecars:
            side = src.with_name(src.name + ext)
            if side.exists():
                shutil.copy2(side, db.with_name(db.name + ext))
        yield db
