"""The palette and the shared furniture every screen is drawn with.

One place, because a colour repeated per page is a colour that drifts. The
values are the ones Andrei approved on the redesign mockup: paper and ink
rather than white and black, a single green accent, amber and red reserved for
STATES — never for decoration, so an amber number on screen always means
something is waiting and a red one always means something is wrong.

Light only, deliberately. Quasar's components follow NiceGUI's own dark mode
and the rules below follow the browser's, so honouring both would leave half
the screen in one scheme and half in the other. When dark returns it has to be
one switch driving both.
"""

from nicegui import ui

PAPER = "#f6f5f1"
INK = "#17201d"
ACCENT = "#1f6b57"
WARN = "#9a5b18"
DANGER = "#9b2c2c"

# Serif for the things he reads (titles, posting text), sans for the interface,
# mono for every figure — a column of scores only lines up in a tabular font.
CSS = """
:root {
  --paper:#f6f5f1; --surface:#fff; --surface-2:#fbfaf7; --sunken:#eceae3;
  --ink:#17201d; --ink-2:#5a655f; --ink-3:#8b948e; --ink-4:#b3bab5;
  --rule:#e3e1d9; --rule-2:#cfcdc3;
  --accent:#1f6b57; --accent-2:#3d8f78; --accent-deep:#12483b; --accent-soft:#e4efe9;
  --warn:#9a5b18; --warn-soft:#fbf0e2; --danger:#9b2c2c; --danger-soft:#f8e9e7;
  --jd-sans:"Cantarell","Noto Sans","Segoe UI",system-ui,sans-serif;
  --jd-serif:"Noto Serif","DejaVu Serif",Georgia,serif;
  --jd-mono:"JetBrains Mono","DejaVu Sans Mono",ui-monospace,monospace;
}
body { background: var(--paper); color: var(--ink); font-family: var(--jd-sans); }
.jd-mono { font-family: var(--jd-mono); font-variant-numeric: tabular-nums; }
.jd-serif { font-family: var(--jd-serif); }

/* ---- the rail: a spine, not a menu ---------------------------------- */
.jd-rail { background: var(--surface-2); border-right: 1px solid var(--rule); }
.jd-brand { font: 500 15px/1 var(--jd-serif); }
.jd-sec { display:block; width:100%; text-align:left; padding:8px; border-radius:7px;
          cursor:pointer; border:0; background:transparent; }
.jd-sec:hover { background: var(--accent-soft); }
.jd-sec[data-current="true"] { background: var(--accent-soft); }
.jd-sec-name { font:500 14px/1.2 var(--jd-sans); color: var(--ink-2); }
.jd-sec[data-current="true"] .jd-sec-name { color: var(--accent-deep); font-weight:600; }
.jd-sec-count { font:500 12px/1 var(--jd-mono); color: var(--ink-3);
                font-variant-numeric: tabular-nums; }
.jd-sec-count.amber { color: var(--warn); }
.jd-sec-sub { font:400 11px/1.4 var(--jd-mono); color: var(--ink-4); }
.jd-track { display:block; height:4px; border-radius:3px; background: var(--sunken);
            overflow:hidden; }
.jd-track > i { display:block; height:100%; background: var(--accent-2); border-radius:3px; }
.jd-track > i.amber { background: var(--warn); }
.jd-sec[data-enabled="false"] { opacity:.55; cursor:default; }
.jd-sec[data-enabled="false"]:hover { background: transparent; }

/* the views listed under the rubric he is in */
.jd-view { display:flex; width:100%; gap:8px; padding:4px 7px; border:0;
           background:transparent; border-radius:5px; cursor:pointer;
           color: var(--ink-2); font:400 12.5px/1.4 var(--jd-sans); text-align:left; }
.jd-view:hover { background: var(--accent-soft); color: var(--accent-deep); }
.jd-view[data-current="true"] { color: var(--ink); font-weight:600; }
.jd-view-count { margin-left:auto; font:400 11.5px/1.4 var(--jd-mono);
                 color: var(--ink-3); font-variant-numeric: tabular-nums; }

/* ---- the foot: what may still leave today, and what the engine is at -- */
.jd-flabel { font:600 9.5px/1 var(--jd-sans); letter-spacing:.13em;
             text-transform:uppercase; color: var(--ink-4); }
.jd-budget-box { width:15px; height:15px; border-radius:4px;
                 border:1px solid var(--rule-2); display:block; }
.jd-budget-box.on { background: var(--accent); border-color: var(--accent); }
.jd-pulse { font:400 11px/1.4 var(--jd-mono); color: var(--ink-3); }
.jd-pulse-dot { width:6px; height:6px; border-radius:50%; background: var(--accent);
                flex:none; }
.jd-pulse-dot.idle { background: var(--rule-2); }
.jd-pulse-dot.run { animation: jd-pulse 1.4s ease-in-out infinite; }
@keyframes jd-pulse { 0%,100%{opacity:1} 50%{opacity:.25} }
@media (prefers-reduced-motion: reduce) { .jd-pulse-dot.run { animation: none; } }
"""


def install() -> None:
    """Apply the palette to the current page. Called once by `frame`."""
    ui.dark_mode(False)
    ui.colors(primary=ACCENT, secondary=WARN, negative=DANGER,
              positive=ACCENT, dark=INK)
    ui.add_css(CSS)
