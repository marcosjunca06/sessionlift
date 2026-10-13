"""Nothing private ships: no names, addresses, hosts, accounts or paths from the setup sessionlift was
extracted from, and nothing that looks like a key or a token. Everything specific to one setup belongs
in the user's config file, never in the repository."""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = re.compile(r"senecta|denpod|marcos|junca|mauro|taleva|mj924|34\.21\.121\.199|34\.86\.164\.31|"
                     r"34\.175\.112\.175|us-east4|senecta-dev|senecta-ops|vm-session-archive|C:\\Users|Users/marco",
                     re.I)
# The repository's own address is the one allowed mention of its owner's account.
ALLOWED = re.compile(r"github\.com/marcosjunca06/sessionlift", re.I)
KEYS = re.compile(r"(sk-ant-[A-Za-z0-9_-]{10,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|"
                  r"github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}|"
                  r"ya29\.[0-9A-Za-z_-]{20,}|[rs]k_live_[0-9A-Za-z]{10,}|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}|"
                  r"-----BEGIN [A-Z ]*PRIVATE KEY-----)")


def tracked() -> list:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True,
                             stdin=subprocess.DEVNULL).stdout
        files = [ROOT / line for line in out.splitlines() if line]
    except (OSError, subprocess.CalledProcessError):  # not a checkout (an sdist): every file there ships
        files = [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts]
    return [p for p in files if p.is_file() and p.resolve() != Path(__file__).resolve()]


class NothingPrivateTest(unittest.TestCase):
    def test_no_private_strings_or_keys(self):
        found = []
        for path in tracked():
            rel = path.relative_to(ROOT).as_posix()
            if PRIVATE.search(rel):
                found.append(f"{rel}: the file name")
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if PRIVATE.search(ALLOWED.sub("", line)) or KEYS.search(line):
                    found.append(f"{rel}:{number}: {line.strip()[:100]}")
        self.assertEqual(found, [])

    def test_the_patterns_catch_what_they_should(self):
        for text in ("ssh senecta-dev", "C:\\Users\\someone", "/home/x/Users/marco", "MARCOS", "34.21.121.199"):
            self.assertTrue(PRIVATE.search(text), text)
        for text in ("sk-ant-" + "a" * 30, "AKIA" + "A" * 16, "ghp_" + "a" * 36, "-----BEGIN OPENSSH PRIVATE KEY-----"):
            self.assertTrue(KEYS.search(text), text)
        for text in ("sessionlift", "the server", "secret-token", "https://claude.ai/code/session_0123456789abcdef"):
            self.assertFalse(PRIVATE.search(text) or KEYS.search(text), text)
        self.assertFalse(PRIVATE.search(ALLOWED.sub("", "pip install git+https://github.com/marcosjunca06/sessionlift")))


if __name__ == "__main__":
    unittest.main()
