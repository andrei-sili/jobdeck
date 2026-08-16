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

/* ---- a screen that owns the viewport: a list and what it opens ------- */
.jd-screen { height: 100vh; display: grid; grid-template-rows: auto 1fr; min-height: 0; }
.jd-strip { display:flex; align-items:center; gap:12px; padding:10px 16px;
            border-bottom:1px solid var(--rule); background: var(--surface-2); }
.jd-strip-title { font:400 16px/1 var(--jd-serif); }
.jd-panes { display:grid; grid-template-columns: 400px 1fr; min-height:0; }
.jd-list { border-right:1px solid var(--rule); display:flex; flex-direction:column;
           min-height:0; background: var(--surface-2); }
.jd-rows { overflow-y:auto; min-height:0; flex:1; }
.jd-reader { display:flex; flex-direction:column; min-height:0; background: var(--surface);
             overflow-y:auto; }

.jd-row { display:grid; grid-template-columns:4px 1fr; border-bottom:1px solid var(--rule);
          cursor:pointer; background: var(--surface-2); width:100%; text-align:left;
          border-top:0; border-left:0; border-right:0; padding:0; }
.jd-row .jd-gutter { background:transparent; }
.jd-row[data-unread="true"] .jd-gutter { background: var(--accent); }
.jd-row[aria-selected="true"] { background: var(--surface); }
.jd-row[aria-selected="true"] .jd-gutter { background: var(--accent-deep); }
.jd-row-body { padding:10px 12px 9px; min-width:0; }
.jd-firma { font:600 14px/1.25 var(--jd-sans); overflow:hidden; text-overflow:ellipsis;
            white-space:nowrap; }
.jd-row[data-unread="false"] .jd-firma { font-weight:400; color: var(--ink-2); }
.jd-score { font:500 13.5px/1 var(--jd-mono); color: var(--ink-2);
            font-variant-numeric: tabular-nums; }
