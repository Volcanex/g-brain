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
# du walks every file, so the disk report runs at the cleanup cadence, not every probe.
REPORT_EVERY = 6 * 3600
REPORT_KEEP = 8          # two days of reports at that cadence, enough to see a day's growth
REPORT_GROWTH = 100 * 1024 ** 2
REPORT_TOP = 10


def _disk(usage):
    # As df reports it: blocks reserved for root are not space the services can use.
    return {"used_pct": round(100 * usage.used / (usage.used + usage.free), 1),
            "free_gb": round(usage.free / 1024 ** 3, 2)}


def size(n):
    return f"{n / 1024 ** 3:.1f} GB" if abs(n) >= 1024 ** 3 else f"{n / 1024 ** 2:.0f} MB"


def _leaves(rows, key):
    """Drop a path when one directory under it carries most of its number, so the
    list names the tree that actually holds or grew the data, not each parent of it."""
    keep = []
    for row in rows:
        inside = [r for r in rows if r["path"].startswith(row["path"].rstrip("/") + "/")]
        if not any(r[key] >= 0.8 * row[key] for r in inside):
            keep.append(row)
    return sorted(keep, key=lambda r: -r[key])[:REPORT_TOP]


def _growers(reports):
    """Paths that grew between the oldest kept report and the newest, fastest first."""
    if len(reports) < 2:
        return []
    first, last = reports[0], reports[-1]
    hours = round((last["ts"] - first["ts"]) / 3600, 1)
    rows = [{"path": p, "grew": n - first["sizes"][p], "hours": hours}
            for p, n in last["sizes"].items() if p in first["sizes"]]
    return _leaves([r for r in rows if r["grew"] >= REPORT_GROWTH], "grew")


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

    def disk_report(self, disk):
        """What holds the disk and what grew, while it stays over the cleanup line.

        Read-only: the wrapper runs du as root and prints {path: bytes}. A short
        history sits beside the snapshot in `disk-usage.json`, so growth is measured
        rather than guessed, and the vault syncs it, so an agent off the host can read
        it too. Returns the fastest growers for the page."""
        if disk["used_pct"] < 85:
            return []
        path = self.directory / "disk-usage.json"
        try:
            data = json.loads(path.read_text())
            reports = [r for r in data.get("reports", [])
                       if type(r.get("ts")) in (int, float) and isinstance(r.get("sizes"), dict)]
        except (OSError, ValueError, AttributeError):
            data, reports = {}, []
        now = time.time()
        last = max([r["ts"] for r in reports] + [data.get("attempted") or 0])
        if now - last < REPORT_EVERY:
            return data.get("growers", [])
        data["attempted"] = now
        try:
            out = subprocess.run(["sudo", "-n", WRAPPER, "disk-report"],
                                 capture_output=True, text=True, timeout=300)
            sizes = json.loads(out.stdout)
            if not isinstance(sizes, dict) or not sizes:
                raise ValueError((out.stderr or out.stdout).strip() or f"exit {out.returncode}")
            reports = (reports + [{"ts": now, "sizes": sizes}])[-REPORT_KEEP:]
            largest = [{"path": p, "bytes": n} for p, n in sizes.items()]
            data.update(checked_at=datetime.now(timezone.utc).isoformat(), disk=disk, error=None,
                        largest=_leaves(largest, "bytes"), growers=_growers(reports), reports=reports)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            # Kept, with the attempt time, so a broken wrapper is not retried every probe.
            data["error"] = f"disk report unavailable: {exc}"[:300]
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
            tmp.replace(path)
        except OSError as exc:
            print(f"disk report not saved: {exc}", file=sys.stderr)
        return data.get("growers", [])

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
