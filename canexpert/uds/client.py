"""
ISO 14229-1 (UDS) service functions for panel scripts, e.g. RDBI(0xF190) sends 22 F1 90.

Every service is covered except Authentication (0x29) and SecuredDataTransmission (0x84). The
functions are available by name in panel scripts and return a UdsResult:

    vin = RDBI(0xF190)
    if vin:                              # positive response
        api.ui.set_value("vin", vin.text)
    else:
        api.log(vin.error)               # "NRC 0x31 requestOutOfRange" or "no response"

Requests go to the configuration's request/response IDs over ISO-TP (multi-frame, flow control,
NRC 0x78 response pending). Sub-function services take suppress=True to set the
suppressPosRspMsgIndicationBit; the request is then sent without waiting for a reply.
"""
from __future__ import annotations

import inspect

NRC_NAMES = {
    0x10: "generalReject", 0x11: "serviceNotSupported", 0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat", 0x14: "responseTooLong", 0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect", 0x24: "requestSequenceError", 0x25: "noResponseFromSubnetComponent",
    0x26: "failurePreventsExecutionOfRequestedAction", 0x31: "requestOutOfRange", 0x33: "securityAccessDenied",
    0x34: "authenticationRequired", 0x35: "invalidKey", 0x36: "exceededNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired", 0x70: "uploadDownloadNotAccepted", 0x71: "transferDataSuspended",
    0x72: "generalProgrammingFailure", 0x73: "wrongBlockSequenceCounter",
    0x78: "requestCorrectlyReceived-ResponsePending", 0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession", 0x81: "rpmTooHigh", 0x82: "rpmTooLow", 0x83: "engineIsRunning",
    0x84: "engineIsNotRunning", 0x85: "engineRunTimeTooLow", 0x86: "temperatureTooHigh", 0x87: "temperatureTooLow",
    0x88: "vehicleSpeedTooHigh", 0x89: "vehicleSpeedTooLow", 0x8A: "throttle/PedalTooHigh",
    0x8B: "throttle/PedalTooLow", 0x8C: "transmissionRangeNotInNeutral", 0x8D: "transmissionRangeNotInGear",
    0x8F: "brakeSwitch(es)NotClosed", 0x90: "shifterLeverNotInPark", 0x91: "torqueConverterClutchLocked",
    0x92: "voltageTooHigh", 0x93: "voltageTooLow", 0x94: "resourceTemporarilyNotAvailable",
}

# Functional units of ISO 14229-1, in the standard's order (panel groups).
GROUPS = ("Diagnostic and communication management", "Data transmission", "Stored data transmission",
          "Input/output control", "Remote activation of routine", "Upload/download", "Helpers")


def to_bytes(value) -> bytes:
    """bytes/bytearray as-is, a list of ints, text (encoded as Latin-1), or None for nothing."""
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("latin-1")
    if isinstance(value, int):
        return bytes([value])
    return bytes(value)


def _number(value: int, length: int) -> bytes:
    return int(value).to_bytes(length, "big")


def _memory(address: int, size: int, fmt: int) -> bytes:
    """addressAndLengthFormatIdentifier + memoryAddress + memorySize (high nibble: size length)."""
    address_length, size_length = fmt & 0x0F, fmt >> 4
    if not 1 <= address_length <= 15 or not 1 <= size_length <= 15:
        raise ValueError("format must give address and size lengths, e.g. 0x44")
    return bytes([fmt]) + _number(address, address_length) + _number(size, size_length)


