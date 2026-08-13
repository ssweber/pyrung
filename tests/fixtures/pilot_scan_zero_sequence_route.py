"""Neutral, high-fidelity scan-zero sequence-routing fixture.

This preserves the rung topology and ordering of the field sequence that
motivated the adjacent-scan lifecycle work.  The names are deliberately
domain-neutral; the important features are the same-scan cascade, two
watchdogs, the terminal settling timer, the active-low interruption rung,
the late error writers, the reconnect repair followed by its adjacent-scan
status overwrite, and the unconditional reporting copy which observes the
structural sequence channel without becoming another route.
"""

from pyrung import (
    And,
    Bool,
    Counter,
    Int,
    Or,
    Program,
    Timer,
    branch,
    copy,
    count_up,
    latch,
    on_delay,
    out,
    rung,
    system,
)

SequenceStep = Int("RouteSequenceStep")
LocalSequenceStep = Int("RouteLocalSequenceStep")
ReportedSequenceStep = Int("RouteReportedSequenceStep")
RouteControl = Int("RouteControl")
RouteEnabled = Int("RouteEnabled")

ReadyCommand = Bool("RouteReadyCommand", external=True)
BaseSensor = Bool("RouteBaseSensor", external=True)
CheckpointSensor = Bool("RouteCheckpointSensor", external=True)
InterruptionInput = Bool("RouteInterruptionInput", external=True)
SafetyPermit = Bool("RouteSafetyPermit", external=True)
SimulationMode = Bool("RouteSimulationMode")
NetworkAvailable = Bool("RouteNetworkAvailable", external=True)
NetworkPeerReady = Bool("RouteNetworkPeerReady", external=True)
CommunicationPulse = Bool("RouteCommunicationPulse")
CommunicationSuccess = Bool("RouteCommunicationSuccess")
CommunicationError = Bool("RouteCommunicationError", external=True)
ServiceStatusInput = Int("RouteServiceStatusInput", external=True)
ServiceStatus = Int("RouteServiceStatus")

FirstWatchdogPresetMs = Int("RouteFirstWatchdogPresetMs", external=True)
SecondWatchdogPresetMs = Int("RouteSecondWatchdogPresetMs", external=True)
FirstTransitionDelayMs = Int("RouteFirstTransitionDelayMs")
SecondTransitionDelayMs = Int("RouteSecondTransitionDelayMs")
FirstDwellSeconds = Int("RouteFirstDwellSeconds")
SecondDwellSeconds = Int("RouteSecondDwellSeconds")
SettleSeconds = Int("RouteSettleSeconds")

PositionReading = Int("RoutePositionReading")
PositionThreshold = Int("RoutePositionThreshold")
BypassPositionCheck = Bool("RouteBypassPositionCheck")
SequenceInterrupted = Int("RouteSequenceInterrupted")

MovingPositive = Bool("RouteMovingPositive")
MovingNegative = Bool("RouteMovingNegative")
PositionSeen = Bool("RoutePositionSeen")

FirstWatchdog = Timer.clone("RouteFirstWatchdog")
FirstTransitionDelay = Timer.clone("RouteFirstTransitionDelay")
FirstDwell = Timer.clone("RouteFirstDwell")
SecondWatchdog = Timer.clone("RouteSecondWatchdog")
SecondTransitionDelay = Timer.clone("RouteSecondTransitionDelay")
SecondDwell = Timer.clone("RouteSecondDwell")
SettleDelay = Timer.clone("RouteSettleDelay")
RecoveryCounter = Counter.clone("RouteRecoveryCounter")


