"""The config file: what init writes parses back, and mistakes are refused with a reason."""

import tempfile
import unittest
from pathlib import Path

from sessionlift.config import ConfigError, parse, template, toml_str

WINDOWS_LOCAL = "D:\\code\\demo"


class ConfigTest(unittest.TestCase):
    path = Path("config.toml")

    def test_template_round_trip(self):
        text = template("devbox", [(WINDOWS_LOCAL, "/home/dev/demo"), ("/Users/dev/other", "/srv/other")],
                        user="dev", port=2222, name="Dev box")
        cfg = parse(text, self.path)
        self.assertEqual((cfg.host, cfg.user, cfg.port, cfg.label), ("devbox", "dev", 2222, "Dev box"))
        self.assertEqual([(p.name, p.local, p.remote) for p in cfg.projects],
                         [("demo", WINDOWS_LOCAL, "/home/dev/demo"), ("other", "/Users/dev/other", "/srv/other")])
        self.assertFalse(cfg.bypass_permissions, "skipping permission prompts must be opt-in")
        self.assertEqual(cfg.provider.kind, "none")
        self.assertTrue(all(p.git_sync and p.sync_memory for p in cfg.projects))

    def test_gcp_provider(self):
        text = template("devbox", [(WINDOWS_LOCAL, "/home/dev/demo")],
                        provider={"kind": "gcp", "instance": "dev-1", "zone": "europe-west1-b", "firewall_rule": "dev-ssh"})
        p = parse(text, self.path).provider
        self.assertEqual((p.kind, p.instance, p.zone, p.firewall_rule, p.keep_addresses),
                         ("gcp", "dev-1", "europe-west1-b", "dev-ssh", 4))

    def test_refusals(self):
        good = template("devbox", [(WINDOWS_LOCAL, "/home/dev/demo")])
        cases = {
            "unknown key": good.replace("[claude]", "[claude]\nbypass = true"),
            "unknown section": good + "\n[extras]\nx = 1\n",
            "no host": good.replace("host = 'devbox'", "host = ''"),
            "relative remote": good.replace("remote = '/home/dev/demo'", "remote = 'demo'"),
            "relative local": good.replace(f"local = '{WINDOWS_LOCAL}'", "local = 'code/demo'"),
            "wrong type": good.replace("bypass_permissions = false", "bypass_permissions = 'no'"),
            "gcp without zone": good.replace("kind = 'none'", "kind = 'gcp'\ninstance = 'x'"),
            "no project": good.split("[[project]]")[0],
        }
        for what, text in cases.items():
            with self.subTest(what), self.assertRaises(ConfigError):
                parse(text, self.path)

    def test_two_projects_cannot_share_a_checkout(self):
        text = template("devbox", [(WINDOWS_LOCAL, "/srv/demo"), ("E:\\other\\demo2", "/srv/demo")])
        with self.assertRaises(ConfigError):
            parse(text, self.path)

    def test_strings_survive_toml(self):
        for text in ["plain", "it's", 'say "hi"', "back\\slash", "tab\there", "C\u00e9line"]:
            cfg = parse(template(text if text.isprintable() else "h", [(WINDOWS_LOCAL, "/srv/x")], name=text), self.path)
            self.assertEqual(cfg.name, text, toml_str(text))

    def test_load_reports_a_missing_file(self):
        from sessionlift.config import load
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ConfigError) as caught:
            load(Path(tmp) / "nope.toml")
        self.assertIn("sessionlift init", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