class UdsResult:
    """Outcome of one request. True for a positive response.

    raw: full response (SID first) or None; data: response bytes after the SID and the echoed
    parameters (for RDBI: the data record); nrc / nrc_name for a negative response.
    """

    def __init__(self, request: bytes, raw: bytes | None, echo: int = 0, suppressed: bool = False):
        self.request, self.raw, self.suppressed = request, raw, suppressed
        self.nrc = raw[2] if raw is not None and len(raw) >= 3 and raw[0] == 0x7F else None
        self.ok = suppressed or (raw is not None and raw[0] == (request[0] + 0x40) & 0xFF)
        self.data = raw[1 + echo:] if self.ok and raw is not None else b""
        self.max_block_length = None

    @property
    def timeout(self):
        return self.raw is None and not self.suppressed

    @property
    def nrc_name(self):
        return NRC_NAMES.get(self.nrc, "unknown") if self.nrc is not None else ""

    @property
    def error(self):
        if self.ok:
            return ""
        return f"NRC 0x{self.nrc:02X} {self.nrc_name}" if self.nrc is not None else "no response"

    @property
    def int(self):
        """data as a big-endian unsigned integer."""
        return int.from_bytes(self.data, "big")

    @property
    def text(self):
        """data as text (non-printable bytes dropped)."""
        return self.data.decode("latin-1").rstrip("\x00").strip()

    def hex(self, sep=" "):
        return self.data.hex(sep)

    def __bool__(self):
        return self.ok

    def __bytes__(self):
        return self.data

    def __repr__(self):
        request = self.request.hex(" ").upper()
        if self.suppressed:
            return f"{request} (response suppressed)"
        return f"{request} -> {self.raw.hex(' ').upper() if self.raw is not None else 'no response'}" + \
            (f" ({self.error})" if self.nrc is not None else "")


