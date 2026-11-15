# Freeze at build: sizing (read-only, 2026-11-09)

Ruling: a PLC keeps the definitions it was built with; edits reach only PLCs built after.

Facts: edits write in place into frozen Tags via object.__setattr__ (memory_block.py:722-725, TagMap _map.py:750-771,
note_metadata_edit _map.py:239); only unset fields, a second set raises (714-721). PLCs find Tags by walking the
shared rungs (runner.py:407-408); forks/replays/history copy _tag_index (1203, 2868, 3407).
Side bugs: late name= changes Tag.name, and __hash__ hashes the name (tag.py:231-232). Late default= skips
__post_init__ (tag.py:183-229): store_char, REAL->float, readonly+external check.
Range-reached slots are live by design (input_overrides.py:169-170, 265-266, 375, 122-128).
Docs: docs/guides/tag-structures.md:205 "All configuration must happen before you index the slot".
Tests pinning late edits: test_slots_a_range_reaches.py:352-360, 370-377, 413-421; test_memory_bank.py:311-319 (no PLC).

Readers of Tag metadata: runtime ~75 (48 get_tag(name, default) fallbacks; reset/reboot runner.py:4424-4428,
compiled_plc.py:891-894; recovers runner.py:2724-2744), kernel/codegen ~25 (kernel.py:199-228 compiled lazily
runner.py:3307-3362; read_trace.py:103), analyses ~180 (classify 68, absorb 25, pilot ~24, pdg, value_sets...),
harness ~31 (read at install), DAP ~43, validation ~31, TagMap/export ~225 (out of scope).

(a) per-PLC definitions table, every reader asks: 40-60 files, 400-700 lines. Big.
(b) copy-on-write Tags: fails the ruling (new PLCs still walk old Tags), breaks identity. No.
(c) freeze what the scan reads, analyses refuse:
  - build copies each known Tag's fields plus configured range-reached slots' name/default/retentive; forks inherit.
  - scan reads switch: reset rebuild (runner.py:4421-4433, compiled_plc.py:885-894), TagsRungsReach stops following
    renames/resets after build (input_overrides.py:122-128, 291-317, 375), missing-name fallback in
    ScanContext.get_tag (context.py:180-200, 413-440) and CompiledPLC._read (compiled_plc.py:129), recovers().
  - kernel guard: refuse a kernel whose defaults/readonly differ from the copy, per tag (not the global counter, so
    ClickDataProvider(plc, TagMap(...)) keeps the kernel).
  - analysis guard at prove, how, dataview, cause, effect, recovers, query surveys, Harness.install: raise "build a
    new PLC" if a known tag's definition changed after build.
  - separate staleness bug: stamp Rung._written_value_cache (sp_values.py:188-203) and pilot lru_caches
    (advance.py:62, static_expressions.py:28, trace.py:308) with metadata_edits(), as _cached_graph does.
  - optional: refuse late name= on a slot a built PLC knows; normalise late default= as __post_init__ does.
  8-10 files, 200-300 src lines, 150-250 test lines. Not big.
