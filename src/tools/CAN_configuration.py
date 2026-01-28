from typing import Any, DefaultDict, Dict, Iterable, Optional, Tuple, Union, List

from src.common.server import mcp

import re


CAN_transceiver_configuration: Dict[int, Dict[str, Any]] = {
    0: {"baudrate": 125, "identifier_type": "standard", "identifier_bits": 11, "label": "125 kbps & 11-bit"},
    1: {"baudrate": 125, "identifier_type": "extended", "identifier_bits": 29, "label": "125 kbps & 29-bit"},
    2: {"baudrate": 250, "identifier_type": "standard", "identifier_bits": 11, "label": "250 kbps & 11-bit"},
    3: {"baudrate": 250, "identifier_type": "extended", "identifier_bits": 29, "label": "250 kbps & 29-bit"},
    4: {"baudrate": 500, "identifier_type": "standard", "identifier_bits": 11, "label": "500 kbps & 11-bit"},
    5: {"baudrate": 500, "identifier_type": "extended", "identifier_bits": 29, "label": "500 kbps & 29-bit"},
}


CAN_configuration_string_regex = re.compile(r"^\s*\$CNCF,(?P<body>.*)\#\s*$", re.IGNORECASE)


def _parse_int(s: str) -> int:
    s = s.strip()
    if not s:
        raise ValueError("Empty integer field")
    if s.lower().startswith("0x"):
        return int(s, 16)
    if re.search(r"[a-fA-F]", s):
        return int(s, 16)
    return int(s, 10)


def _format_CAN_identifier(i: int) -> str:
    return format(i, "X")


def _is_CAN_identifier_valid(i: int, identifier_bits: int) -> bool:
    if i < 0:
        return False
    if identifier_bits == 11:
        return i <= 0x7FF
    if identifier_bits == 29:
        return i <= 0x1FFFFFFF
    return False


def _parse_CAN_identifier_list(field: str) -> List[int]:
    """
    Parse a bracket list: [18F09001, 18C04031] -> [0x18F09001, 0x18C04031]
    Accepts whitespace, optional 0x, and empty list [].
    """
    field = field.strip()
    if not (field.startswith("[") and field.endswith("]")):
        raise ValueError(f"Expected bracket list like [..], got: {field!r}")

    inner = field[1:-1].strip()
    if inner == "":
        return []

    parts = [part.strip() for part in inner.split(",")]
    identifiers: List[int] = []
    for part in parts:
        if not part:
            continue
        identifiers.append(_parse_int(part))
    return identifiers


def _split_CAN_configuration_fields(body: str) -> List[str]:
    """
    Split CAN configuration body by commas but keep bracket-lists intact.
    Example body:
      3,2,1,0,2,0,[18F09001,18C04031],[18F09001,18C04031]
    """
    fields: List[str] = []
    cursor: List[str] = []
    depth = 0
    for character in body:
        if character == "[":
            depth += 1
            cursor.append(character)
        elif character == "]":
            depth = max(0, depth - 1)
            cursor.append(character)
        elif character == "," and depth == 0:
            fields.append("".join(cursor).strip())
            cursor = []
        else:
            cursor.append(character)
    if cursor:
        fields.append("".join(cursor).strip())
    return fields


