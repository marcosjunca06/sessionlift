"""Auto memory on both machines: files go the way they are newer; the index merges by line."""

import os
import time
import unittest

from sessionlift.memory import merge_memory_index, sync_memory

from fakes import SessionCase


class MergeTest(unittest.TestCase):
    def test_three_way(self):
        base = "- a\n- b\n"
        self.assertEqual(merge_memory_index(base, "- a\n- b\n- mine\n", "- a\n- theirs\n"), "- a\n- mine\n- theirs\n")

    def test_union_without_a_base(self):
        self.assertEqual(merge_memory_index(None, "- a\n", "- b\n- a\n"), "- a\n- b\n")


class SyncTest(SessionCase):
    sync_memory = True

    def test_newer_files_win_both_ways_and_the_index_merges(self):
        laptop = self.folder / "memory"
        laptop.mkdir()
        server = self.server_folder / "memory"
        server.mkdir(parents=True)
        old, new = time.time() - 1000, time.time() - 10
        (laptop / "fact.md").write_text("laptop version")
        os.utime(laptop / "fact.md", (new, new))
        (server / "fact.md").write_text("server version")
        os.utime(server / "fact.md", (old, old))
        (server / "only-there.md").write_text("from the server")
        (laptop / "MEMORY.md").write_text("- [Fact](fact.md)\n")
        (server / "MEMORY.md").write_text("- [Only there](only-there.md)\n")
        sync_memory(self.ctx, self.project)
        self.assertEqual((server / "fact.md").read_text(), "laptop version")
        self.assertEqual((laptop / "only-there.md").read_text(), "from the server")
        merged = "- [Fact](fact.md)\n- [Only there](only-there.md)\n"
        self.assertEqual((laptop / "MEMORY.md").read_text(), merged)
        self.assertEqual((server / "MEMORY.md").read_text(), merged)

    def test_no_index_anywhere_makes_none(self):
        sync_memory(self.ctx, self.project)
        self.assertFalse((self.folder / "memory" / "MEMORY.md").exists())


if __name__ == "__main__":
    unittest.main()
