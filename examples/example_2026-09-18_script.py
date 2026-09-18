"""Copy alongside the matching XML in Databases and choose family 'example'."""


def DatabaseMainFunction(api):
    api.ui.set_value("status", "Ready")
    api.on("start", lambda value: api.can.send(0x200, [1]))
    api.on("enable", lambda value: api.can.send(0x201, [int(value)]))
    api.on("input", lambda value: api.ui.set_value("status", value))
    api.on_can(lambda can_id, data: api.ui.set_value("rx", f"0x{can_id:X}: {data.hex(' ')}"))
