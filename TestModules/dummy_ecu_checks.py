"""Dummy ECU checks

An example test module: run it against the Dummy ECU (python dummy_ecu.py) with the default configuration
(request 7E0, response 7E8). Tools -> Test opens it; tick the test cases and press Run.

Every test case gets t: t.check(...) records a step that passes or fails and goes on, t.require(...) ends
the test case when it fails, t.expect_nrc(...) wants a negative response. The UDS functions are the ones the
panel scripts use.
"""

ENGINE_DATA = 0x300          # the Dummy ECU's EngineData message (DBC/dummy_ecu.dbc)
KEY_MASK = 0xA5              # the Dummy ECU's key: seed XOR A5


def setup(t):
    """Before the test cases: the ECU answers, in the default session."""
    t.require(DSC(0x01), "the ECU answers DiagnosticSessionControl default (10 01)")


def teardown(t):
    """After them, also when one failed: back to the default session."""
    DSC(0x01)


@testcase("Identification: VIN, part number and software version")
def identification(t):
    vin = RDBI(0xF190)
    t.require(vin, "VIN read (22 F1 90)")
    t.check_equal(len(vin.data), 17, "the VIN has 17 characters")
    t.check(RDBI(0xF187), "spare part number read (22 F1 87)")
    version = RDBI(0xF195)
    t.check(version and version.text.startswith("APP-"), "the software version starts with APP-",
            version.text or version.error)


@testcase("An unknown identifier is refused with requestOutOfRange")
def unknown_identifier(t):
    t.expect_nrc(RDBI(0x1234), 0x31, "22 12 34 answered 7F 22 31")


@testcase("Security access in the extended session")
def security_access(t):
    t.require(DSC(0x03), "extended session (10 03)")
    unlocked = SecurityUnlock(0x01, lambda seed: bytes(byte ^ KEY_MASK for byte in seed))
    t.check(unlocked, "unlocked with the key seed XOR A5")
    t.check(RDBI(0x0200), "the calibration identifier needs the unlock, and is read (22 02 00)")


@testcase("The fault memory can be read and cleared")
def fault_memory(t):
    t.check(CDTCI(0xFFFFFF), "ClearDiagnosticInformation (14 FF FF FF)")
    dtcs = RDTCI(0x02, 0x08)
    t.check(dtcs, "ReadDTCInformation reportDTCByStatusMask confirmed (19 02 08)")
    t.log(f"{(len(dtcs.data) - 1) // 4 if dtcs else 0} confirmed DTC(s) after clearing")


@testcase("Engine data is broadcast with a plausible temperature")
def engine_data(t):
    frame = t.wait_for_frame(ENGINE_DATA, timeout=2.0)
    t.require(frame, "EngineData (0x300) received within 2 s")
    if "Temperature" in frame.signals:
        t.check_range(frame.signals["Temperature"], -40, 150, "the temperature is between -40 and 150 degC")
    else:
        t.log("Add DBC/dummy_ecu.dbc under Tools -> Symbol databases to check the decoded temperature")
