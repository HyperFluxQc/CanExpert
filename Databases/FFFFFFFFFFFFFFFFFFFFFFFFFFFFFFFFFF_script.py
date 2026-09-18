"""
Database script - runs when this database is loaded.
Define DatabaseMainFunction(api) - it will be called with the API object.
You can define your own functions and call them from DatabaseMainFunction.
"""
# API: api.can.send(id, data), api.can.get_latest_messages()
#       api.uds.tester_present(), api.uds.rdbi(did)
#       api.uds.request_download(format, address, size)
#       api.uds.transfer_data_from_file(s19_path, packet_size)
#       api.dll.load(path), api.dll.call(path, "FunctionName", ...)
#       api.ui.get_value(name), api.ui.set_value(name, value)
#       api.log("message")


def DatabaseMainFunction(api):
    # The connection now schedules TesterPresent using the active configuration.
    api.log("Database loaded; configured TesterPresent monitoring is active.")