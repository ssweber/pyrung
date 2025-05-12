# clickplc_dsl.py
from typing import Any, Callable, List, Optional, Tuple, Union
from enum import Enum


class Td:
    """Timer day unit"""

    pass


class Th:
    """Timer hour unit"""

    pass


class Tm:
    """Timer minute unit"""

    pass


class Ts:
    """Timer second unit"""

    pass


class Tms:
    """Timer millisecond unit"""

    pass


class AddressBase:
    def __getitem__(self, key):
        if isinstance(key, slice):
            start = key.start if key.start is not None else ""
            stop = key.stop if key.stop is not None else ""
            return Address(f"{self.prefix}[{start}:{stop}]")
        return Address(f"{self.prefix}[{key}]")

    def __getattr__(self, name):
        return Address(f"{self.prefix}.{name}")


class Address:
    def __init__(self, name: str):
        self.name = name

    def __str__(self):
        return self.name

    def __repr__(self):
        return self.name

    def __add__(self, other):
        return Expression(
            f"{self.name} + {other.name if hasattr(other, 'name') else other}"
        )

    def __sub__(self, other):
        return Expression(
            f"{self.name} - {other.name if hasattr(other, 'name') else other}"
        )

    def __mul__(self, other):
        return Expression(
            f"{self.name} * {other.name if hasattr(other, 'name') else other}"
        )

    def __truediv__(self, other):
        return Expression(
            f"{self.name} / {other.name if hasattr(other, 'name') else other}"
        )


class Expression:
    def __init__(self, expr: str):
        self.expr = expr

    def __str__(self):
        return self.expr


class Addresses:
    @staticmethod
    def get():
        x = AddressBase()
        x.prefix = "x"

        y = AddressBase()
        y.prefix = "y"

        c = AddressBase()
        c.prefix = "c"

        t = AddressBase()
        t.prefix = "t"

        ct = AddressBase()
        ct.prefix = "ct"

        sc = AddressBase()
        sc.prefix = "sc"

        ds = AddressBase()
        ds.prefix = "ds"

        dd = AddressBase()
        dd.prefix = "dd"

        dh = AddressBase()
        dh.prefix = "dh"

        df = AddressBase()
        df.prefix = "df"

        xd = AddressBase()
        xd.prefix = "xd"

        yd = AddressBase()
        yd.prefix = "yd"

        td = AddressBase()
        td.prefix = "td"

        ctd = AddressBase()
        ctd.prefix = "ctd"

        sd = AddressBase()
        sd.prefix = "sd"

        txt = AddressBase()
        txt.prefix = "txt"

        return x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, td, ctd, sd, txt


class Conditions:
    @staticmethod
    def get():
        def nc(address):
            """Normally Closed Contact"""
            return f"nc({address})"

        def re(address):
            """Rising Edge Contact"""
            return f"re({address})"

        def fe(address):
            """Falling Edge Contact"""
            return f"fe({address})"

        def all(*args):
            """Series connection (AND logic)"""
            return f"all({', '.join(str(arg) for arg in args)})"

        def any(*args):
            """Parallel branches (OR logic)"""
            return f"any({', '.join(str(arg) for arg in args)})"

        return nc, re, fe, all, any


