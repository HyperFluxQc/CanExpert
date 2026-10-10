"""Calibration example: structured variables against the Dummy ECU (dummy_ecu.py, or the Form Designer's Test panel).

The Variables tab defines two variables: Calib Data, 136 bytes in the ECU's memory at 0x00010000, little-endian,
and Idle, the idle speed the ECU keeps in DID 0x0110. The Variable Lists show them field by field - double-click a
value to type a new one - and their Read and Write buttons read them from the ECU and write them back. The gauge
is named after a field, Idle.speed, so it shows that field.

Reading needs nothing. Writing needs the extended session and, for the memory, security access: press Unlock
first (then write within a few seconds: without TesterPresent - the Test panel sends none - the ECU leaves the
extended session after its S3 time).

The main window's Read and Write buttons - and the Test panel's - run Read() and Write() below: both variables at
once, Write() taking the extended session and security access itself.
"""

VARIABLES = ("Idle", "Calib Data")


def key(seed):
    """The Dummy ECU's seed and key: the seed XOR 0xA5."""
    return bytes(b ^ 0xA5 for b in seed)


@on_start
def load(api):
    idle, calib = api.var("Idle"), api.var("Calib Data")
    if idle.read() and calib.read():
        api.ui.set_value("log", f"Idle speed {idle.speed} rpm; Calib Data read ({calib.structure.size} bytes)")
    else:
        api.ui.set_value("log", "The ECU did not answer: is it connected?")


def on_unlock_clicked(api, value):
    session = DSC(0x03)                                                   # extended session
    unlocked = SecurityUnlock(0x01, key)
    api.ui.set_value("log", f"Extended session: {'yes' if session else session.error}; "
                            f"unlocked: {'yes' if unlocked else unlocked.error}")


@on_variable("Idle", "Calib Data")
def typed(api, variable, field):
    api.ui.set_value("log", f"{variable.name}.{field} = {variable[field]}: press Write to send it")


def Read(api):
    """The Read button: both variables read from the ECU again."""
    for name in VARIABLES:
        result = api.var(name).read()
        if not result:
            return result                                                 # Read failed, and says why
    api.ui.set_value("log", f"Read: idle speed {api.var('Idle').speed} rpm, Calib Data")


def Write(api):
    """The Write button: the extended session and security access, then both variables written."""
    session = DSC(0x03)
    if not session:
        return session
    unlocked = SecurityUnlock(0x01, key)
    if not unlocked:
        return unlocked
    for name in VARIABLES:
        result = api.var(name).write()
        if not result:
            return result
    api.ui.set_value("log", "Written: Idle and Calib Data")
