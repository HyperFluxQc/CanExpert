"""Copy alongside the matching XML in Databases and choose family 'example'."""


def DatabaseMainFunction(api):
    api.ui.set_value("status", "Ready")
    api.on("start", lambda value: api.can.send(0x200, [1]))
    api.on("enable", lambda value: api.can.send(0x201, [int(value)]))
    api.on("input", lambda value: api.ui.set_value("status", value))
    api.on_can(lambda can_id, data: api.ui.set_value("rx", f"0x{can_id:X}: {data.hex(' ')}"))


# --- Firmware flashing (ISO 14229-1) ---------------------------------------------------------
# Defining Flashing() enables the Flashing toolbar button (and Flashing... in the Form Designer's
# Test panel). The user picks an S-record or Intel HEX file (examples/firmware/demo_app.s19 or .hex),
# confirms, and Flashing(api, firmware) runs with firmware.path, firmware.size and
# firmware.segments = [(address, bytes), ...].
#
# The UDS functions (DSC, SecurityUnlock, RD, TD, ...) use the configuration's request/response IDs,
# which must be the ECU's physical addresses (e.g. 7E0/7E8). Adjust compute_key() and the routine
# identifiers for your bootloader; this sequence matches dummy_ecu.py.

ERASE_MEMORY = 0xFF00                   # RoutineControl: eraseMemory
CHECK_PROGRAMMING_DEPENDENCIES = 0xFF01


def compute_key(seed):
    """Placeholder seed/key algorithm (the dummy ECU's) - replace with your ECU's or call its DLL."""
    return bytes(b ^ 0xA5 for b in seed)


def step(result, what):
    """Stop the sequence with a clear message when a request fails."""
    if not result:
        raise RuntimeError(f"{what}: {result.error}")
    return result


def Flashing(api, firmware):
    total, written = firmware.size, 0

    # 1. Pre-programming: extended session, DTC setting off, normal communication off.
    api.progress(0, total, "Pre-programming")
    step(DSC(0x03), "DiagnosticSessionControl (extended)")
    step(CDTCS(0x02), "ControlDTCSetting (off)")
    step(CC(0x03, 0x01), "CommunicationControl (disable normal communication)")

    # 2. Programming session and security unlock.
    step(DSC(0x02), "DiagnosticSessionControl (programming)")
    step(SecurityUnlock(0x01, compute_key), "SecurityAccess")

    # 3. Per segment: erase, RequestDownload, TransferData blocks, RequestTransferExit.
    for address, data in firmware.segments:
        memory = [0x44, *address.to_bytes(4, "big"), *len(data).to_bytes(4, "big")]
        api.progress(written, total, f"Erasing 0x{address:08X}")
        step(StartRoutine(ERASE_MEMORY, memory), "RoutineControl (eraseMemory)")
        download = step(RD(address, len(data)), "RequestDownload")
        # maxNumberOfBlockLength counts the 0x36 SID and the block counter. Blocks stay within 4095 bytes,
        # the longest message ISO-TP carries without the 2016 escape sequence older bootloaders lack.
        block = min(download.max_block_length or 4095, 4095) - 2
        for counter, offset in enumerate(range(0, len(data), block), start=1):
            if api.flash_cancelled:
                raise RuntimeError("Cancelled by user")
            chunk = data[offset:offset + block]
            step(TD(counter, chunk), f"TransferData (block {counter})")    # counter wraps 0xFF -> 0x00
            written += len(chunk)
            api.progress(written, total, f"Writing 0x{address + offset:08X}")
        step(RTE(), "RequestTransferExit")

    # 4. Post-programming: verify, then reset into the new application.
    api.progress(written, total, "Checking programming dependencies")
    check = step(StartRoutine(CHECK_PROGRAMMING_DEPENDENCIES), "checkProgrammingDependencies")
    if check.data[:1] not in (b"", b"\x00"):
        raise RuntimeError(f"checkProgrammingDependencies failed (status 0x{check.data[0]:02X})")
    api.progress(written, total, "Resetting ECU")
    step(ER(0x01), "ECUReset (hardReset)")
    api.sleep(1.0)                                         # let the ECU start the new application
    version = RDBI(0xF195)
    api.log(f"Flashing complete, software version: {version.text if version else version.error}")
    return True
