"""Showcase panel: every control type, bound to DBC/dummy_ecu.dbc.

Run it against dummy_ecu.py (or with the Form Designer's "Test panel..." button): the Run switch
starts the simulated engine, the gauge, 7-segment display and trend follow the temperature, the
indicator shows the diagnostic session from the DBC value table, and the output box logs events.
The ECU information page shows what the ECU tells over UDS: its VIN, serial number, part number and
software version, its session, security and uptime, live values by DID and its fault memory.
"""
import time


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


# --- ECU information page ----------------------------------------------------------------------
# Everything the Dummy ECU tells over UDS: who it is, the state it is in, live values by DID and its
# fault memory. Connecting reads it all, and so do Read all and the toolbar's Read, which runs Read();
# Unlock takes the extended session and security access, which the calibration ID needs; Live reads
# the state and the live values again every second.

IDENTIFICATION = {"info_vin": 0xF190, "info_serial": 0xF18C, "info_part_number": 0xF187,
                  "info_software": 0xF195}                   # control -> DID
DTC_STATUS = ("test failed", "failed this cycle", "pending", "confirmed", "not completed since clear",
              "failed since clear", "not completed this cycle", "warning lamp")      # status bits 0-7


def Read(api):
    """The toolbar's Read: the ECU information page read again. It fails when the ECU does not answer."""
    return read_all(api)


@on_start
def read_at_start(api):
    read_all(api)


def on_read_info_clicked(api, value):
    read_all(api)


def on_unlock_clicked(api, value):
    """The extended session, then security access: the calibration ID answers once both are granted."""
    session = DSC(0x03)
    unlocked = SecurityUnlock(0x01, compute_key) if session else session
    api.ui.set_value("info_status", "Unlocked: extended session and security access" if unlocked else
                     f"Unlock failed: {unlocked.error}")
    if read_state(api):
        read_calibration_id(api)


def on_live_changed(api, value):
    if value:
        read_state(api)


@on_timer(1.0)
def live(api):
    if api.ui.get_value("live") and not read_state(api):
        api.ui.set_value("live", False)              # an ECU that does not answer is not asked every second


def read_all(api):
    """Everything on the page. Returns the answer to the first request: a silent ECU is asked no more."""
    state = read_state(api)
    if state:
        for name, did in IDENTIFICATION.items():
            result = RDBI(did)
            api.ui.set_value(name, result.text if result else f"({result.error})")
        read_calibration_id(api)
        read_fault_memory(api)
        api.ui.set_value("info_status", f"Read at {time.strftime('%H:%M:%S')}")
    return state


def read_state(api):
    """The session, the uptime and the live values. Returns the answer to the first request."""
    session = RDBI(0xF186, timeout=1.0)              # a short wait: an ECU that is not there is soon known
    if not session:
        api.ui.set_value("info_status", f"Not read: {session.error}")
        return session
    api.ui.set_value("info_session", session.int)
    uptime = RDBI(0x0100)                            # seconds since the ECU started
    if uptime:
        minutes, seconds = divmod(uptime.int, 60)
        api.ui.set_value("info_uptime", f"{minutes // 60} h {minutes % 60:02d} min {seconds:02d} s")
    else:
        api.ui.set_value("info_uptime", f"({uptime.error})")
    temperature = RDBI(0x0101)                       # 0.1 degC, signed
    api.ui.set_value("info_temperature", int.from_bytes(temperature.data, "big", signed=True) / 10
                     if temperature else f"({temperature.error})")
    pressure = RDBI(0x0102)                          # 0.01 bar
    api.ui.set_value("info_pressure", pressure.int / 100 if pressure else f"({pressure.error})")
    idle = RDBI(0x0110)                              # rpm
    api.ui.set_value("info_idle_speed", idle.int if idle else f"({idle.error})")
    return session


def read_calibration_id(api):
    """0200 answers in the extended session with security access only: its answer is the security state."""
    result = RDBI(0x0200)
    api.ui.set_value("info_security", bool(result))
    if result:
        api.ui.set_value("info_calibration", result.text)
    elif result.nrc in (0x31, 0x33):                 # not in the extended session, or not unlocked
        api.ui.set_value("info_calibration", "locked - press Unlock")
    else:
        api.ui.set_value("info_calibration", f"({result.error})")


def read_fault_memory(api):
    """ReadDTCInformation reportDTCByStatusMask: every DTC stored, and what its status byte says."""
    result = RDTCI(0x02, 0xFF)
    api.ui.set_value("info_dtcs", None)              # the list emptied
    if not result:
        api.ui.set_value("info_dtc_count", f"({result.error})")
        return
    records = result.data[1:]                        # after the status availability mask: DTC (3 bytes), status
    dtcs = [(int.from_bytes(records[i:i + 3], "big"), records[i + 3])
            for i in range(0, len(records) - 3, 4)]
    for dtc, status in dtcs:
        meaning = ", ".join(name for bit, name in enumerate(DTC_STATUS) if status & (1 << bit))
        api.ui.set_value("info_dtcs", f"{dtc_code(dtc)}   status {status:02X}: {meaning or 'no bit set'}")
    failing = sum(1 for _dtc, status in dtcs if status & 0x01)
    api.ui.set_value("info_dtc_count", f"{len(dtcs)} stored, {failing} failing now" if dtcs else
                     "none stored")


def dtc_code(dtc):
    """A 3-byte DTC as SAE J2012 writes it: 0x010100 is P0101-00, the last byte its failure type."""
    system, digit = "PCBU"[dtc >> 22], (dtc >> 20) & 0x3
    return f"{system}{digit}{(dtc >> 16) & 0xF:X}{(dtc >> 8) & 0xFF:02X}-{dtc & 0xFF:02X}"


# --- Firmware flashing (ISO 14229-1) ---------------------------------------------------------
# Defining Flashing() makes Reflash on the toolbar offer it (and Flashing... in the Form Designer's
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
