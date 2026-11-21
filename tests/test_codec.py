import math
import pytest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))
from codec import decode, encode  # noqa: E402

CASES = [("uint16", 65535), ("int16", -1234), ("uint32", 4_000_000_000), ("int32", -2_000_000_000),
         ("float32", 3.5), ("uint64", 2**63 + 5), ("int64", -(2**62)), ("float64", math.pi), ("bool", 1)]


@pytest.mark.parametrize("dtype,value", CASES)
@pytest.mark.parametrize("wo", ["big", "little"])
@pytest.mark.parametrize("bo", ["big", "little"])
def test_roundtrip(dtype, value, wo, bo):
    assert decode(encode(value, dtype, wo, bo), dtype, wo, bo) == pytest.approx(float(value))


def test_known_vectors():
    # 123.456 float32 = 0x42F6E979 (ABCD)
    assert decode([0x42F6, 0xE979], "float32") == pytest.approx(123.456, rel=1e-6)
    assert decode([0xE979, 0x42F6], "float32", word_order="little") == pytest.approx(123.456, rel=1e-6)  # CDAB
    assert decode([0xF642, 0x79E9], "float32", byte_order="little") == pytest.approx(123.456, rel=1e-6)  # BADC
    assert decode([0xFFFF], "int16") == -1
    with pytest.raises(ValueError):
        decode([1], "uint32")
