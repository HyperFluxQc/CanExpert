"""Copy alongside the matching XML in Databases and choose family 'example'."""


def DatabaseMainFunction(api):
    api.ui.set_value("status", "Ready")
    api.on("start", lambda value: api.can.send(0x200, [1]))
    api.on("enable", lambda value: api.can.send(0x201, [int(value)]))
    api.on("input", lambda value: api.ui.set_value("status", value))
    api.on_can(lambda can_id, data: api.ui.set_value("rx", f"0x{can_id:X}: {data.hex(' ')}"))


# --- Firmware flashing (ISO 14229-1 UDS over ISO 15765-2) ---------------------------
# Defining Flashing() enables the Flashing toolbar button. It receives the selected
# S-record / Intel HEX file as `firmware` (firmware.path, firmware.size and
# firmware.segments: a list of (address, bytes) in ascending order).
#
# Requests go to the configuration's request/response IDs, which must be the ECU's
# *physical* addresses (e.g. 7E0/7E8): multi-frame requests are not allowed on the
# functional 7DF address. Adjust the constants and compute_key() for your bootloader.

ADDRESS_AND_LENGTH_FORMAT = 0x44       # 4-byte memoryAddress, 4-byte memorySize
DATA_FORMAT = 0x00                     # no compression, no encryption
SECURITY_LEVEL = 0x01                  # requestSeed sub-function; programming level is OEM specific
ERASE_MEMORY_ROUTINE = 0xFF00          # RoutineControl: eraseMemory
CHECK_DEPENDENCIES_ROUTINE = 0xFF01    # RoutineControl: checkProgrammingDependencies

NRC_NAMES = {
    0x10: "generalReject", 0x11: "serviceNotSupported", 0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat", 0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError", 0x31: "requestOutOfRange", 0x33: "securityAccessDenied",
    0x35: "invalidKey", 0x36: "exceededNumberOfAttempts", 0x37: "requiredTimeDelayNotExpired",
    0x70: "uploadDownloadNotAccepted", 0x71: "transferDataSuspended",
    0x72: "generalProgrammingFailure", 0x73: "wrongBlockSequenceCounter",
    0x7E: "subFunctionNotSupportedInActiveSession", 0x7F: "serviceNotSupportedInActiveSession",
}


class FlashingError(Exception):
    pass


def uds(api, step, request, timeout=None):
    """Send one request; return the positive response or raise with the step name and NRC."""
    reply = api.uds.request(bytes(request), timeout)
    if reply is None:
        raise FlashingError(f"{step}: no response from ECU")
    if reply[0] == 0x7F:
        nrc = reply[2] if len(reply) > 2 else 0
        raise FlashingError(f"{step}: negative response 0x{nrc:02X} ({NRC_NAMES.get(nrc, 'unknown')})")
    return reply


def compute_key(seed):
    """Placeholder seed/key algorithm - replace with your ECU's (or call its DLL via api.dll.call)."""
    return bytes(b ^ 0xA5 for b in seed)


def memory(address, size):
    return [*address.to_bytes(ADDRESS_AND_LENGTH_FORMAT & 0x0F, "big"),
            *size.to_bytes(ADDRESS_AND_LENGTH_FORMAT >> 4, "big")]


def check_cancel(api):
    if api.flash_cancelled:
        raise FlashingError("Cancelled by user")


def Flashing(api, firmware):
    total = firmware.size
    api.log(f"Flashing {firmware.path}: {total} bytes in {len(firmware.segments)} segment(s)")

    # 1. Pre-programming: extended session, stop DTC logging and normal communication.
    api.progress(0, total, "Extended diagnostic session")
    uds(api, "DiagnosticSessionControl (extended)", [0x10, 0x03])
    uds(api, "ControlDTCSetting (off)", [0x85, 0x02])
    uds(api, "CommunicationControl (disable normal communication)", [0x28, 0x03, 0x01])

    # 2. Programming session and security unlock.
    api.progress(0, total, "Programming session")
    uds(api, "DiagnosticSessionControl (programming)", [0x10, 0x02])
    seed = uds(api, "SecurityAccess (requestSeed)", [0x27, SECURITY_LEVEL])[2:]
    if any(seed):  # an all-zero seed means the ECU is already unlocked
        uds(api, "SecurityAccess (sendKey)", [0x27, SECURITY_LEVEL + 1, *compute_key(seed)])

    # 3. Erase, download and exit transfer for each contiguous segment.
    written = 0
    for address, data in firmware.segments:
        check_cancel(api)
        api.progress(written, total, f"Erasing 0x{address:08X} ({len(data)} bytes)")
        uds(api, "RoutineControl (eraseMemory)",
            [0x31, 0x01, *ERASE_MEMORY_ROUTINE.to_bytes(2, "big"), ADDRESS_AND_LENGTH_FORMAT,
             *memory(address, len(data))])

        reply = uds(api, "RequestDownload",
                    [0x34, DATA_FORMAT, ADDRESS_AND_LENGTH_FORMAT, *memory(address, len(data))])
        length_size = reply[1] >> 4
        max_block = int.from_bytes(reply[2:2 + length_size], "big") or 4095
        # maxNumberOfBlockLength counts the 0x36 SID and the block counter; ISO-TP caps a message at 4095 bytes.
        chunk_size = max(1, min(max_block, 4095) - 2)

        sequence = 1
        for offset in range(0, len(data), chunk_size):
            check_cancel(api)
            block = data[offset:offset + chunk_size]
            reply = uds(api, f"TransferData (block {sequence})", [0x36, sequence, *block])
            if len(reply) < 2 or reply[1] != sequence:
                raise FlashingError(f"TransferData: ECU acknowledged block {reply[1:2].hex()} instead of {sequence:02x}")
            sequence = (sequence + 1) & 0xFF  # wraps 0xFF -> 0x00
            written += len(block)
            api.progress(written, total, f"Writing 0x{address + offset:08X}")

        uds(api, "RequestTransferExit", [0x37])

    # 4. Post-programming: verify, then reset into the new application.
    api.progress(written, total, "Checking programming dependencies")
    reply = uds(api, "RoutineControl (checkProgrammingDependencies)",
                [0x31, 0x01, *CHECK_DEPENDENCIES_ROUTINE.to_bytes(2, "big")])
    if len(reply) > 4 and reply[4] != 0x00:
        raise FlashingError(f"checkProgrammingDependencies failed (status 0x{reply[4]:02X})")
    api.progress(written, total, "Resetting ECU")
    uds(api, "ECUReset (hardReset)", [0x11, 0x01])
    api.log("Flashing complete")
    return True