class UdsFunctions:
    """The ISO 14229-1 service functions, bound to a request function (payload, timeout, wait) -> reply."""

    def __init__(self, request, log=None):
        self._request = request
        self._log = log
        self.log_requests = False

    def _send(self, payload, echo=0, suppress=False, timeout=None):
        payload = bytes(payload)
        if suppress:
            if len(payload) < 2:
                raise ValueError("suppress needs a sub-function")
            payload = payload[:1] + bytes([payload[1] | 0x80]) + payload[2:]
            self._request(payload, timeout, False)
            result = UdsResult(payload, None, suppressed=True)
        else:
            result = UdsResult(payload, self._request(payload, timeout, True), echo)
        if self.log_requests and self._log:
            self._log(f"UDS {result!r}")
        return result

    # --- Diagnostic and communication management ----------------------------------------

    def DSC(self, session: int, suppress=False, timeout=None):
        """0x10 DiagnosticSessionControl. session: 0x01 default, 0x02 programming, 0x03 extended,
        0x04 safety system. data = sessionParameterRecord (P2 and P2* timing)."""
        return self._send([0x10, session], 1, suppress, timeout)

    def ER(self, reset_type: int = 0x01, suppress=False, timeout=None):
        """0x11 ECUReset. reset_type: 0x01 hardReset, 0x02 keyOffOnReset, 0x03 softReset,
        0x04 enableRapidPowerShutDown, 0x05 disableRapidPowerShutDown."""
        return self._send([0x11, reset_type], 1, suppress, timeout)

    def SA(self, sub_function: int, data=b"", suppress=False, timeout=None):
        """0x27 SecurityAccess. Odd sub_function = requestSeed (data = seed in the reply),
        even sub_function = sendKey with data = key. See SecurityUnlock() for both steps."""
        return self._send(bytes([0x27, sub_function]) + to_bytes(data), 1, suppress, timeout)

    def CC(self, control_type: int, communication_type: int = 0x01, node_id: int | None = None, suppress=False,
           timeout=None):
        """0x28 CommunicationControl. control_type: 0x00 enableRxAndTx, 0x01 enableRxAndDisableTx,
        0x02 disableRxAndEnableTx, 0x03 disableRxAndTx (0x04/0x05 with node_id).
        communication_type: 0x01 normal, 0x02 network management, 0x03 both."""
        payload = bytes([0x28, control_type, communication_type])
        if node_id is not None:
            payload += _number(node_id, 2)
        return self._send(payload, 1, suppress, timeout)

    def TP(self, suppress=False, timeout=None):
        """0x3E TesterPresent (sub-function 0x00). The connection already sends it periodically."""
        return self._send([0x3E, 0x00], 1, suppress, timeout)

    def ATP(self, access_type: int, record=b"", suppress=False, timeout=None):
        """0x83 AccessTimingParameter. access_type: 0x01 readExtendedTimingParameterSet,
        0x02 setTimingParametersToDefaultValues, 0x03 readCurrentlyActiveTimingParameters,
        0x04 setTimingParametersToGivenValues (record = TimingParameterRequestRecord)."""
        return self._send(bytes([0x83, access_type]) + to_bytes(record), 1, suppress, timeout)

    def CDTCS(self, setting_type: int, record=b"", suppress=False, timeout=None):
        """0x85 ControlDTCSetting. setting_type: 0x01 on, 0x02 off; record = DTCSettingControlOptionRecord."""
        return self._send(bytes([0x85, setting_type]) + to_bytes(record), 1, suppress, timeout)

    def ROE(self, event_type: int, window_time: int = 0x02, event_record=b"", service_record=b"", suppress=False,
            timeout=None):
        """0x86 ResponseOnEvent. event_type: 0x00 stopResponseOnEvent, 0x01 onDTCStatusChange,
        0x03 onChangeOfDataIdentifier, 0x04 reportActivatedEvents, 0x05 startResponseOnEvent,
        0x06 clearResponseOnEvent, 0x07 onComparisonOfValues... window_time: eventWindowTime
        (0x02 infinite). service_record: the request to send when the event occurs, e.g. [0x22, 0xF1, 0x90]."""
        payload = bytes([0x86, event_type, window_time]) + to_bytes(event_record) + to_bytes(service_record)
        return self._send(payload, 1, suppress, timeout)

    def LC(self, control_type: int, parameter=b"", suppress=False, timeout=None):
        """0x87 LinkControl. control_type: 0x01 verifyModeTransitionWithFixedParameter
        (parameter = linkControlModeIdentifier, e.g. 0x12 CAN 500 kbit/s), 0x02 ...WithSpecificParameter
        (parameter = 3-byte linkRecord), 0x03 transitionMode."""
        return self._send(bytes([0x87, control_type]) + to_bytes(parameter), 1, suppress, timeout)

    # --- Data transmission -----------------------------------------------------------------

    def RDBI(self, did: int, *more_dids: int, timeout=None):
        """0x22 ReadDataByIdentifier. RDBI(0xF190) sends 22 F1 90; data = the data record.
        With several DIDs, data holds every DID and record as returned by the ECU."""
        payload = bytes([0x22]) + b"".join(_number(d, 2) for d in (did, *more_dids))
        return self._send(payload, 0 if more_dids else 2, False, timeout)

    def RMBA(self, address: int, size: int, format: int = 0x44, timeout=None):
        """0x23 ReadMemoryByAddress. format = addressAndLengthFormatIdentifier (0x44: 4-byte address
        and size). data = the memory contents."""
        return self._send(bytes([0x23]) + _memory(address, size, format), 0, False, timeout)

    def RSDBI(self, did: int, timeout=None):
        """0x24 ReadScalingDataByIdentifier. data = scalingByte(s) and scaling data for the DID."""
        return self._send(bytes([0x24]) + _number(did, 2), 2, False, timeout)

    def RDBPI(self, transmission_mode: int, *periodic_ids: int, timeout=None):
        """0x2A ReadDataByPeriodicIdentifier. transmission_mode: 0x01 slow, 0x02 medium, 0x03 fast,
        0x04 stopSending. periodic_ids: 0xF2xx identifiers (only the low byte is sent)."""
        return self._send(bytes([0x2A, transmission_mode] + [pid & 0xFF for pid in periodic_ids]), 0, False, timeout)

    def DDDI_DefineById(self, dynamic_did: int, sources, suppress=False, timeout=None):
        """0x2C 01 DynamicallyDefineDataIdentifier - defineByIdentifier.
        sources: [(source_did, position_in_source_record, memory_size), ...] (position starts at 1)."""
        payload = bytes([0x2C, 0x01]) + _number(dynamic_did, 2)
        for source_did, position, size in sources:
            payload += _number(source_did, 2) + bytes([position, size])
        return self._send(payload, 1, suppress, timeout)

    def DDDI_DefineByAddress(self, dynamic_did: int, areas, format: int = 0x44, suppress=False, timeout=None):
        """0x2C 02 DynamicallyDefineDataIdentifier - defineByMemoryAddress. areas: [(address, size), ...]."""
        payload = bytes([0x2C, 0x02]) + _number(dynamic_did, 2) + bytes([format])
        for address, size in areas:
            payload += _memory(address, size, format)[1:]
        return self._send(payload, 1, suppress, timeout)

    def DDDI_Clear(self, dynamic_did: int | None = None, suppress=False, timeout=None):
        """0x2C 03 DynamicallyDefineDataIdentifier - clearDynamicallyDefinedDataIdentifier
        (all of them when dynamic_did is None)."""
        payload = bytes([0x2C, 0x03]) + (_number(dynamic_did, 2) if dynamic_did is not None else b"")
        return self._send(payload, 1, suppress, timeout)

    def WDBI(self, did: int, data, timeout=None):
        """0x2E WriteDataByIdentifier. WDBI(0xF190, "WVWZZZ1KZAW000001") or WDBI(0x0101, [0x01, 0x02])."""
        return self._send(bytes([0x2E]) + _number(did, 2) + to_bytes(data), 2, False, timeout)

    def WMBA(self, address: int, data, format: int = 0x44, timeout=None):
        """0x3D WriteMemoryByAddress. Writes data at address; the size is taken from data."""
        data = to_bytes(data)
        return self._send(bytes([0x3D]) + _memory(address, len(data), format) + data, 0, False, timeout)

    # --- Stored data transmission ----------------------------------------------------------

    def CDTCI(self, group: int = 0xFFFFFF, memory_selection: int | None = None, timeout=None):
        """0x14 ClearDiagnosticInformation. group: groupOfDTC (0xFFFFFF = all groups);
        memory_selection: optional user-defined DTC memory."""
        payload = bytes([0x14]) + _number(group, 3) + (bytes([memory_selection]) if memory_selection is not None else b"")
        return self._send(payload, 0, False, timeout)

    def RDTCI(self, sub_function: int, *parameters, suppress=False, timeout=None):
        """0x19 ReadDTCInformation. RDTCI(0x02, 0xFF) reports DTCs by status mask; RDTCI(0x01, 0xFF)
        counts them; RDTCI(0x04, 0x010100, 0xFF) reads a snapshot (a 3-byte DTC, then record number).
        Integer parameters above 0xFF are sent as 3-byte DTC numbers. See ReadDTCs()."""
        payload = bytes([0x19, sub_function])
        for parameter in parameters:
            payload += _number(parameter, 3) if isinstance(parameter, int) and parameter > 0xFF else to_bytes(parameter)
        return self._send(payload, 1, suppress, timeout)

    # --- Input/output control -------------------------------------------------------------

    def IOCBI(self, did: int, control_parameter: int, state=b"", mask=b"", timeout=None):
        """0x2F InputOutputControlByIdentifier. control_parameter: 0x00 returnControlToECU,
        0x01 resetToDefault, 0x02 freezeCurrentState, 0x03 shortTermAdjustment (state = controlState).
        mask = controlEnableMaskRecord when the DID packs several signals."""
        payload = bytes([0x2F]) + _number(did, 2) + bytes([control_parameter]) + to_bytes(state) + to_bytes(mask)
        return self._send(payload, 2, False, timeout)

    # --- Remote activation of routine ------------------------------------------------------

    def RC(self, sub_function: int, routine_id: int, data=b"", suppress=False, timeout=None):
        """0x31 RoutineControl. sub_function: 0x01 startRoutine, 0x02 stopRoutine,
        0x03 requestRoutineResults. data = routineControlOptionRecord; reply data = status record."""
        payload = bytes([0x31, sub_function]) + _number(routine_id, 2) + to_bytes(data)
        return self._send(payload, 3, suppress, timeout)

    # --- Upload/download -------------------------------------------------------------------

    def _transfer_request(self, sid, address, size, format, data_format, timeout):
        result = self._send(bytes([sid, data_format]) + _memory(address, size, format), 0, False, timeout)
        if result and result.data:
            length = result.data[0] >> 4
            result.max_block_length = int.from_bytes(result.data[1:1 + length], "big") or None
        return result

    def RD(self, address: int, size: int, format: int = 0x44, data_format: int = 0x00, timeout=None):
        """0x34 RequestDownload (tester to ECU). data_format: compression/encryption (0x00 none).
        result.max_block_length = maxNumberOfBlockLength (includes SID and block counter)."""
        return self._transfer_request(0x34, address, size, format, data_format, timeout)

    def RU(self, address: int, size: int, format: int = 0x44, data_format: int = 0x00, timeout=None):
        """0x35 RequestUpload (ECU to tester). result.max_block_length as for RD()."""
        return self._transfer_request(0x35, address, size, format, data_format, timeout)

    def TD(self, block_counter: int, data=b"", timeout=None):
        """0x36 TransferData. block_counter starts at 1 and wraps from 0xFF to 0x00.
        After RU(), send without data to receive a block (reply data = the block)."""
        return self._send(bytes([0x36, block_counter & 0xFF]) + to_bytes(data), 1, False, timeout)

    def RTE(self, data=b"", timeout=None):
        """0x37 RequestTransferExit. data = transferRequestParameterRecord (e.g. a checksum)."""
        return self._send(bytes([0x37]) + to_bytes(data), 0, False, timeout)

    def RFT(self, mode: int, path: str, data_format: int = 0x00, size: int | None = None,
            compressed_size: int | None = None, size_length: int = 4, timeout=None):
        """0x38 RequestFileTransfer. mode: 0x01 AddFile, 0x02 DeleteFile, 0x03 ReplaceFile,
        0x04 ReadFile, 0x05 ReadDir, 0x06 ResumeFile. size/compressed_size are needed for
        add/replace/resume; result.max_block_length as for RD()."""
        name = to_bytes(path)
        payload = bytes([0x38, mode]) + _number(len(name), 2) + name
        if mode not in (0x02, 0x05):
            payload += bytes([data_format])
        if mode in (0x01, 0x03, 0x06):
            if size is None:
                raise ValueError("size is required to add, replace or resume a file")
            payload += bytes([size_length]) + _number(size, size_length)
            payload += _number(size if compressed_size is None else compressed_size, size_length)
        result = self._send(payload, 1, False, timeout)
        if result and len(result.data) >= 1 and mode not in (0x02,):
            length = result.data[0]
            result.max_block_length = int.from_bytes(result.data[1:1 + length], "big") or None
        return result

    # --- Raw and helpers ----------------------------------------------------------------------

    def UDS(self, request, timeout=None):
        """Any request, as bytes, a list of ints or a hex string: UDS("22 F1 90")."""
        if isinstance(request, str):
            request = bytes.fromhex(request)
        return self._send(to_bytes(request), 0, False, timeout)

    def SecurityUnlock(self, level: int, compute_key, timeout=None):
        """SecurityAccess requestSeed (level, odd) then sendKey (level + 1) with compute_key(seed) -> key.
        An all-zero seed means the ECU is already unlocked. Returns the last UdsResult."""
        seed = self.SA(level, timeout=timeout)
        if not seed or not any(seed.data):
            return seed
        return self.SA(level + 1, compute_key(seed.data), timeout=timeout)

    def ReadDTCs(self, status_mask: int = 0xFF, timeout=None):
        """ReadDTCInformation reportDTCByStatusMask. Returns [(dtc, status), ...]; empty on failure."""
        result = self.RDTCI(0x02, status_mask, timeout=timeout)
        records = result.data[1:] if result else b""
        return [(int.from_bytes(records[i:i + 3], "big"), records[i + 3]) for i in range(0, len(records) - 3, 4)]

    def StartRoutine(self, routine_id: int, data=b"", timeout=None):
        """RoutineControl startRoutine (31 01 RID ...)."""
        return self.RC(0x01, routine_id, data, timeout=timeout)

    def StopRoutine(self, routine_id: int, data=b"", timeout=None):
        """RoutineControl stopRoutine (31 02 RID ...)."""
        return self.RC(0x02, routine_id, data, timeout=timeout)

    def RoutineResults(self, routine_id: int, timeout=None):
        """RoutineControl requestRoutineResults (31 03 RID)."""
        return self.RC(0x03, routine_id, timeout=timeout)

    def UdsLog(self, enabled: bool = True):
        """Log every request and response of these functions to the application log."""
        self.log_requests = bool(enabled)

    def namespace(self):
        """name -> bound function, for the script globals."""
        return {entry.name: getattr(self, entry.name) for entry in FUNCTIONS}


