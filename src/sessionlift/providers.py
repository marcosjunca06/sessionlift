"""Optional: a cloud machine sessionlift may start, and a firewall rule it keeps open for the laptop's
current address. Only Google Cloud (gcloud) is built; kind "none" leaves the server to you."""

from __future__ import annotations

import re
import shutil
import urllib.request

from .context import log
from .gitsync import run


class NoProvider:
    can_open = False

    def __init__(self, cfg) -> None:
        self.cfg = cfg

    def open(self) -> None:
        log(f"no provider configured: nothing to start or open for {self.cfg.host}")


class Gcp:
    """Starts the instance when it is stopped, and keeps the firewall rule's source ranges on this
    laptop's last few public addresses (it moves between networks; key-only ssh guards the rest)."""
    can_open = True

    def __init__(self, cfg) -> None:
        self.cfg, self.p = cfg, cfg.provider

    def gcloud(self, *args: str) -> str:
        exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
        if not exe:
            raise SystemExit("gcloud not found; cannot start the instance or open the firewall")
        argv = [exe, "--quiet"]
        if self.p.gcloud_configuration:
            argv.append(f"--configuration={self.p.gcloud_configuration}")
        if self.p.project:
            argv.append(f"--project={self.p.project}")
        r = run([*argv, *args], text=True)
        if r.returncode != 0:
            raise SystemExit(f"gcloud {' '.join(args[:3])}: {r.stderr.strip()}")
        return r.stdout.strip()

    def open(self) -> None:
        status = self.gcloud("compute", "instances", "describe", self.p.instance, f"--zone={self.p.zone}",
                             "--format=value(status)")
        if status != "RUNNING":
            log(f"{self.p.instance} is {status}; starting it")
            self.gcloud("compute", "instances", "start", self.p.instance, f"--zone={self.p.zone}")
        if not self.p.firewall_rule:
            return
        # This laptop's public IPv4 address, from a public "what is my IP" service.
        ip = urllib.request.urlopen("https://api.ipify.org", timeout=15).read().decode().strip()
        if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
            raise SystemExit(f"api.ipify.org answered {ip[:60]!r}, not an address; the firewall is left as it is")
        current = [r for r in re.split(r"[;,\s]+", self.gcloud(
            "compute", "firewall-rules", "describe", self.p.firewall_rule, "--format=value(sourceRanges)")) if r]
        wanted = keep_addresses(f"{ip}/32", current, self.p.keep_addresses)
        if wanted != current:
            log(f"firewall: {self.p.firewall_rule} now admits {ip} (and the last {len(wanted) - 1} address(es))")
            self.gcloud("compute", "firewall-rules", "update", self.p.firewall_rule,
                        f"--source-ranges={','.join(wanted)}")


def keep_addresses(cidr: str, current: list, keep: int) -> list:
    """The rule's new source ranges: this address first, then the ones it had, `keep` at most."""
    return list(dict.fromkeys([cidr, *current]))[:keep]


def provider_for(cfg):
    return Gcp(cfg) if cfg.provider.kind == "gcp" else NoProvider(cfg)
