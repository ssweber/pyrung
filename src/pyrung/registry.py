"""Central registry for PLC simulator to avoid circular imports."""

# Store the current PLC instance
_current_plc = None


def register_current_plc(plc):
    """Register the current PLC instance globally"""
    global _current_plc
    _current_plc = plc


def get_current_plc():
    """Get the current PLC instance"""
    return _current_plc
