"""Decode Modbus register words into engineering values.

word_order: order of 16-bit registers for multi-register types ("big" = high word first).
byte_order: order of the two bytes inside each register ("big" = standard Modbus)."""
import struct

REGISTER_COUNT = {"bool": 1, "uint16": 1, "int16": 1, "uint32": 2, "int32": 2, "float32": 2,
                  "uint64": 4, "int64": 4, "float64": 4}
_FMT = {"uint16": ">H", "int16": ">h", "uint32": ">I", "int32": ">i", "float32": ">f",
        "uint64": ">Q", "int64": ">q", "float64": ">d"}


def decode(registers: list[int], data_type: str, word_order: str = "big", byte_order: str = "big") -> float:
    n = REGISTER_COUNT[data_type]
    if len(registers) < n:
        raise ValueError(f"{data_type} needs {n} registers, got {len(registers)}")
    words = list(registers[:n])
    if data_type == "bool":
        return 1.0 if words[0] else 0.0
    if word_order == "little":
        words.reverse()
    raw = b"".join(w.to_bytes(2, "big") if byte_order == "big" else w.to_bytes(2, "little") for w in words)
    return float(struct.unpack(_FMT[data_type], raw)[0])


def encode(value: float, data_type: str, word_order: str = "big", byte_order: str = "big") -> list[int]:
    """Inverse of decode (used by tests and simulators only — never for device writes)."""
    if data_type == "bool":
        return [1 if value else 0]
    fmt = _FMT[data_type]
    raw = struct.pack(fmt, value if "float" in data_type else int(value))
    words = [int.from_bytes(raw[i:i + 2], "big" if byte_order == "big" else "little") for i in range(0, len(raw), 2)]
    if word_order == "little":
        words.reverse()
    return words


def engineering(raw: float, scale: float, offset: float) -> float:
    return raw * scale + offset
