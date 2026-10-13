"""What `setup` makes of the laptop's Claude Code settings for the server, and the one switch for
bypass mode: off unless the config says so, wherever a session on the server could get it from."""

import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from sessionlift import moves, serverctl
from sessionlift.config import ConfigError, parse, template

from fakes import SessionCase

LAPTOP_HOOK = '"C:/Python/python.exe" -m sessionlift --config "C:/Users/me/AppData/Roaming/sessionlift/config.toml" hook'


class BypassTest(SessionCase):
    def test_off_by_default_everywhere(self):
        self.assertFalse(self.cfg.bypass_permissions)
        self.assertNotIn("--dangerously-skip-permissions", moves.server_claude_args(self.ctx))
        s = serverctl.adapted_settings({}, self.cfg)
        self.assertNotIn("permissions", s)
        self.assertNotIn("skipDangerousModePermissionPrompt", s)
        self.assertNotIn("bypass", serverctl.rc_unit(self.ctx, "dev", "/home/dev"))

    def test_a_laptop_in_bypass_mode_does_not_pass_it_on(self):
        laptop = {"permissions": {"defaultMode": "bypassPermissions", "allow": ["Bash(ls)"]},
                  "skipDangerousModePermissionPrompt": True}
        s = serverctl.adapted_settings(laptop, self.cfg)
        self.assertEqual(s["permissions"], {"allow": ["Bash(ls)"]})
        self.assertNotIn("skipDangerousModePermissionPrompt", s)
        self.assertEqual(laptop["permissions"]["defaultMode"], "bypassPermissions", "the laptop's own copy is untouched")

    def test_on_when_the_config_says_so(self):
        cfg = replace(self.cfg, bypass_permissions=True)
        self.ctx.cfg = cfg
        self.assertEqual(moves.server_claude_args(self.ctx)[0], "--dangerously-skip-permissions")
        s = serverctl.adapted_settings({}, cfg)
        self.assertEqual(s["permissions"]["defaultMode"], "bypassPermissions")
        self.assertIn("--permission-mode bypassPermissions", serverctl.rc_unit(self.ctx, "dev", "/home/dev"))

    def test_server_args_cannot_switch_it_on_behind_the_setting(self):
        good = template("devbox", [("/home/me/demo", "/srv/demo")])
        for args in ('["--dangerously-skip-permissions"]', '["--permission-mode", "bypassPermissions"]',
                     '["--permission-mode=bypassPermissions"]', '["--allow-dangerously-skip-permissions"]'):
            with self.subTest(args), self.assertRaises(ConfigError):
                parse(good.replace("server_args = []", f"server_args = {args}"), Path("c.toml"))
        self.assertEqual(parse(good.replace("server_args = []", 'server_args = ["--chrome"]'), Path("c.toml"))
                         .server_args, ["--chrome"])


class SettingsTest(SessionCase):
    def test_laptop_only_settings_stay_behind(self):
        laptop = {"statusLine": {"type": "command", "command": "powershell -File status.ps1"}, "voiceEnabled": True,
                  "model": "opus",
                  "hooks": {"UserPromptExpansion": [{"matcher": "to-server", "hooks": [
                      {"type": "command", "command": LAPTOP_HOOK, "timeout": 120}]}],
                      "Stop": [{"hooks": [{"type": "command", "command": "powershell -File C:\\hooks\\done.ps1"},
                                          {"type": "command", "command": "notify-send done"}]}],
                      "PreToolUse": [{"matcher": "Bash", "hooks": [
                          {"type": "command", "command": "python C:/tools/check.py"}]}]}}
        dropped = []
        s = serverctl.adapted_settings(laptop, self.cfg, dropped)
        self.assertNotIn("statusLine", s)
        self.assertNotIn("voiceEnabled", s)
        self.assertTrue(s["remoteControlAtStartup"])
        self.assertEqual(s["model"], "opus")
        self.assertEqual(s["hooks"]["UserPromptExpansion"][0]["hooks"][0]["command"], serverctl.SERVER_HOOK)
        self.assertEqual(s["hooks"]["Stop"][0]["hooks"], [{"type": "command", "command": "notify-send done"}])
        self.assertNotIn("PreToolUse", s["hooks"], "an event left without hooks goes")
        self.assertEqual(len(dropped), 2)

    def test_claude_json_patch(self):
        claude_json = self.tmp / "claude.json"
        claude_json.write_text(json.dumps({
            "oauthAccount": {"emailAddress": "not copied"},
            "mcpServers": {"docs": {"type": "http", "url": "https://example.com/mcp"},
                           "desktop-app": {"type": "stdio", "command": "C:\\Tools\\app.exe"},
                           "ps": {"type": "stdio", "command": "powershell", "args": ["-File", "x.ps1"]}},
            "projects": {str(self.local).replace("\\", "/").upper(): {
                "mcpServers": {"local-db": {"type": "stdio", "command": "npx", "args": ["db-mcp"]}}}}}),
            encoding="utf-8")
        with mock.patch.dict(os.environ, {"SESSIONLIFT_CLAUDE_JSON": str(claude_json)}):
            patch = serverctl.claude_json_patch(self.ctx, "/home/dev")
        self.assertEqual(set(patch), {"hasCompletedOnboarding", "mcpServers", "projects"})
        self.assertEqual(set(patch["mcpServers"]), {"docs"}, "servers that start Windows programs stay on the laptop")
        self.assertEqual(patch["projects"], {self.project.remote: {
            "hasTrustDialogAccepted": True, "mcpServers": {"local-db": {"type": "stdio", "command": "npx",
                                                                        "args": ["db-mcp"]}}}})
        self.ctx.cfg = replace(self.cfg, browser_mcp=True, copy_mcp_servers=False)
        with mock.patch.dict(os.environ, {"SESSIONLIFT_CLAUDE_JSON": str(claude_json)}):
            patch = serverctl.claude_json_patch(self.ctx, "/home/dev")
        self.assertEqual(list(patch["mcpServers"]), [serverctl.BROWSER_MCP_NAME])
        self.assertIn("/home/dev/.cache/playwright-mcp", patch["mcpServers"][serverctl.BROWSER_MCP_NAME]["args"])

    def test_the_remote_control_unit(self):
        unit = serverctl.rc_unit(self.ctx, "dev", "/home/dev")
        self.assertIn("User=dev", unit)
        self.assertIn(f"WorkingDirectory={self.project.remote}", unit)
        self.assertIn("claude remote-control --spawn worktree;", unit)
        self.assertNotIn("--chrome", unit)
        self.ctx.cfg = replace(self.cfg, server_args=["--chrome"])
        self.assertIn("--spawn worktree --chrome;", serverctl.rc_unit(self.ctx, "dev", "/home/dev"))

    def test_the_provisioning_script_ships(self):
        script = serverctl.provision_script()
        self.assertTrue(script.startswith(b"#!/usr/bin/env bash"))
        self.assertNotIn(b"\r\n", script)


if __name__ == "__main__":
    unittest.main()
