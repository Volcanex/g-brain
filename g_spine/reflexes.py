"""Local, bounded remedies. The watcher supplies probes and owns paging."""
import errno
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import g_config


WINDOW = 6 * 3600
MAX_ATTEMPTS = 2
WRAPPER = "/usr/local/sbin/g-reflex"


def _disk(usage):
    # As df reports it: blocks reserved for root are not space the services can use.
    return {"used_pct": round(100 * usage.used / (usage.used + usage.free), 1),
            "free_gb": round(usage.free / 1024 ** 3, 2)}


def _unit(name):
    return name if Path(name).suffix else name + ".service"


class Reflexes:
    def __init__(self, directory=None):
        self.directory = directory or g_config.BRAIN_ROOT / "status"
        self.path = self.directory / "reflexes.json"
        self.error = None
        try:
            self.state = json.loads(self.path.read_text())
            if not isinstance(self.state, dict):
                raise ValueError("reflex state is not an object")
            for entry in self.state.values():
                if (not isinstance(entry, dict) or not isinstance(entry.get("attempts"), list)
                        or any(type(ts) not in (int, float) for ts in entry["attempts"])):
                    raise ValueError("invalid reflex attempt history")
        except FileNotFoundError:
            self.state = {}
        except (OSError, ValueError) as exc:
            self.state = {}
            self.error = f"reflex state unavailable: {exc}"

    def _save(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self.path)

    def _entry(self, remedy, target):
        entry = self.state.setdefault(f"{remedy}:{target}", {"attempts": []})
        entry["attempts"] = [ts for ts in entry["attempts"] if time.time() - ts < WINDOW]
        return entry

    def _attempt(self, remedy, target):
        entry = self._entry(remedy, target)
        if self.error or len(entry["attempts"]) >= MAX_ATTEMPTS:
            return None
        entry["attempts"].append(time.time())
        try:
            # Reserve the attempt before acting, including commands that fail.
            self._save()
        except OSError as exc:
            # Disk cleanup must still work when the disk cannot accept a write.
            if remedy != "clean-disk" or exc.errno != errno.ENOSPC:
                self.error = f"could not record reflex attempt: {exc}"
                return None
        cmd = ["sudo", "-n", WRAPPER, remedy]
        if remedy != "clean-disk":
            cmd.append(target)
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=180 if remedy == "clean-disk" else 30)
            detail = (out.stderr or out.stdout).strip()
            return {"ok": out.returncode == 0, "stdout": out.stdout,
                    "detail": detail or f"exit {out.returncode}"}
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "stdout": "", "detail": f"{type(exc).__name__}: {exc}"}

    def _record(self, remedy, target, before, after, detail):
        record = {"ts": datetime.now(timezone.utc).isoformat(), "remedy": remedy,
                  "target": target, "before": before, "after": after, "detail": detail}
        try:
            self._save()
            with (self.directory / "reflex-log.jsonl").open("a") as log:
                log.write(json.dumps(record) + "\n")
        except OSError as exc:
            print(f"reflex record failed: {exc}", file=sys.stderr)

    def clean_disk(self, usage):
        before = _disk(usage)
        if before["used_pct"] < 85:
            return before
        action = self._attempt("clean-disk", "/")
        if action is None:
            return before
        after_usage = shutil.disk_usage("/")
        after = _disk(after_usage)
        try:
            freed = max(0, int(action["stdout"].strip()))
        except ValueError:
            freed = max(0, after_usage.free - usage.free)
        after["freed_bytes"] = freed
        self._record("clean-disk", "/", before, after,
                     f"freed {freed} bytes; {action['detail']}")
        return after

    def repair_services(self, results, services, hosts, probe, local, check_unit):
        by_id = {svc["id"]: svc for svc in services}
        for index, r in enumerate(results):
            if not local(r.get("host")):
                continue
            unit = _unit(r["unit"]) if r.get("unit") else None
            # A restart is verified by a later timer run, never by waiting for indexing.
            for remedy, target in (("start", unit), ("restart", "basic-memory")):
                if not target or (remedy == "restart" and r["id"] != "basic-memory"):
                    continue
                entry = self.state.get(f"{remedy}:{target}", {})
                if r["state"] == "up" and entry.pop("pending", False):
                    r["healed"] = f"{'started' if remedy == 'start' else 'restarted'} {target}"
                    self._record(remedy, target, entry.get("before", "down"), "up",
                                 f"{r['healed']}; verified on a later probe")
            if r["state"] not in ("down", "warn"):
                continue
            detail = r.get("detail", "")
            live_detail, separator, _ = detail.partition("; search freshness:")
            stale = (r["id"] == "basic-memory" and separator and
                     live_detail.startswith("TCP ") and live_detail.endswith(" open"))
            if stale:
                remedy, target = "restart", "basic-memory"
            elif r["state"] == "down" and unit:
                host = hosts.get(r.get("host"), {})
                _, unit_detail = check_unit(r.get("host"), host.get("user"),
                                            host.get("hostname"), unit)
                if unit_detail not in ("systemctl: inactive", "systemctl: failed"):
                    continue
                remedy, target = "start", unit
            else:
                continue
            entry = self._entry(remedy, target)
            action = self._attempt(remedy, target)
            if action is None:
                tail = self.error or f"{remedy} attempted twice, still {r['state']}"
                r["detail"] += f"; {tail}"
                continue
            before = r["state"]
            entry["before"] = before
            entry["pending"] = action["ok"]
            if remedy == "start":
                time.sleep(3)
                r = probe(by_id[r["id"]], hosts)
                results[index] = r
                if r["state"] == "up":
                    r["healed"] = f"started {target}"
                    entry["pending"] = False
            if r["state"] in ("down", "warn"):
                if len(entry["attempts"]) < MAX_ATTEMPTS or (remedy == "restart" and action["ok"]):
                    r["reflex_pending"] = True
                    r["detail"] += f"; {remedy} attempted, verification pending"
                else:
                    r["detail"] += f"; {remedy} attempted twice, still {r['state']}"
            self._record(remedy, target, before, r["state"],
                         f"{action['detail']}; {r['detail']}")
