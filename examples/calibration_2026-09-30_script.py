"""Calibration example: structured variables against the Dummy ECU (dummy_ecu.py, or the Form Designer's Test panel).

The Variables tab defines two variables: Calib Data, 136 bytes in the ECU's memory at 0x00010000, little-endian,
and Idle, the idle speed the ECU keeps in DID 0x0110. The Variable Lists show them field by field - double-click a
value to type a new one - and their Read and Write buttons read them from the ECU and write them back. The gauge
is named after a field, Idle.speed, so it shows that field.

Reading needs nothing. Writing needs the extended session and, for the memory, security access: press Unlock
first (then write within a few seconds: without TesterPresent - the Test panel sends none - the ECU leaves the
extended session after its S3 time).
"""


@on_start
def load(api):
    idle, calib = api.var("Idle"), api.var("Calib Data")
    if idle.read() and calib.read():
        api.ui.set_value("log", f"Idle speed {idle.speed} rpm; Calib Data read ({calib.structure.size} bytes)")
    else:
        api.ui.set_value("log", "The ECU did not answer: is it connected?")


def on_unlock_clicked(api, value):
    session = DSC(0x03)                                                   # extended session
    unlocked = SecurityUnlock(0x01, lambda seed: bytes(b ^ 0xA5 for b in seed))
    api.ui.set_value("log", f"Extended session: {'yes' if session else session.error}; "
                            f"unlocked: {'yes' if unlocked else unlocked.error}")


@on_variable("Idle", "Calib Data")
def typed(api, variable, field):
    api.ui.set_value("log", f"{variable.name}.{field} = {variable[field]}: press Write to send it")
