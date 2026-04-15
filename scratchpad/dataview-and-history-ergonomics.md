  2. DataView usability

  Current state: it's a functional watch panel. You right-click tags to add them, they show up in a table, you can
  force/patch. Groups auto-expand UDTs. But it's pretty bare-bones as a tool you'd actually leave open during a debug
  session. Here's what I'd bucket:

  2a. Filtering out noise (the "why would I add RESETTING" problem)

  Your readonly hint already gives a visual cue. But the real issue is the add-to-view flow -- when you right-click a
  tag in the editor, you get every tag at that location. For enum constants, there's no point adding them.

  Options:
  - Filter at source: The "Add to Data View" context menu could skip readonly tags, or show them in a separate "also
  available (read-only)" submenu
  - Filter in view: Let them be added but auto-collapse readonly tags into a dimmed section
  - Do nothing: The RO badge + dimmed force button is enough -- the user just won't add them

  I'd lean toward "filter at source" for constants and "show with RO badge" for inputs (which are readonly from the
  PLC's perspective but you'd still want to monitor them).

  2b. Drag-to-reorder

  The table is insertion-ordered right now. Drag-to-reorder is one of those things where you either use a library or
  spend 3 days fighting drag ghosts and scroll boundaries. In a VS Code webview:

  - Lightweight option: https://github.com/SortableJS/Sortable -- ~10KB, zero deps, works in any DOM. Single <script>
  tag, no npm/build step. This is the practical choice.
  - Even lighter: Native HTML5 drag events. Doable for a flat list, annoying for groups. Probably not worth the DIY
  effort.
  - Skip it: Let users remove and re-add in the order they want. Ugly but functional.

  SortableJS seems like the right call -- it's one file, no dep chain, and it handles groups natively.

  2c. Multiple DataViews / save-load

  This is a bigger architectural question. Right now it's a single WebviewViewProvider registered to one sidebar slot.

  - Multiple views: VS Code's webview view API doesn't natively support "open N instances of the same view." You'd need
  to switch to WebviewPanel (editor-area panels) or use a single view with tabs/presets inside it.
  - Save/load: A "presets" approach inside the single view seems more practical. Store watched tag sets as named configs
   in workspace settings or a .pyrung/dataviews.json. A dropdown to switch between "Motor Control", "State Machine",
  "Counters" presets.

  I'd suggest starting with save/load presets inside the existing single view before thinking about multiple
  simultaneous panels.

  2d. Other small wins

  - Auto-add from UDTs: Right-clicking a Timer/Counter could auto-add the whole group
  - Search/filter within the view: When you have 20+ tags, a filter box helps
  - Sparkline or trend: Show last N values inline (tiny, no library needed, just a <canvas> with a few lines)

  3. History -- making it actually useful for debugging

  The current slider is "scan 0 to scan N, drag to seek." That's only useful if you know which scan to go to, which you
  almost never do. Here's what would make it a real debugging tool:

  3a. "Rewind to just before this change"

  This is the killer feature. You want: "show me the state right before State changed from SORTING to RESETTING." The
  engine already has runner.history and runner.diff(). The UI needs:

  - A "changed tags" list showing which tags changed at the current scan
  - Click a tag to jump to "previous change of this tag"
  - Or: a filtered history showing only scans where a specific tag changed

  This is basically a tag change log with navigation, not a slider.

  3b. Time-based rewind

  "Rewind 500ms" is more natural than "rewind 50 scans" because dt varies. The engine already tracks timestamps. The
  slider labels could show elapsed time instead of scan IDs, and input fields could accept 500ms, 2s, 1min etc.

  3c. Diff view

  When you seek to a historical scan, show what's different from the current state (or from the previous scan), not just
   a flat dump of all tags. Highlight changed values. The runner.diff() API already exists.

  3d. What to build first

  I'd prioritize in this order:
  1. Tag change log (scan-by-scan changelog with tag filters) -- this is what makes history usable
  2. Time labels on the slider (elapsed time, not scan IDs)
  3. Diff highlighting when seeking
  4. "Jump to previous change" per tag

  ---
  My suggested prioritization across all three areas:

  ┌──────────┬───────────────────────────────────────────┬────────┐
  │ Priority │                   Item                    │ Effort │
  ├──────────┼───────────────────────────────────────────┼────────┤
  │ 1        │ Add readonly/choices to click_conveyor.py │ Small  │
  ├──────────┼───────────────────────────────────────────┼────────┤
  │ 2        │ Tag change log in History panel           │ Medium │
  ├──────────┼───────────────────────────────────────────┼────────┤
  │ 3        │ Save/load DataView presets                │ Medium │
  ├──────────┼───────────────────────────────────────────┼────────┤
  │ 4        │ SortableJS drag-to-reorder                │ Small  │
  ├──────────┼───────────────────────────────────────────┼────────┤
  │ 5        │ Time labels on history slider             │ Small  │
  ├──────────┼───────────────────────────────────────────┼────────┤
  │ 6        │ Diff highlighting on seek                 │ Medium │
  ├──────────┼───────────────────────────────────────────┼────────┤
  │ 7        │ Filter readonly from "Add to DataView"    │ Small  │
  └──────────┴───────────────────────────────────────────┴────────┘

	