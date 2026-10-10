"""Compat alias: brain_alerts is g_spine.alerts. Remove when no caller uses the old name."""
import importlib, runpy, sys
if __name__ == "__main__":
    runpy.run_module("g_spine.alerts", run_name="__main__", alter_sys=True)
else:
    sys.modules[__name__] = importlib.import_module("g_spine.alerts")
