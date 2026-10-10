"""Compat alias: brain_api is g_brain.api. Remove when no caller uses the old name."""
import importlib, runpy, sys
if __name__ == "__main__":
    runpy.run_module("g_brain.api", run_name="__main__", alter_sys=True)
else:
    sys.modules[__name__] = importlib.import_module("g_brain.api")