def _decode_CAN_configuration(command: str) -> Dict[str, Any]:
    m = CAN_configuration_string_regex.match(command)
    if not m:
        raise ValueError("Command must look like: $CNCF,<fields...>#")

    body = m.group("body").strip()
    fields = _split_CAN_configuration_fields(body)

    if len(fields) not in (7, 8, 9):
        raise ValueError(
            f"Unexpected field count after $CNCF,: got {len(fields)} fields: {fields}"
        )

    my_CAN_transceiver_configuration_encoded = _parse_int(fields[0])
    count_of_CAN_identifiers_RX = _parse_int(fields[1])
    request = _parse_int(fields[2])
    remote_request = _parse_int(fields[3])
    count_of_CAN_identifiers_TX = _parse_int(fields[4])
    TX_DLC = _parse_int(fields[5])

    list_of_CAN_identifiers_RX: List[int] = []
    list_of_CAN_identifiers_TX: List[int] = []

    # Cases:
    # - len==7: has only [Rx]
    # - len==8: likely [Rx],[Tx]
    # - len==9: might be because of an extra empty field, but we’ll interpret last two as lists
    if len(fields) == 7:
        list_of_CAN_identifiers_RX = _parse_CAN_identifier_list(fields[6])
        list_of_CAN_identifiers_TX = []
    else:
        list_fields = [f for f in fields[6:] if f.strip().startswith("[") and f.strip().endswith("]")]
        if len(list_fields) == 1:
            list_of_CAN_identifiers_RX = _parse_CAN_identifier_list(list_fields[0])
            list_of_CAN_identifiers_TX = []
        elif len(list_fields) >= 2:
            list_of_CAN_identifiers_RX = _parse_CAN_identifier_list(list_fields[0])
            list_of_CAN_identifiers_TX = _parse_CAN_identifier_list(list_fields[1])
        else:
            raise ValueError("Could not find [Rx] / [Tx] bracket lists in command")

    my_CAN_transceiver_configuration_decoded = CAN_transceiver_configuration.get(my_CAN_transceiver_configuration_encoded)
    if not my_CAN_transceiver_configuration_decoded:
        raise ValueError(f"Unknown code: {my_CAN_transceiver_configuration_encoded}")

    identifier_bits = int(my_CAN_transceiver_configuration_decoded["identifier_bits"])
    identifier_type = str(my_CAN_transceiver_configuration_decoded["identifier_type"])

    # Semantics: RxCount == 0 and Rx CAN identifiers contains 0 => receive all
    receive_all = (count_of_CAN_identifiers_RX == 0) or (len(list_of_CAN_identifiers_RX) == 1 and list_of_CAN_identifiers_RX[0] == 0) or (0 in list_of_CAN_identifiers_RX and count_of_CAN_identifiers_RX == 0)
    request_enabled = request == 1
    remote_request_enabled = remote_request == 1

    # Semantics: TxCount == 0 OR Tx CAN identifiers contains 0 => no request / none
    no_request = (not request_enabled) or (count_of_CAN_identifiers_TX == 0) or (len(list_of_CAN_identifiers_TX) == 0) or (len(list_of_CAN_identifiers_TX) == 1 and list_of_CAN_identifiers_TX[0] == 0)

    # Validate ranges for IDs based on standard/extended
    warnings_for_CAN_identifiers: List[str] = []
    if not receive_all:
        for i in list_of_CAN_identifiers_RX:
            if i == 0:
                continue
            if not _is_CAN_identifier_valid(i, identifier_bits):
                warnings_for_CAN_identifiers.append(f"Rx CAN identifier {_format_CAN_identifier(i)} out of range for {identifier_bits}-bit ({identifier_type})")
    if not no_request:
        for i in list_of_CAN_identifiers_TX:
            if i == 0:
                continue
            if not _is_CAN_identifier_valid(i, identifier_bits):
                warnings_for_CAN_identifiers.append(f"Tx CAN identifier {_format_CAN_identifier(i)} out of range for {identifier_bits}-bit ({identifier_type})")

    count_warnings: List[str] = []
    if not receive_all and count_of_CAN_identifiers_RX not in (len(list_of_CAN_identifiers_RX), 0):
        count_warnings.append(f"RxCount={count_of_CAN_identifiers_RX} but parsed {len(list_of_CAN_identifiers_RX)} Rx CAN identifiers")
    if request_enabled and not no_request and count_of_CAN_identifiers_TX not in (len(list_of_CAN_identifiers_TX), 0):
        count_warnings.append(f"TxCount={count_of_CAN_identifiers_TX} but parsed {len(list_of_CAN_identifiers_TX)} Tx CAN identifiers")

    warnings_for_DLC: Optional[str] = None
    if TX_DLC < 0:
        warnings_for_DLC = "TxDLC is negative (invalid)"
    elif TX_DLC > 8:
        warnings_for_DLC = "TxDLC > 8 (might indicate CAN-FD or a device-specific meaning)"

    decoded: Dict[str, Any] = {
        "command_type": "CNCF",
        "raw": command.strip(),

        "my_CAN_transceiver_configuration_encoded": {
            "code": my_CAN_transceiver_configuration_encoded,
            "baudrate": my_CAN_transceiver_configuration_decoded["baudrate"],
            "identifier_type": identifier_type,
            "identifier_bits": identifier_bits,
            "label": my_CAN_transceiver_configuration_decoded["label"],
        },

        "receive": {
            "count_of_CAN_identifiers_RX": count_of_CAN_identifiers_RX,
            "receive_all": receive_all,
            "list_of_CAN_identifiers_RX": [] if receive_all else list_of_CAN_identifiers_RX,
            "which_CAN_identifiers": [] if receive_all else [_format_CAN_identifier(i) for i in list_of_CAN_identifiers_RX],
        },

        "transmit": {
            "request_enable": request_enabled,
            "remote_request_enable": remote_request_enabled,
            "count_of_CAN_identifiers_TX": count_of_CAN_identifiers_TX,
            "TX_DLC": TX_DLC,
            "no_request": no_request,
            "list_of_CAN_identifiers_TX": [] if no_request else list_of_CAN_identifiers_TX,
            "which_CAN_identifiers": [] if no_request else [_format_CAN_identifier(i) for i in list_of_CAN_identifiers_TX],
        },

        "warnings": {
            "warnings_for_CAN_identifiers": warnings_for_CAN_identifiers,
            "count_mismatch": count_warnings,
            "warnings_for_DLC": warnings_for_DLC,
        },

        "explanation": {
            "summary": (
                f"{my_CAN_transceiver_configuration_decoded['label']} ({identifier_type}); "
                f"{'receive all IDs' if receive_all else f'receive {len(list_of_CAN_identifiers_RX)} ID(s)'}; "
                f"{'no request' if no_request else f'request {len(list_of_CAN_identifiers_TX)} ID(s)'}; "
                f"remote_request={'on' if remote_request_enabled else 'off'}; "
                f"TX_DLC={TX_DLC}"
            ),
            "notes": [
                "Rx Count==0 or Rx CAN identifiers contains 0 typically means 'receive all CAN IDs'.",
                "Tx Count==0 or Tx CAN identifiers [0] typically means 'no request IDs configured'.",
                "CAN identifiers are interpreted as hex if they contain A-F (or if prefixed with 0x); otherwise decimal.",
            ],
        },
    }

    return decoded