.jd-score.hi { color: var(--accent); }
.jd-title { font:400 12.5px/1.35 var(--jd-sans); color: var(--ink-2);
            overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.jd-meta { font:400 11px/1.5 var(--jd-mono); color: var(--ink-3);
           overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.jd-siblings { padding:5px 12px 7px 16px; background: var(--surface-2);
               border-bottom:1px solid var(--rule); font:400 11px/1.4 var(--jd-mono);
               color: var(--ink-3); }

.jd-ad { max-width:72ch; font-size:14px; line-height:1.65; }
.jd-why { margin-top:24px; max-width:72ch; border:1px solid var(--rule); border-radius:8px;
          padding:12px 15px; background: var(--surface-2); }
.jd-note { border-left:2px solid var(--rule-2); padding:5px 0 5px 10px;
           font-size:12.5px; color: var(--ink-2); }
.jd-note.warn { border-color: var(--warn); color: var(--warn); }
.jd-note.danger { border-color: var(--danger); color: var(--danger); }
.jd-facts { display:grid; grid-template-columns:auto 1fr; gap:2px 16px;
            font:400 12px/1.6 var(--jd-mono); color: var(--ink-2); }
.jd-facts .k { color: var(--ink-3); }
.jd-reason { font-size:12px; color: var(--warn); }
/* Applications under way, above the list and inside it: a sibling of the
   scroll container rather than a floating bar, so it is present in every view,
   on every page, under any search — and costs no grid change. */
.jd-laeuft:not(:empty) { border-bottom:1px solid var(--accent);
                         background: var(--surface-2); }
.jd-laeuft-row { padding:7px 10px; border-bottom:1px solid var(--rule); }
.jd-laeuft-row:last-child { border-bottom:0; }
/* One personal answer, ready to copy. Chips because they are the same on every
   German form — a row each would be eleven rows of furniture. */
.jd-chip { border:1px solid var(--rule-2); border-radius:20px;
           font:400 11.5px/1 var(--jd-mono); color: var(--ink-2);
           background: var(--surface); }
/* The ten seconds in which a recorded application can be taken back. Quiet,
   because it is a confirmation and not a warning — but it holds a control, so
   it sits on its own surface rather than reading as one more note. */
/* Pinned to the bottom of the viewport, because `overlay` renders as a
   sibling PRECEDING the 100vh screen: laid out in flow it sat above the fold,
   and this is the control that replaced a confirmation dialog — one he cannot
   see is one he cannot use. */
.jd-undo { position:fixed; bottom:18px; left:50%; transform:translateX(-50%);
           z-index:2500; border:1px solid var(--accent); border-radius:8px;
           background: var(--surface); box-shadow:0 3px 14px rgba(0,0,0,.16);
           padding:6px 8px 6px 12px; }
@media (max-width: 1080px) {
  .jd-panes { grid-template-columns: 1fr; }
  .jd-screen { height: auto; }
}

/* The two faces of one rubric. Underlined rather than boxed: they are places
   in the same room, and a boxed tab reads as a mode you have to leave. */
.jd-tabs { border-bottom:1px solid var(--rule); padding-bottom:0; }
.jd-tab { border:0; background:transparent; cursor:pointer; padding:6px 12px 7px;
          font:400 13.5px/1.4 var(--jd-sans); color: var(--ink-3);
          border-bottom:2px solid transparent; margin-bottom:-1px; }
.jd-tab:hover { color: var(--accent-deep); }
.jd-tab[data-current="true"] { color: var(--ink); font-weight:600;
                               border-bottom-color: var(--accent); cursor:default; }

/* The stack that drains to zero. Present only while something is in it, so
   it is drawn as a thing that arrived rather than as a permanent row. */
.jd-shelf { display:block; width:100%; text-align:left; padding:7px 9px;
            margin-bottom:14px; border:1px solid var(--accent-2);
            border-radius:7px; background: var(--accent-soft); cursor:pointer; }
.jd-shelf:hover { background: var(--surface); }
.jd-shelf-name { display:block; font:600 12px/1.2 var(--jd-sans);
                 color: var(--accent-deep); }
.jd-shelf-sub { display:block; margin-top:2px; font:400 11px/1.4 var(--jd-mono);
                color: var(--ink-2); }

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

/* ---- Unterlagen: the Mappe as a stack you can weigh ------------------ */
.jd-card { background: var(--surface); border:1px solid var(--rule); border-radius:10px;
           padding:16px 18px; width:100%; }
.jd-card-title { font:400 17px/1.3 var(--jd-serif); }
.jd-card-sub { font:400 12px/1.5 var(--jd-sans); color: var(--ink-3); }

/* Page range, what it is, how heavy — one grid so the columns line up and
   the eye can run down the weights without reading the names. */
.jd-stack { display:grid; grid-template-columns:auto 1fr auto auto; gap:0 14px;
            width:100%; }
.jd-stack > * { padding:6px 0; border-bottom:1px solid var(--rule); min-width:0; }
.jd-stack > .last { border-bottom:0; }
.jd-pageno { font:400 11.5px/1.6 var(--jd-mono); color: var(--ink-4);
             font-variant-numeric: tabular-nums; white-space:nowrap; }
.jd-partname { font:400 13px/1.6 var(--jd-sans); overflow:hidden;
               text-overflow:ellipsis; white-space:nowrap; }
.jd-partmeta { font:400 11.5px/1.6 var(--jd-mono); color: var(--ink-3);
               font-variant-numeric: tabular-nums; white-space:nowrap;
               text-align:right; }
.jd-partmeta.warn { color: var(--warn); }
.jd-total { font:500 13px/1.6 var(--jd-mono); font-variant-numeric: tabular-nums; }

/* The letter head, drawn the way the page it describes is laid out. */
.jd-letter { border:1px solid var(--rule); border-radius:8px; background: var(--surface-2);
             padding:16px 18px; max-width:52ch; font:400 12.5px/1.7 var(--jd-sans); }
.jd-letter .addr { white-space:pre-line; }
.jd-letter .date { margin-top:14px; text-align:right; color: var(--ink-2); }
.jd-letter .subj { margin-top:14px; font-weight:600; }
.jd-letter .body { margin-top:10px; color: var(--ink-3); font-style:italic; }
.jd-letter .gap { color: var(--warn); font-family: var(--jd-mono); }

/* ---- Bewerbungen: where the work went, and what came back ------------ */
/* Name, bar, figure, and the caveat the figure needs — one grid, so the
   numbers line up in a column the eye can run down without reading names. */
.jd-funnel { display:grid; grid-template-columns:minmax(0,15rem) 1fr auto;
             gap:3px 14px; width:100%; align-items:center; }
.jd-funnel .name { font:400 13px/1.5 var(--jd-sans); color: var(--ink-2); }
.jd-funnel .num { font:500 13px/1.5 var(--jd-mono); text-align:right;
                  font-variant-numeric: tabular-nums; }
/* The note spans all three columns under its own step: a caveat parked in a
   caption at the foot of the card is a caveat about nothing in particular. */
.jd-funnel .why { grid-column:1 / -1; font:400 11.5px/1.5 var(--jd-sans);
                  color: var(--warn); padding:0 0 5px 2px; }
.jd-bar { display:block; height:9px; border-radius:3px; background: var(--sunken); }
.jd-bar > i { display:block; height:100%; background: var(--accent-2);
              border-radius:3px; min-width:2px; }
.jd-bar > i.dim { background: var(--accent-soft); border:1px solid var(--accent-2); }
.jd-bar > i.warn { background: var(--warn); }

/* Sixty days, one column each. Height is the day's count against the busiest
   day, so an empty day is a visible gap rather than a missing column. */
.jd-rhythm { display:flex; align-items:flex-end; gap:2px; height:56px;
             width:100%; }
.jd-rhythm > i { flex:1 1 0; min-width:0; background: var(--accent-2);
                 border-radius:2px 2px 0 0; }
.jd-rhythm > i.empty { background: var(--sunken); }
.jd-rhythm > i.today { background: var(--accent-deep); }
.jd-ends { display:flex; justify-content:space-between; width:100%;
           font:400 11px/1.4 var(--jd-mono); color: var(--ink-4); }

/* Who has not answered, and for how long. */
.jd-wait { display:grid; grid-template-columns:minmax(0,1fr) 7rem auto;
           gap:0 12px; align-items:center; width:100%; }
.jd-wait > * { padding:6px 0; border-bottom:1px solid var(--rule); min-width:0; }
.jd-wait > .last { border-bottom:0; }
.jd-wait .firma { font:400 13px/1.5 var(--jd-sans); overflow:hidden;
                  text-overflow:ellipsis; white-space:nowrap; }
.jd-wait .age { font:400 12px/1.5 var(--jd-mono); text-align:right;
                color: var(--ink-3); font-variant-numeric: tabular-nums;
                white-space:nowrap; }
.jd-wait .age.over { color: var(--warn); }

/* One application of the register, as a row that opens. */
.jd-app { display:grid; grid-template-columns:minmax(0,1fr) 6rem 7rem 8rem 5rem;
          gap:0 12px; align-items:center; width:100%; text-align:left;
          border:0; border-bottom:1px solid var(--rule); background:transparent;
          padding:0; cursor:pointer; font:inherit; }
.jd-app:hover { background: var(--accent-soft); }
.jd-app > * { padding:7px 0; min-width:0; }
.jd-app .firma { font:500 13px/1.5 var(--jd-sans); overflow:hidden;
                 text-overflow:ellipsis; white-space:nowrap; }
.jd-app .cell { font:400 11.5px/1.5 var(--jd-mono); color: var(--ink-3);
                font-variant-numeric: tabular-nums; overflow:hidden;
                text-overflow:ellipsis; white-space:nowrap; }
.jd-app .cell.right { text-align:right; }
.jd-app .cell.over { color: var(--warn); }
.jd-head { display:grid; grid-template-columns:minmax(0,1fr) 6rem 7rem 8rem 5rem;
           gap:0 12px; width:100%; border-bottom:1px solid var(--rule-2);
           font:600 9.5px/1 var(--jd-sans); letter-spacing:.1em;
           text-transform:uppercase; color: var(--ink-4); padding-bottom:6px; }
.jd-pill { display:inline-block; border-radius:20px; padding:2px 9px;
           font:400 11px/1.5 var(--jd-sans); background: var(--sunken);
           color: var(--ink-2); white-space:nowrap; }
.jd-pill.ok { background: var(--accent-soft); color: var(--accent-deep); }
.jd-pill.warn { background: var(--warn-soft); color: var(--warn); }

/* One row of the register: a permission, and how often a letter used it. */
.jd-claim { display:grid; grid-template-columns:1fr auto auto; gap:0 12px;
            align-items:center; width:100%; border-bottom:1px solid var(--rule); }
.jd-claim:last-child { border-bottom:0; }
.jd-claim-fact { font:400 13px/1.5 var(--jd-sans); padding:8px 0; min-width:0; }
.jd-claim-bind { color: var(--ink-3); }
.jd-claim-count { font:400 11.5px/1.5 var(--jd-mono); color: var(--ink-3);
                  white-space:nowrap; }
.jd-claim-count.never { color: var(--warn); }
.jd-claim-count.unknown { color: var(--ink-4); }
"""


def install() -> None:
    """Apply the palette to the current page. Called once by `frame`."""
    ui.dark_mode(False)
    ui.colors(primary=ACCENT, secondary=WARN, negative=DANGER,
              positive=ACCENT, dark=INK)
    ui.add_css(CSS)
