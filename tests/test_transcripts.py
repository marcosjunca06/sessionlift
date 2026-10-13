"""Transcripts as files: project slugs, the working-directory rewrite between machines, titles, and the
archives that carry them."""

import json
import tempfile
import unittest
from pathlib import Path

from sessionlift.transcripts import TitleCache, rewrite_cwd, session_title, slug, tail_title, tar_files, untar

from fakes import jl


class SlugTest(unittest.TestCase):
    def test_every_other_character_becomes_a_dash(self):
        self.assertEqual(slug("D:\\code\\demo"), "D--code-demo")
        self.assertEqual(slug("/home/dev/demo"), "-home-dev-demo")
        self.assertEqual(slug("/srv/my.repo_2"), "-srv-my-repo-2")


class RewriteTest(unittest.TestCase):
    def cwds(self, data: bytes) -> list:
        return [json.loads(line)["cwd"] for line in data.splitlines() if b'"cwd"' in line]

    def test_windows_laptop_both_ways(self):
        local, remote = "D:\\code\\demo", "/home/dev/demo"
        data = (jl(cwd="D:\\code\\demo", n=1) + jl(cwd="d:\\CODE\\demo\\web\\app", n=2)
                + jl(cwd="D:\\code\\demo-other", n=3) + jl(cwd="E:\\elsewhere", n=4))
        there = rewrite_cwd(data, local, remote, to_server=True)
        self.assertEqual(self.cwds(there), ["/home/dev/demo", "/home/dev/demo/web/app", "D:\\code\\demo-other",
                                            "E:\\elsewhere"])
        back = rewrite_cwd(there, local, remote, to_server=False)
        self.assertEqual(self.cwds(back), ["D:\\code\\demo", "D:\\code\\demo\\web\\app", "D:\\code\\demo-other",
                                           "E:\\elsewhere"])
        self.assertEqual([json.loads(x)["n"] for x in back.splitlines()], [1, 2, 3, 4], "nothing else changes")

    def test_posix_laptop_is_case_sensitive(self):
        local, remote = "/Users/dev/demo", "/srv/demo"
        data = jl(cwd="/Users/dev/demo/sub") + jl(cwd="/users/dev/demo")
        self.assertEqual(self.cwds(rewrite_cwd(data, local, remote, to_server=True)), ["/srv/demo/sub", "/users/dev/demo"])
        self.assertEqual(self.cwds(rewrite_cwd(jl(cwd="/srv/demo/sub"), local, remote, to_server=False)),
                         ["/Users/dev/demo/sub"])

    def test_spaces_and_unicode_in_paths(self):
        local, remote = "D:\\code\\d\u00e9mo x", "/srv/d\u00e9mo"
        data = jl(cwd=local + "\\a")
        self.assertEqual(self.cwds(rewrite_cwd(data, local, remote, to_server=True)), ["/srv/d\u00e9mo/a"])


class TitleTest(unittest.TestCase):
    def test_rename_wins_over_the_generated_title(self):
        sid = "12345678-aaaa-bbbb-cccc-1234567890ab"
        self.assertEqual(session_title(jl(aiTitle="Generated") + jl(customTitle="Mine"), sid), "Mine")
        self.assertEqual(session_title(jl(aiTitle="Old") + jl(aiTitle="New"), sid), "New")
        self.assertEqual(session_title(b"", sid), "session 12345678")

    def test_cache_reads_only_what_was_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            path.write_bytes(jl(aiTitle="First"))
            cache = TitleCache()
            self.assertEqual(cache.title(path, "s"), "First")
            with open(path, "ab") as f:
                f.write(jl(customTitle="Renamed"))
            self.assertEqual(cache.title(path, "s"), "Renamed")
            self.assertEqual(tail_title(path, "s"), "Renamed")


class ArchiveTest(unittest.TestCase):
    def test_round_trip_keeps_bytes_and_mtimes(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "a.txt"
            src.write_bytes(b"alpha")
            import os
            os.utime(src, (1_700_000_000, 1_700_000_000))
            got = untar(tar_files({"dir/a.txt": src, "b.bin": b"\x00\x01"}))
            self.assertEqual(got["dir/a.txt"], (b"alpha", 1_700_000_000))
            self.assertEqual(got["b.bin"][0], b"\x00\x01")
            self.assertEqual(untar(b""), {})


if __name__ == "__main__":
    unittest.main()
