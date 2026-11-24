"""Device templates: register maps for common inexpensive Modbus meters.

Register maps are taken from the manufacturers' published Modbus documents.
Status: "문서 확인" (documented) — not yet verified against a physical unit in this project."""

TEMPLATES = {
    "pzem016": {
        "name": "Peacefair PZEM-016 (단상 AC 100A, Modbus RTU)",
        "protocol": "modbus_rtu", "baudrate": 9600, "unit_id": 1,
        "notes": "9600bps 8N1, 기본 주소 1, 입력 레지스터(FC4), 32비트 값은 하위 워드가 먼저(little word order)",
        "verification": "문서 확인 (실장비 미검증)",
        "tags": [
            {"name": "전압", "category": "voltage", "function_code": 4, "address": 0, "data_type": "uint16", "scale": 0.1, "unit": "V"},
            {"name": "전류", "category": "current", "function_code": 4, "address": 1, "data_type": "uint32", "word_order": "little", "scale": 0.001, "unit": "A"},
            {"name": "유효전력", "category": "power", "function_code": 4, "address": 3, "data_type": "uint32", "word_order": "little", "scale": 0.0001, "unit": "kW"},
            {"name": "누적전력량", "category": "energy", "function_code": 4, "address": 5, "data_type": "uint32", "word_order": "little", "scale": 0.001, "unit": "kWh"},
            {"name": "주파수", "category": "frequency", "function_code": 4, "address": 7, "data_type": "uint16", "scale": 0.1, "unit": "Hz"},
            {"name": "역률", "category": "pf", "function_code": 4, "address": 8, "data_type": "uint16", "scale": 0.01, "unit": ""},
        ],
    },
    "sdm120": {
        "name": "Eastron SDM120-Modbus (단상 45A, Modbus RTU)",
        "protocol": "modbus_rtu", "baudrate": 2400, "unit_id": 1,
        "notes": "공장 기본 2400bps 8N1, 주소 1, 입력 레지스터(FC4), IEEE754 float32 big-endian",
        "verification": "문서 확인 (실장비 미검증)",
        "tags": [
            {"name": "전압", "category": "voltage", "function_code": 4, "address": 0x0000, "data_type": "float32", "unit": "V"},
            {"name": "전류", "category": "current", "function_code": 4, "address": 0x0006, "data_type": "float32", "unit": "A"},
            {"name": "유효전력", "category": "power", "function_code": 4, "address": 0x000C, "data_type": "float32", "scale": 0.001, "unit": "kW"},
            {"name": "역률", "category": "pf", "function_code": 4, "address": 0x001E, "data_type": "float32", "unit": ""},
            {"name": "주파수", "category": "frequency", "function_code": 4, "address": 0x0046, "data_type": "float32", "unit": "Hz"},
            {"name": "누적전력량", "category": "energy", "function_code": 4, "address": 0x0156, "data_type": "float32", "unit": "kWh"},
        ],
    },
    "sim_meter": {
        "name": "가상 전력계 (시뮬레이터)", "protocol": "simulator", "verification": "시뮬레이터",
        "notes": "실제 장비 없이 화면·경보를 시험하기 위한 가상 장치",
        "tags": [
            {"name": "전압", "category": "voltage", "unit": "V"},
            {"name": "전류", "category": "current", "unit": "A"},
            {"name": "유효전력", "category": "power", "unit": "kW"},
            {"name": "누적전력량", "category": "energy", "unit": "kWh"},
            {"name": "역률", "category": "pf", "unit": ""},
            {"name": "주파수", "category": "frequency", "unit": "Hz"},
        ],
    },
}


def tag_defaults(t: dict) -> dict:
    return {"function_code": t.get("function_code", 3), "address": t.get("address", 0),
            "data_type": t.get("data_type", "float32"), "word_order": t.get("word_order", "big"),
            "byte_order": t.get("byte_order", "big"), "scale": t.get("scale", 1.0), "offset": t.get("offset", 0.0),
            "unit": t.get("unit", ""), "category": t.get("category", "other"), "name": t["name"]}
