"""Showcase panel: every control type, bound to DBC/dummy_ecu.dbc.

Run it against dummy_ecu.py (or with the Form Designer's "Test panel..." button): the Run switch
starts the simulated engine, the gauge, 7-segment display and trend follow the temperature, the
indicator shows the diagnostic session from the DBC value table, and the output box logs events.
"""


def DatabaseMainFunction(api):
    api.ui.set_value("log", "Panel started - flip Run to start the engine")


# --- Handlers named in the controls' Handler property ---------------------------------------

def on_run_changed(api, value):
    api.can.send(0x200, [1 if value else 2])            # dummy ECU: 01 = start, 02 = stop
    api.ui.set_value("log", f"Engine {'started' if value else 'stopped'}")


def on_logging_changed(api, value):
    api.can.send(0x201, [int(bool(value))])


def on_hello_clicked(api, value):
    vin = RDBI(0xF190)                                   # sends 22 F1 90; multi-frame reply over ISO-TP
    api.ui.set_value("log", f"VIN: {vin.text}" if vin else f"VIN: {vin.error}")


def on_session_changed(api, value):
    sessions = {"Default": 0x01, "Extended": 0x03}
    result = DSC(sessions[value])                        # sends 10 01 or 10 03
    api.ui.set_value("log", f"Session {value}: {'ok' if result else result.error}")


def on_limit_changed(api, value):
    api.ui.set_value("limit_display", value)


# --- CAPL-style event procedures -------------------------------------------------------------

@on_signal("EngineData.Temperature")
def temperature_changed(api, value):
    limit = api.ui.get_value("limit") or 60
    api.ui.set_value("overheat", value > limit)


@on_signal("EcuStatus.Running")
def running_changed(api, value):
    api.ui.set_value("log", f"ECU reports Running = {value}")


@on_timer(5.0)
def heartbeat(api):
    temperature = api.signal("EngineData.Temperature")
    if temperature is not None:
        api.ui.set_value("log", f"Temperature {temperature:.1f} degC")


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
