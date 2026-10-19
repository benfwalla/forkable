"""Offline tests for browser cookie import (Firefox and Safari readers), using synthetic files."""

import os
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from forkable import browser as B


def binarycookies(cookies):
    """Build a Safari Cookies.binarycookies file from [(domain, name, value, created unix time)]."""
    blobs = []
    for domain, name, value, created in cookies:
        strings, offsets = b"", []
        for s in (domain, name, "/", value):
            offsets.append(56 + len(strings))
            strings += s.encode() + b"\0"
        size = 56 + len(strings)
        blobs.append(struct.pack("<8I", size, 0, 0, 0, *offsets) + b"\0" * 8
                     + struct.pack("<2d", 0.0, created - B.MAC_EPOCH) + strings)
    header_len = 8 + 4 * len(blobs) + 4
    offsets, pos = [], header_len
    for b in blobs:
        offsets.append(pos)
        pos += len(b)
    page = (b"\x00\x00\x01\x00" + struct.pack("<I", len(blobs)) + struct.pack(f"<{len(blobs)}I", *offsets)
            + b"\0\0\0\0" + b"".join(blobs))
    return b"cook" + struct.pack(">I", 1) + struct.pack(">I", len(page)) + page + b"\0" * 8


class SafariTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "Cookies.binarycookies"
        patches = [mock.patch.object(B, "SAFARI_COOKIES", [self.path]),
                   mock.patch.object(B.sys, "platform", "darwin")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        if self.path.exists():
            os.chmod(self.path, 0o600)
        self.tmp.cleanup()

    def test_parse_and_filter(self):
        self.path.write_bytes(binarycookies([
            (".example.com", "_easyorder_session", "nope", 1_700_000_000),
            ("forkable.com", "other", "nope", 1_700_000_000),
            ("forkable.com", "_easyorder_session", "abc%3D%3D--123", 1_760_000_000),
        ]))
        found = B.find_session_cookies("safari")
        self.assertEqual([(c.value, c.browser, c.needs_keychain) for c in found],
                         [("abc%3D%3D--123", "safari", False)])
        self.assertAlmostEqual(found[0].last_access, 1_760_000_000)

    def test_no_full_disk_access(self):
        self.path.write_bytes(b"cook")
        self.path.chmod(0)
        with self.assertRaises(B.SafariNoAccess):
            B.find_session_cookies("safari")

    def test_missing_file(self):
        self.assertEqual(B.find_session_cookies("safari"), [])


class FirefoxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for p in [mock.patch.object(B, "FIREFOX", root), mock.patch.object(B.sys, "platform", "darwin")]:
            p.start()
            self.addCleanup(p.stop)
        (root / "profiles.ini").write_text(
            "[Profile0]\nName=default-release\nIsRelative=1\nPath=Profiles/abc.default-release\n\n"
            "[Profile1]\nName=work\nIsRelative=1\nPath=Profiles/xyz.work\n")
        self.dbs = {}
        for d in ("abc.default-release", "xyz.work"):
            (root / "Profiles" / d).mkdir(parents=True)
            con = sqlite3.connect(root / "Profiles" / d / "cookies.sqlite")
            con.execute("pragma journal_mode=wal")
            con.execute("create table moz_cookies (name, value, host, lastAccessed)")
            self.dbs[d] = con

    def tearDown(self):
        for con in self.dbs.values():
            con.close()
        self.tmp.cleanup()

    def add(self, profile, name, value, host, last):
        self.dbs[profile].execute("insert into moz_cookies values (?, ?, ?, ?)", (name, value, host, last * 10**6))
        self.dbs[profile].commit()  # stays in the -wal file while the connection is open, like a running Firefox

    def test_profiles_newest_first(self):
        self.add("abc.default-release", "_easyorder_session", "old", ".forkable.com", 1_700_000_000)
        self.add("xyz.work", "_easyorder_session", "new", "forkable.com", 1_760_000_000)
        self.add("xyz.work", "_easyorder_session", "nope", "evil.com", 1_770_000_000)
        found = B.find_session_cookies("firefox")
        self.assertEqual([(c.profile_name, c.value) for c in found], [("work", "new"), ("default-release", "old")])
        self.assertFalse(found[0].needs_keychain)

    def test_profile_filter(self):
        self.add("abc.default-release", "_easyorder_session", "old", ".forkable.com", 1_700_000_000)
        self.add("xyz.work", "_easyorder_session", "new", "forkable.com", 1_760_000_000)
        self.assertEqual([c.value for c in B.find_session_cookies("firefox", "default-release")], ["old"])


if __name__ == "__main__":
    unittest.main()
