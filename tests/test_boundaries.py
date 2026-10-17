"""Component boundaries (brain-split.md section 1): which component may import which.

An AST scan maps every tracked module outside tests/ to a component and checks each
import against ALLOWED. An underscored name reached across components fails unless it
is in ALLOWLIST, which holds the todos privates that pulse still uses until todos grows
a public API (split step 3c). A component can only move to its own repo once the
allowlist is empty.
"""
import ast
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Module prefixes per component, longest match wins. Both the flat names (now shims)
# and the package homes are listed, so the map holds before and after each move.
COMPONENTS = {
    "config": ["g_config"],
    "spine": ["g_spine", "brain_alerts", "brain_status", "unit_alert"],
    "todos": ["g_todos", "brain_todos", "outlets"],
    "pulse": ["g_pulse", "brain_pulse", "inlets"],
    "brain": ["g_brain", "brain_reader", "brain_commit", "brain_index", "brain_refs",
              "brain_source", "brain_codex", "brain_verify", "brain_condense", "brain_gmap",
              "brain_miner", "brain_capture"],
    "host": ["brain", "brain_api", "brain_oauth", "brain_access", "brain_surface",
             "g_brain.server", "g_brain.api", "g_brain.oauth", "g_brain.access",
             "g_brain.surface"],
    "voice": ["voice"],
}

ALLOWED = {
    "config": set(),
    "spine": {"config"},
    "todos": {"spine", "config"},
    "pulse": {"todos", "spine", "config"},
    "brain": {"spine", "config", "pulse"},
    "voice": {"brain", "todos", "pulse", "spine", "config"},
    "host": {"config", "spine", "todos", "pulse", "brain", "voice"},
}

# Edges that may use only these names. Capture writes one line to the pulse page and
# reads the page first so it does not write the same line twice.
NARROW = {("brain", "pulse"): {"append_page", "PAGE_PATH"}}

# (importing module, "target module.private name"). Remove entries as todos gains
# public names; an entry nothing uses any more fails the test.
ALLOWLIST = {
    ("g_pulse.pulse", "brain_todos._iter_todos"),
    ("g_pulse.pulse", "brain_todos._serialize"),
    ("inlets.subs", "brain_todos._load"),
    ("inlets.subs", "brain_todos._save"),
    ("inlets.subs", "brain_todos._today"),
}


def _tracked_modules():
    out = subprocess.run(["git", "-c", f"safe.directory={REPO}", "-C", str(REPO),
                          "ls-files", "*.py"], capture_output=True, text=True, check=True)
    mods = {}
    for rel in out.stdout.split():
        if rel.startswith("tests/"):
            continue
        parts = rel[:-3].split("/")
        if parts[-1] == "__init__":
            parts.pop()
        mods[".".join(parts)] = REPO / rel
    return mods


def component(module):
    best = None
    for comp, prefixes in COMPONENTS.items():
        for p in prefixes:
            if (module == p or module.startswith(p + ".")) and (best is None or len(p) > len(best[1])):
                best = (comp, p)
    return best and best[0]


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def uses(module, path, known):
    """(target module, name or None) for every local import and module attribute read."""
    tree = ast.parse(path.read_text(), str(path))
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    bound, found = {}, []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in {k.split(".")[0] for k in known}:
                    found.append((a.name, None))
                    if a.asname:
                        bound[a.asname] = a.name
                    else:
                        bound[a.name.split(".")[0]] = a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".") if package else []
                anchor = anchor[:len(anchor) - (node.level - 1)]
                base = ".".join(anchor + ([base] if base else []))
            if base.split(".")[0] not in {k.split(".")[0] for k in known}:
                continue
            for a in node.names:
                sub = f"{base}.{a.name}" if base else a.name
                if sub in known:
                    found.append((sub, None))
                    bound[a.asname or a.name] = sub
                else:
                    found.append((base, a.name))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            chain = _dotted(node)
            if not chain or chain.split(".")[0] not in bound:
                continue
            head, _, rest = chain.partition(".")
            full = (bound[head] + "." + rest).split(".")
            for i in range(len(full) - 1, 0, -1):
                if ".".join(full[:i]) in known:
                    found.append((".".join(full[:i]), full[i]))
                    break
    return found


def _private(name):
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def scan():
    known = _tracked_modules()
    edges, problems, allowlisted = set(), [], set()
    for mod, path in sorted(known.items()):
        src = component(mod)
        if src is None:
            problems.append(f"{mod}: not assigned to a component")
            continue
        for target, name in uses(mod, path, known):
            dst = component(target)
            if dst is None:
                problems.append(f"{mod}: imports unassigned module {target}")
            if dst in (None, src):
                continue
            edges.add((src, dst))
            if dst not in ALLOWED[src]:
                problems.append(f"{mod} ({src}) imports {target} ({dst})")
            elif name and _private(name):
                if (mod, f"{target}.{name}") in ALLOWLIST:
                    allowlisted.add((mod, f"{target}.{name}"))
                else:
                    problems.append(f"{mod} ({src}) uses private {target}.{name} ({dst})")
            elif name and (src, dst) in NARROW and name not in NARROW[(src, dst)]:
                problems.append(f"{mod} ({src}) uses {target}.{name}, outside {src}->{dst}")
    return edges, problems, allowlisted


class Boundaries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.edges, cls.problems, cls.allowlisted = scan()

    def test_no_forbidden_imports_or_private_names(self):
        self.assertEqual(self.problems, [])

    def test_allowlist_has_no_stale_entries(self):
        self.assertEqual(ALLOWLIST - self.allowlisted, set())

    def test_scan_sees_the_known_edges(self):
        # Guards the scanner itself: these edges exist today, so a scan that finds
        # none of them is broken rather than clean.
        for edge in (("spine", "config"), ("todos", "spine"), ("pulse", "todos"),
                     ("brain", "spine"), ("brain", "pulse"), ("voice", "brain"),
                     ("voice", "pulse"), ("host", "brain")):
            self.assertIn(edge, self.edges)


class Scanner(unittest.TestCase):
    def test_component_prefers_the_longest_prefix(self):
        self.assertEqual(component("g_brain.reader"), "brain")
        self.assertEqual(component("g_brain.server"), "host")
        self.assertEqual(component("brain"), "host")
        self.assertEqual(component("brain_reader"), "brain")
        self.assertEqual(component("inlets.subs"), "pulse")
        self.assertIsNone(component("brainx"))

    def test_uses_resolves_aliases_relative_and_lazy_imports(self):
        import tempfile
        known = {"pkg", "pkg.a", "pkg.b", "flat"}
        src = ("import flat as f\nfrom . import a\nfrom .b import thing\n"
               "def g():\n    import pkg.a\n    return f._x, a.y, pkg.a._z\n")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.py"
            p.write_text(src)
            got = set(uses("pkg.c", p, known))
        for want in (("flat", None), ("flat", "_x"), ("pkg.a", None), ("pkg.a", "y"),
                     ("pkg.b", "thing"), ("pkg.a", "_z")):
            self.assertIn(want, got)


if __name__ == "__main__":
    unittest.main()
