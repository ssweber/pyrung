"""Neutral scan-0 target overwritten by a later ordinary guard.

The first program scan writes ``TARGET``.  A later, non-advance rung observes
the default-false interlock and overwrites that transient value before the
scan commits.  Pilot therefore has exact scan-0 evidence that preserving the
target requires the interlock to be true before that later read.
"""

from pyrung import Bool, Int, Program, copy, rung, system

INITIAL = 0
TARGET = 1
DIVERTED = 9

SequenceState = Int("BootstrapGuardSequenceState", default=INITIAL)
OverwriteInterlock = Bool("BootstrapOverwriteInterlock", external=True)


with Program() as logic:
    with rung(system.sys.first_scan):
        copy(TARGET, SequenceState)

    with rung(~OverwriteInterlock):
        copy(DIVERTED, SequenceState, oneshot=True)
