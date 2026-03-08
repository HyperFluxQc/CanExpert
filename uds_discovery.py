"""
UDS Discovery - Sends UDS command on CAN and parses response to get application database ID.
"""
import time
import can


def parse_hex_payload(hex_str: str) -> list:
    """Parse hex string like '03 22 F1 80' into list of integers."""
    if isinstance(hex_str, list):
        return [int(x) for x in hex_str]
    return [int(x, 16) for x in hex_str.replace(",", " ").split() if x.strip()]


def extract_database_id(response_data: list, config: dict) -> str | None:
    """
    Extract database ID from UDS response based on connection database config.
    Returns string like '1001' or None if parsing fails.
    """
    parsing = config.get("response_parsing", {})
    byte_indices = parsing.get("database_id_bytes", [4, 5])
    fmt = parsing.get("format", "decimal")
    byte_order = parsing.get("byte_order", "big")

    try:
        if len(response_data) < max(byte_indices) + 1:
            return None

        if len(byte_indices) == 1:
            raw = response_data[byte_indices[0]]
        else:
            bytes_slice = [response_data[i] for i in byte_indices if i < len(response_data)]
            if byte_order == "little":
                bytes_slice.reverse()
            raw = sum(b << (8 * (len(bytes_slice) - 1 - i)) for i, b in enumerate(bytes_slice))

        if fmt == "decimal":
            return str(raw)
        elif fmt == "hex":
            return f"{raw:04X}"
        elif fmt == "bcd":
            high, low = (raw >> 8) & 0xFF, raw & 0xFF
            high_dec = (high >> 4) * 10 + (high & 0xF)
            low_dec = (low >> 4) * 10 + (low & 0xF)
            return str(high_dec * 100 + low_dec)
        elif fmt == "packed":
            result = ""
            for i in byte_indices:
                if i < len(response_data):
                    b = response_data[i]
                    result += f"{(b >> 4) * 10 + (b & 0xF):02d}"
            return result.lstrip("0") or "0"
        return str(raw)
    except (IndexError, KeyError, TypeError):
        return None


def build_uds_payload_from_did(did: int) -> bytes:
    """Build UDS ReadDataByIdentifier (0x22) payload: length=3, service=0x22, DID 2 bytes."""
    return bytes([3, 0x22, (did >> 8) & 0xFF, did & 0xFF])


def send_uds_and_wait_response(
    bus,
    connection_db: dict,
    *,
    did: int | None = None,
    timeout_seconds: float | None = None,
    identifier_11_bit: bool = True,
    extended_id_uds: bool = False,
    extended_id_byte: int | None = None,
    request_id: int | None = None,
) -> str | None:
    """
    Send UDS ReadDataByIdentifier and wait for response. Returns database ID string or None.
    If did is provided, payload is built as 03 22 [DID_hi] [DID_lo]; otherwise uses connection_db payload.
    timeout_seconds overrides connection_db. identifier_11_bit: False = 29-bit CAN ID.
    extended_id_uds: when True, first data byte is the extended identifier (prepended to payload).
    extended_id_byte: when extended_id_uds is True, this byte is prepended to every UDS request payload (0-255).
    request_id: if provided (e.g. from configuration), use as CAN arbitration ID for the request; else use connection_db.
    """
    req = connection_db.get("uds_request", {})
    if request_id is None:
        request_id = req.get("request_id", 0x7DF)
    response_id = req.get("response_id", 0x7E8)
    timeout = timeout_seconds if timeout_seconds is not None else req.get("timeout_seconds", 2.0)
    if timeout >= 1000:
        timeout = timeout / 1000.0

    if did is not None:
        payload_bytes = build_uds_payload_from_did(did)
    else:
        payload = req.get("payload")
        if payload is None:
            payload = parse_hex_payload(req.get("payload_hex", "03 22 F1 80"))
        payload_bytes = bytes(payload) if isinstance(payload[0], int) else bytes([int(x, 16) for x in payload])

    if extended_id_uds and extended_id_byte is not None:
        payload_bytes = bytes([extended_id_byte & 0xFF]) + payload_bytes

    is_extended = not identifier_11_bit
    msg = can.Message(arbitration_id=request_id, data=payload_bytes, is_extended_id=is_extended)
    bus.send(msg)

    deadline = time.time() + timeout
    while time.time() < deadline:
        recv = bus.recv(timeout=0.1)
        if recv and recv.dlc > 0:
            if recv.arbitration_id != response_id:
                if is_extended and extended_id_uds and len(recv.data) >= 1:
                    pass
                continue
            data = list(recv.data)
            db_id = extract_database_id(data, connection_db)
            if db_id:
                return db_id
            if len(recv.data) >= 6:
                return str(int.from_bytes(recv.data[4:6], "big"))
    return None