@mcp.tool()
async def explain__CAN_configuration(command: str) -> dict:
    """
    Explain a CAN configuration by decoding the CAN transceiver baudrate, standard/extended setting, DLC, count and list of identifiers to receive, and count and list of identifiers to transmit, e.g. 'explain the CAN configuration $CNCF,3,13,1,0,9,8,[18904001,18914001,18924001,18934001,18944001,18964001,18974001,18984001,1026105A,10261101,10262001,10261022,10261023],[18900140,18910140,18920140,18930140,18940140,18950140,18960140,18970140,18980140]#'
    """
    m = re.search(r"(\$CNCF,.*?\#)", command, flags=re.IGNORECASE | re.DOTALL)
    payload = m.group(1).strip() if m else command.strip()

    try:
        return _decode_CAN_configuration(payload)
    except Exception as e:
        return {
            "command_type": "CNCF",
            "raw": command.strip(),
            "error": str(e),
            "expected_format": "$CNCF,<CAN Transceiver Configuration>,<Count of CAN identifiers RX>,<Request Enable>,<Remote Request Enable>,<Count of CAN identifiers TX>,<DLC for TX>,[Rx CAN identifiers],[Tx CAN identifiers]#",
            "CAN_transceiver_configuration": CAN_transceiver_configuration,
            "examples": [
                "$CNCF,3,2,1,0,2,0,[18F09001,18C04031],[18F09001,18C04031]#",
                "$CNCF,3,2,0,0,0,0,[18F09001,18C04031]#",
                "$CNCF,3,0,1,0,3,8,[0],[18F09001,18C04031,18FC1456]#",
                "$CNCF,4,0,0,0,0,0,[0],[0]#",
            ],
        }
