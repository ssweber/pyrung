"""Tests for the static facts that how() steers with."""

from __future__ import annotations

from pyrung.core import (
    Bool,
    Int,
    Program,
    Rung,
    out,
)
from pyrung.core.analysis.prove import Intractable
from pyrung.core.analysis.prove.classify import _classify_dimensions

# ===================================================================
# Simultaneous edge coverage tests
# ===================================================================


class TestReceiveDestAutoND:
    """Receive() destination tags are nondeterministic regardless of external annotation."""

    def test_receive_dest_classified_as_nd_without_external(self):
        from pyrung.core.instruction.send_receive import ModbusTcpTarget, receive

        Enable = Bool("Enable", external=True)
        Dest = Int("Dest", choices={0: "OFF", 1: "ON", 2: "FAULT"})
        Receiving = Bool("Receiving")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")
        Alarm = Bool("Alarm")

        target = ModbusTcpTarget("peer", "127.0.0.1", port=502, device_id=1)

        with Program() as logic:
            with Rung(Enable):
                receive(
                    target=target,
                    remote_start="DS1",
                    dest=Dest,
                    receiving=Receiving,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )
            with Rung(Dest == 2):
                out(Alarm)

        assert not Dest.external

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _sd, nd, _comb, _da, _dp, _dk = result
        assert "Dest" in nd, "Dest should be nondeterministic without external=True"

    def test_receive_dest_matches_explicit_external(self):
        from pyrung.core.instruction.send_receive import ModbusTcpTarget, receive

        Enable = Bool("Enable", external=True)
        Dest = Int("Dest", choices={0: "OFF", 1: "ON", 2: "FAULT"}, external=True)
        Receiving = Bool("Receiving")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")
        Alarm = Bool("Alarm")

        target = ModbusTcpTarget("peer", "127.0.0.1", port=502, device_id=1)

        with Program() as logic:
            with Rung(Enable):
                receive(
                    target=target,
                    remote_start="DS1",
                    dest=Dest,
                    receiving=Receiving,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )
            with Rung(Dest == 2):
                out(Alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _sd, nd, _comb, _da, _dp, _dk = result
        assert "Dest" in nd, "Dest should be nondeterministic with explicit external=True"
