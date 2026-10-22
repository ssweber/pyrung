# Conformance workspace

<!-- pyrung:guidance --> ClickNick's own guide to this folder is README.pyrung.md, written on every CLICK save. This marker line keeps ClickNick from inserting its pointer here.

The ClickNick workspace for the conformance CLICK project. `.clicknick.toml` links it to the CLICK PLC named `PYRUNG_CONFORMANCE`. ClickNick fills in the rest (`src/plc/`, `csv/`, `nicknames.csv`, its scripts and seeds) after every CLICK save; all of that is gitignored. The suite itself is described in [../AGENTS.md](../AGENTS.md).

## First load

1. `make conformance-export OUT=build/conformance`
2. Open a new project in CLICK for the conformance PLC. In ClickNick, **Ladder > Open in Guided Paste...** and pick `build/conformance`: it imports `nicknames.csv`, then walks you through pasting `main.csv` and each `subroutines/Conf_<Group>.csv`.
3. In ClickNick's Workspace controls, find an existing workspace and pick this folder. If the project has no PLC name yet, ClickNick asks for one: enter `PYRUNG_CONFORMANCE`. A project already named something else has to be renamed to that in CLICK, or ClickNick refuses the folder.
4. Save in CLICK. ClickNick writes `src/plc/` and `csv/` here.
5. Write the project to the PLC, put it in RUN, and run `make test-conformance PLC=host`.

## Per update

1. Edit a group's cases (add the manifest line if `make test-conformance` asks for one).
2. `make conformance-workspace` regenerates `src/plc/` from the suite, exactly as ClickNick would from the same ladder. It leaves `csv/` alone: that is what CLICK last accepted, and the baseline ClickNick diffs against.
3. In ClickNick, **Preview Changes**. Only the changed rungs show: the group's case rungs and its hash rung (rung 1 of `Conf_<Group>`). **Copy to Click**, paste in CLICK, save, write to the PLC.
4. `make test-conformance PLC=host GROUP=<group>`

New slots bring new nicknames. They are in the regenerated `src/plc/tags.py`; `clicknick-cli tag apply` stages them in the Address Editor (review **Changed**, then **Sync**). Nicknames only make the ladder readable; the harness reads registers, so this step can wait.

Adding, removing, renaming, or moving a group changes the main program and the layout hash in `DS1`, so Preview Changes shows the main rungs too (`R1` and the `call` rungs). A new group's subroutine does not show at all, because ClickNick only diffs subroutines CLICK already has: paste `subroutines/Conf_<Group>.csv` from `make conformance-export` with Guided Paste first, then take main from Preview Changes.

If a CLICK save lands while `src/plc/` holds unpasted edits, ClickNick copies them to `backup/src/plc/` first; `make conformance-workspace` regenerates them anyway.