with Program() as logic:
    # R7/R9: the pre-route handoff that makes the disconnected-state writer's
    # <=20 source depend on the communication transaction.
    with rung(SequenceStep <= 10, RouteEnabled == 1):
        copy(11, SequenceStep)

    with rung(ReadyCommand, SequenceStep == 11):
        copy(20, SequenceStep, oneshot=True)

    # R16: the ordinary route into the same-scan sequence cascade.
    with rung(
        Or(SequenceStep == 22, And(ReadyCommand, RouteControl == 100)),
        BaseSensor,
    ):
        copy(40, SequenceStep, oneshot=True)

    # R17-R23: same order and branch structure as the field program.
    with rung(
        Or(
            And(SequenceStep >= 40, SequenceStep < 50),
            And(SequenceStep >= 60, SequenceStep < 70),
        )
    ):
        out(MovingPositive)

    with rung(SequenceStep == 40):
        with branch(CheckpointSensor):
            copy(41, SequenceStep, oneshot=True)
        on_delay(FirstWatchdog, FirstWatchdogPresetMs)

    with rung(SequenceStep == 41):
        on_delay(FirstTransitionDelay, FirstTransitionDelayMs)
        with branch(FirstTransitionDelay.Done):
            copy(50, SequenceStep, oneshot=True)

    with rung(SequenceStep == 50):
        on_delay(FirstDwell, FirstDwellSeconds, "sec")
        with branch(FirstDwell.Done):
            copy(60, SequenceStep, oneshot=True)

    with rung(SequenceStep == 60):
        copy(61, SequenceStep, oneshot=True)
        on_delay(SecondWatchdog, SecondWatchdogPresetMs)

    with rung(SequenceStep == 61):
        on_delay(SecondTransitionDelay, SecondTransitionDelayMs)
        with branch(SecondTransitionDelay.Done):
            copy(70, SequenceStep, oneshot=True)

    with rung(SequenceStep == 70):
        on_delay(SecondDwell, SecondDwellSeconds, "sec")
        with branch(SecondDwell.Done):
            copy(80, SequenceStep, oneshot=True)

    # R24-R27: terminal approach and settling receipt.
    with rung(Or(SequenceStep == 80, And(SequenceStep >= 90, ~SettleDelay.Done))):
        out(MovingNegative)

    with rung(Or(SequenceStep == 80, SequenceStep >= 90), BaseSensor):
        on_delay(SettleDelay, SettleSeconds, "sec")

    with rung(SequenceStep == 80):
        latch(PositionSeen)

    with rung(SequenceStep == 80, SettleDelay.Done):
        copy(81, SequenceStep, oneshot=True)

    # R28: terminal-value validity check.
    with rung(
        Or(
            PositionReading < PositionThreshold,
            BypassPositionCheck,
            SequenceInterrupted == 1,
        ),
        SequenceStep == 81,
    ):
        copy(10, SequenceStep, oneshot=True)

    # R29: active-low interruption.  With BaseSensor true this is the exact
    # late writer that destroys the terminal value and timer enable after the
    # input has gone true long enough to re-arm its one-shot.  The cold-start
    # scan executes it once while false; preserving that spent state is part of
    # the adjacent-scan route receipt.
    with rung(~InterruptionInput):
        with branch(BaseSensor):
            copy(10, SequenceStep, oneshot=True)
        with branch(
            Or(
                And(SequenceStep == 50, FirstDwell.Acc < 10),
                And(SequenceStep == 70, SecondDwell.Acc < 10),
                SequenceStep < 50,
                And(SequenceStep >= 60, SequenceStep < 70),
            ),
            ~BaseSensor,
        ):
            copy(2, LocalSequenceStep)
            copy(80, SequenceStep, oneshot=True)
            copy(1, SequenceInterrupted, oneshot=True)

    # The relevant late error writers retain their original ordering.
    with rung(FirstWatchdog.Done):
        copy(91, SequenceStep, oneshot=True)

    with rung(SecondWatchdog.Done):
        copy(92, SequenceStep, oneshot=True)

    with rung(~NetworkAvailable, SequenceStep <= 20):
        copy(98, SequenceStep, oneshot=True)

    # A durable reconnect first repairs the disconnected state. The sibling
    # status branch does not see that newly written value until the following
    # scan, where it can replace the productive 10 unless the program has
    # advanced beyond the local handoff or one of the program-owned status
    # alternatives changed. This is the neutral 98 -> 10 -> 94 mechanism.
    with rung(NetworkAvailable):
        with branch(SequenceStep == 98):
            copy(10, SequenceStep, oneshot=True)
        with branch(SequenceStep <= 20):
            with branch(RecoveryCounter.Done):
                copy(97, SequenceStep, oneshot=True)
            with branch(~RecoveryCounter.Done, ServiceStatus == 0):
                copy(94, SequenceStep, oneshot=True)

    with rung(~SafetyPermit):
        copy(99, SequenceStep, oneshot=True)

    with rung(SequenceStep == 99, SafetyPermit):
        copy(0, SequenceStep, oneshot=True)

    # The field communication transaction shares NetworkAvailable with a
    # peer-ready contact. This relationship is intentionally included even
    # though CommunicationPulse does not directly drive SequenceStep: it is
    # what lets ProgramStep attach communication context to the reconnect act.
    with rung(system.sys.clock_500ms, NetworkPeerReady, NetworkAvailable):
        out(CommunicationPulse)

    with rung(RouteControl == 0, CommunicationPulse):
        out(CommunicationSuccess)

    with rung(CommunicationSuccess):
        copy(10, RouteControl)

    with rung(RouteControl == 10, CommunicationPulse):
        copy(1, RouteEnabled)

    # As in the source program, the simulation override is evaluated after
    # the sequence and error rungs, so its route effect starts next scan.
    with rung(SimulationMode):
        copy(100, RouteControl)

    with rung(~SimulationMode):
        copy(0, RouteControl, oneshot=True)

    # These writers occur after the recovery reads, matching the field
    # program's later communication subroutine. They make the counter/status
    # terms program-owned without turning the recovery chart into action
    # authority.
    with rung(CommunicationError):
        count_up(RecoveryCounter, 60).reset(~CommunicationError)

    with rung():
        copy(ServiceStatusInput, ServiceStatus)

    # The field program copies its live sequence register unconditionally to
    # a later reporting register. This is a one-way observation handoff, not a
    # second navigated channel. Keeping it here ensures Compass preserves that
    # distinction while still seeing the same receipt topology as the field
    # program.
    with rung():
        copy(SequenceStep, ReportedSequenceStep)