class Actions:
    @staticmethod
    def get():
        def out(address, oneshot=False):
            """Output Coil"""
            if oneshot:
                return f"out({address}, oneshot=True)"
            return f"out({address})"

        def set(address):
            """Set Coil"""
            return f"set({address})"

        def reset(address):
            """Reset Coil"""
            return f"reset({address})"

        def ton(timer, setpoint, unit):
            """Timer On Delay"""
            return f"ton({timer}, setpoint={setpoint}, unit={unit.__class__.__name__})"

        def tof(timer, setpoint, unit):
            """Timer Off Delay"""
            return f"tof({timer}, setpoint={setpoint}, unit={unit.__class__.__name__})"

        def rton(timer, setpoint, unit, reset=None):
            """Retentive Timer On with reset condition"""
            if reset:
                return f"rton({timer}, setpoint={setpoint}, unit={unit.__class__.__name__}, reset={reset})"
            return f"rton({timer}, setpoint={setpoint}, unit={unit.__class__.__name__})"

        def rtof(timer, setpoint, unit, reset=None):
            """Retentive Timer Off with reset condition"""
            if reset:
                return f"rtof({timer}, setpoint={setpoint}, unit={unit.__class__.__name__}, reset={reset})"
            return f"rtof({timer}, setpoint={setpoint}, unit={unit.__class__.__name__})"

        def ctu(counter, setpoint, reset=None):
            """Count Up"""
            if reset:
                return f"ctu({counter}, setpoint={setpoint}, reset={reset})"
            return f"ctu({counter}, setpoint={setpoint})"

        def ctd(counter, setpoint, reset=None):
            """Count Down"""
            if reset:
                return f"ctd({counter}, setpoint={setpoint}, reset={reset})"
            return f"ctd({counter}, setpoint={setpoint})"

        def ctud(counter, setpoint, count_down=None):
            """Count Up/Down"""
            if count_down:
                return f"ctud({counter}, setpoint={setpoint}, count_down={count_down})"
            return f"ctud({counter}, setpoint={setpoint})"

        def copy(source, destination, oneshot=False, options=None):
            """Copies a single value from source to destination"""
            if oneshot and options:
                return f"copy({source}, {destination}, oneshot=True, options={options})"
            elif oneshot:
                return f"copy({source}, {destination}, oneshot=True)"
            elif options:
                return f"copy({source}, {destination}, options={options})"
            return f"copy({source}, {destination})"

        def copy_block(source_range, destination_start, oneshot=False, options=None):
            """Copies a range of sequential addresses to another range"""
            if oneshot and options:
                return f"copy_block({source_range}, {destination_start}, oneshot=True, options={options})"
            elif oneshot:
                return f"copy_block({source_range}, {destination_start}, oneshot=True)"
            elif options:
                return f"copy_block({source_range}, {destination_start}, options={options})"
            return f"copy_block({source_range}, {destination_start})"

        def copy_fill(source, destination_range, oneshot=False, options=None):
            """Copies a single value to multiple sequential addresses"""
            if oneshot and options:
                return f"copy_fill({source}, {destination_range}, oneshot=True, options={options})"
            elif oneshot:
                return f"copy_fill({source}, {destination_range}, oneshot=True)"
            elif options:
                return f"copy_fill({source}, {destination_range}, options={options})"
            return f"copy_fill({source}, {destination_range})"

        def copy_pack(source_range, destination, oneshot=False, options=None):
            """Combines data from multiple source addresses into one destination"""
            if oneshot and options:
                return f"copy_pack({source_range}, {destination}, oneshot=True, options={options})"
            elif oneshot:
                return f"copy_pack({source_range}, {destination}, oneshot=True)"
            elif options:
                return f"copy_pack({source_range}, {destination}, options={options})"
            return f"copy_pack({source_range}, {destination})"

        def copy_unpack(source, destination_range, oneshot=False, options=None):
            """Separates data from a single source register into multiple destinations"""
            if oneshot and options:
                return f"copy_unpack({source}, {destination_range}, oneshot=True, options={options})"
            elif oneshot:
                return f"copy_unpack({source}, {destination_range}, oneshot=True)"
            elif options:
                return f"copy_unpack({source}, {destination_range}, options={options})"
            return f"copy_unpack({source}, {destination_range})"

        def shift(range_address):
            """Shift Data"""
            return f"shift({range_address})"

        def search(
            condition,
            search_value,
            start_address,
            end_address,
            result_address,
            result_flag,
            continuous=False,
            one_shot=False,
        ):
            """Search instruction"""
            params = [
                f'"{condition}"',
                str(search_value),
                str(start_address),
                str(end_address),
                str(result_address),
                str(result_flag),
            ]

            if continuous or one_shot:
                params.append(f"continuous={continuous}")
            if one_shot:
                params.append(f"one_shot={one_shot}")

            return f"search({', '.join(params)})"

        def math_decimal(formula, result_destination, one_shot=False):
            """Decimal math operation"""
            if one_shot:
                return f'math_decimal("{formula}", {result_destination}, one_shot=True)'
            return f'math_decimal("{formula}", {result_destination})'

        def math_hex(formula, result_destination, one_shot=False):
            """Hex math operation"""
            if one_shot:
                return f'math_hex("{formula}", {result_destination}, one_shot=True)'
            return f'math_hex("{formula}", {result_destination})'

        def call(subroutine_name):
            """Call a subroutine"""
            return f"call({subroutine_name.__name__})"

        def next_loop():
            """Next instruction for For-Next loop"""
            return "next_loop()"

        def end():
            """End instruction for program termination"""
            return "end()"

        return (
            out,
            set,
            reset,
            ton,
            tof,
            rton,
            rtof,
            ctu,
            ctd,
            ctud,
            copy,
            copy_block,
            copy_fill,
            copy_pack,
            copy_unpack,
            shift,
            search,
            math_decimal,
            math_hex,
            call,
            next_loop,
            end,
        )


def sub(func):
    """Decorator for subroutines"""

    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    return wrapper
