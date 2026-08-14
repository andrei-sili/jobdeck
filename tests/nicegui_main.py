"""Entry point for NiceGUI's in-process page harness (`user` fixture).

`runpy.run_path` executes this file once per test, and it must leave every
real @ui.page registered — importing a page module is what runs its decorator,
and `pages/__init__.py` is deliberately empty.

Two things make a plain import list wrong here:
  * a module another test module already imported is a no-op on import, so its
    page would simply 404 depending on collection order;
  * NiceGUI's own teardown pops each page module out of sys.modules while the
    parent package keeps the attribute, and `from pkg import mod` then hands
    back that stale attribute without executing anything.
Asking sys.modules directly covers both.
"""

import importlib
import sys

from nicegui import ui

PAGE_MODULES = (
    "applications", "cockpit", "dashboard", "jobs", "queue", "settings",
    "unterlagen",
)

for _name in PAGE_MODULES:
    _full = f"jobdeck.ui.pages.{_name}"
    _loaded = sys.modules.get(_full)
    if _loaded is None:
        importlib.import_module(_full)
    else:
        # reload re-runs the decorator IN THE SAME module namespace, so a test
        # that monkeypatches the module still patches what the route closes over
        importlib.reload(_loaded)

ui.run(port=0, show=False, reload=False, storage_secret="test-only")