class Entry:
    """Panel/completion metadata for one function."""

    def __init__(self, name, sid, service, group, example):
        self.name, self.sid, self.service, self.group, self.example = name, sid, service, group, example
        method = getattr(UdsFunctions, name)
        self.doc = inspect.getdoc(method) or ""
        parameters = [p.replace(annotation=inspect.Parameter.empty)
                      for p in inspect.signature(method).parameters.values() if p.name != "self"]
        self.signature = f"{name}({', '.join(str(p) for p in parameters)})"


_D, _T, _S, _IO, _R, _U, _H = GROUPS
FUNCTIONS = [
    Entry("DSC", 0x10, "DiagnosticSessionControl", _D, "DSC(0x03)"),
    Entry("ER", 0x11, "ECUReset", _D, "ER(0x01)"),
    Entry("SA", 0x27, "SecurityAccess", _D, "seed = SA(0x01)"),
    Entry("CC", 0x28, "CommunicationControl", _D, "CC(0x03, 0x01)"),
    Entry("TP", 0x3E, "TesterPresent", _D, "TP()"),
    Entry("ATP", 0x83, "AccessTimingParameter", _D, "ATP(0x03)"),
    Entry("CDTCS", 0x85, "ControlDTCSetting", _D, "CDTCS(0x02)"),
    Entry("ROE", 0x86, "ResponseOnEvent", _D, "ROE(0x03, 0x02, [0xF1, 0x90], [0x22, 0xF1, 0x90])"),
    Entry("LC", 0x87, "LinkControl", _D, "LC(0x01, 0x12)"),
    Entry("RDBI", 0x22, "ReadDataByIdentifier", _T, "value = RDBI(0xF190)"),
    Entry("RMBA", 0x23, "ReadMemoryByAddress", _T, "memory = RMBA(0x00010000, 16)"),
    Entry("RSDBI", 0x24, "ReadScalingDataByIdentifier", _T, "scaling = RSDBI(0x0100)"),
    Entry("RDBPI", 0x2A, "ReadDataByPeriodicIdentifier", _T, "RDBPI(0x02, 0xF201)"),
    Entry("DDDI_DefineById", 0x2C, "DynamicallyDefineDataIdentifier", _T,
          "DDDI_DefineById(0xF300, [(0xF190, 1, 4), (0x0100, 1, 2)])"),
    Entry("DDDI_DefineByAddress", 0x2C, "DynamicallyDefineDataIdentifier", _T,
          "DDDI_DefineByAddress(0xF301, [(0x00010000, 4)])"),
    Entry("DDDI_Clear", 0x2C, "DynamicallyDefineDataIdentifier", _T, "DDDI_Clear(0xF300)"),
    Entry("WDBI", 0x2E, "WriteDataByIdentifier", _T, 'WDBI(0xF190, "WVWZZZ1KZAW000001")'),
    Entry("WMBA", 0x3D, "WriteMemoryByAddress", _T, "WMBA(0x00010000, [0x01, 0x02, 0x03, 0x04])"),
    Entry("CDTCI", 0x14, "ClearDiagnosticInformation", _S, "CDTCI(0xFFFFFF)"),
    Entry("RDTCI", 0x19, "ReadDTCInformation", _S, "dtcs = RDTCI(0x02, 0xFF)"),
    Entry("IOCBI", 0x2F, "InputOutputControlByIdentifier", _IO, "IOCBI(0x0200, 0x03, [0x01])"),
    Entry("RC", 0x31, "RoutineControl", _R, "RC(0x01, 0xFF00)"),
    Entry("RD", 0x34, "RequestDownload", _U, "download = RD(0x00010000, 0x1000)"),
    Entry("RU", 0x35, "RequestUpload", _U, "upload = RU(0x00010000, 0x1000)"),
    Entry("TD", 0x36, "TransferData", _U, "TD(1, block)"),
    Entry("RTE", 0x37, "RequestTransferExit", _U, "RTE()"),
    Entry("RFT", 0x38, "RequestFileTransfer", _U, 'RFT(0x04, "/logs/trace.bin")'),
    Entry("UDS", None, "Any request (raw)", _H, 'UDS("22 F1 90")'),
    Entry("SecurityUnlock", 0x27, "SecurityAccess seed + key", _H, "SecurityUnlock(0x01, compute_key)"),
    Entry("ReadDTCs", 0x19, "ReadDTCInformation by status mask", _H, "for dtc, status in ReadDTCs(0xFF):"),
    Entry("StartRoutine", 0x31, "RoutineControl startRoutine", _H, "StartRoutine(0xFF00)"),
    Entry("StopRoutine", 0x31, "RoutineControl stopRoutine", _H, "StopRoutine(0xFF00)"),
    Entry("RoutineResults", 0x31, "RoutineControl requestRoutineResults", _H, "RoutineResults(0xFF00)"),
    Entry("UdsLog", None, "Log UDS requests and responses", _H, "UdsLog(True)"),
]
EXCLUDED_SERVICES = {0x29: "Authentication", 0x84: "SecuredDataTransmission"}
