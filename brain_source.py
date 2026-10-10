"""Compat alias: brain_source is g_brain.source. Remove when no caller uses the old name."""
import importlib, runpy, sys
if __name__ == "__main__":
    runpy.run_module("g_brain.source", run_name="__main__", alter_sys=True)
else:
    sys.modules[__name__] = importlib.import_module("g_brain.source")
