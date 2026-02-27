# Drum instructions hold state when not enabled (rung false = pause, not reset).
# time_drum behaves like a retentive timer — accumulator holds its value.
# event_drum behaves like latch/reset — outputs stay in their last state.
# Use .reset() to explicitly return to step 1, clear accumulator, and turn off outputs.
#
# Both drums activate step 1 outputs immediately when enabled.
# Events and presets are exit conditions — they end the current step, not start it.

with Rung(condition):
    event_drum(
        outputs=[Y001, Y002, Y003, Y004],
        events=[C11, C12, C13, C14],
        pattern=[
            [1, 0, 1, 0],
            [0, 1, 1, 0],
            [0, 0, 0, 1],
            [1, 1, 0, 1],
        ],
        current_step=DS1,
        completion_flag=C8,
    ).reset(X002).jump(condition=X003, step=DS2).jog(X004)

with Rung(condition):
    time_drum(
        outputs=[Y001, Y002, Y003, Y004],
        presets=[500, DS11, 200, DS13],
        unit=Tms,
        pattern=[
            [1, 0, 1, 0],
            [0, 1, 1, 0],
            [0, 0, 0, 1],
            [1, 1, 0, 1],
        ],
        current_step=DS1,
        accumulator=DS2,
        completion_flag=C8,
    ).reset(X002).jump(condition=X003, step=DS2).jog(X004)