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
    vin = api.uds.rdbi(0xF190)                           # UDS over ISO-TP (multi-frame reply)
    api.ui.set_value("log", f"VIN: {vin.decode(errors='replace') if vin else 'no answer'}")


def on_session_changed(api, value):
    sessions = {"Default": 0x01, "Extended": 0x03}
    reply = api.uds.request([0x10, sessions[value]])
    api.ui.set_value("log", f"Session {value}: {reply.hex(' ') if reply else 'no answer'}")


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
