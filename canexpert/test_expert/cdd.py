"""
CANdela diagnostic descriptions (.cdd, CANdelaStudio's XML) as TestExpert descriptions.

The chain from a variant to request bytes is the one CANdelaStudio writes:

    ECUDOC/ECU/VAR -> DIAGCLASS (tmplref: a DCLTMPL) -> DIAGINST -> SERVICE (tmplref: a DCLSRVTMPL of that
    DCLTMPL) -> the DCLSRVTMPL's tmplref: a PROTOCOLSERVICE -> its REQ: CONSTCOMP (v, a constant) and STATICCOMP
    (its value: the instance's STATICVALUE for the DCLTMPL's SHSTATIC that names the component)

States are ECUDOC/STATEGROUPS/STATEGROUP (spec "session" or "security") / STATE, numbered from 1 across all
groups; a SERVICE's mayBeExec="(1,2,4)" lists the states it may be executed in, its trans="(1,2,3,2)" the
(from, to) transitions it causes. A DID's data length comes from the DATAOBJs of its instance, and its fields
from their data types (DATATYPES): the coded value's CVALUETYPE (bl bits, enc uns/sgn/asc/bcd, qty field with
minsz/maxsz), a text table's TEXTMAP s/e and TEXT, a linear type's COMP s/e (the valid coded values), f and o
(factor and offset), and the unit of its PVALUETYPE.
"""
from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from pathlib import Path

from canexpert.test_expert.description import DataField, EcuDescription, RawService, RawState, build_description


class CddError(ValueError):
    """Not a CANdela document this loader can read."""


def _text(element, path) -> str:
    if element is None:
        return ""
    found = element.find(path)
    return (found.text or "").strip() if found is not None else ""


def _name(element) -> str:
    """The qualifier, else the displayed name."""
    return _text(element, "QUAL") or _text(element, "NAME/TUV")


def _label(element) -> str:
    """The displayed name, else the qualifier."""
    return _text(element, "NAME/TUV") or _text(element, "QUAL")


def _numbers(value: str | None) -> list[int] | None:
    """"(1,2,4)" -> [1, 2, 4]; None when absent."""
    if value is None:
        return None
    body = value.strip().strip("()")
    return [int(token) for token in body.split(",") if token.strip()] if body else []


class _Document:
    def __init__(self, root):
        self.root = root
        self.ecudoc = root.find("ECUDOC") if root.tag != "ECUDOC" else root
        if self.ecudoc is None:
            raise CddError("no ECUDOC element: not a CANdela document")
        self.by_id = {element.get("id"): element for element in self.root.iter() if element.get("id")}
        self.datatypes = {element.get("id"): element for element in self.ecudoc.findall("DATATYPES/*")}
        self.warnings = []

    def reference(self, identifier, tag):
        element = self.by_id.get(identifier or "")
        return element if element is not None and element.tag == tag else None

    def states(self) -> tuple[dict, list]:
        """{index: RawState} (indexes from 1, across every group), and the indexes in order."""
        states, order = {}, []
        for group in self.ecudoc.findall("STATEGROUPS/STATEGROUP"):
            spec = (group.get("spec") or "").lower()
            if not spec:
                group_name = _name(group).lower()
                spec = "session" if "session" in group_name else "security" if "secur" in group_name else ""
            for state in group.findall("STATE"):
                index = len(order) + 1
                states[index] = RawState(spec, _label(state) or f"State {index}")
                order.append(index)
        return states, order

    def bit_length(self, component) -> int | None:
        width = component.get("bl")
        if width is None and component.get("dtref"):
            datatype = self.datatypes.get(component.get("dtref"))
            coded = datatype.find("CVALUETYPE") if datatype is not None else None
            width = coded.get("bl") if coded is not None else None
        try:
            return int(width) if width is not None else None
        except ValueError:
            return None

    def request_prefix(self, instance, class_template, protocol) -> bytes:
        """The constant bytes the request starts with: CONSTCOMP and STATICCOMP up to the first variable part."""
        request = protocol.find("REQ") if protocol is not None else None
        if request is None:
            return b""
        prefix = bytearray()
        for component in request:
            if component.tag in ("NAME", "QUAL", "DESC"):
                continue
            if component.tag not in ("CONSTCOMP", "STATICCOMP"):
                break                                            # data: the constant part ends here
            width = self.bit_length(component)
            value = component.get("v") if component.tag == "CONSTCOMP" else self.static_value(instance, class_template, component)
            if width is None or value is None or width % 8:
                break
            try:
                prefix += int(str(value).strip("()"), 0 if str(value).startswith("0x") else 10).to_bytes(width // 8, "big")
            except (ValueError, OverflowError):
                break
        return bytes(prefix)

    def static_value(self, instance, class_template, component):
        """A STATICCOMP's value: the instance's STATICVALUE for the SHSTATIC of its DCLTMPL naming it."""
        if class_template is None:
            return None
        for static in class_template.findall("SHSTATIC"):
            if any(reference.get("idref") == component.get("id") for reference in static.findall("STATICCOMPREF")):
                for value in instance.findall("STATICVALUE"):
                    if value.get("shstaticref") == static.get("id"):
                        return value.get("v")
        values = instance.findall("STATICVALUE")
        return values[0].get("v") if len(values) == 1 else None         # a class with a single static

    def data_length(self, instance) -> int | None:
        """Bytes of an instance's data record (its DATAOBJs), when every one has a fixed width."""
        bits = 0
        objects = instance.findall("SIMPLECOMPCONT/DATAOBJ") + instance.findall("SIMPLECOMPCONT/STRUCT/DATAOBJ")
        for reference in instance.findall("SIMPLECOMPCONT/DIDDATAREF"):
            shared = self.by_id.get(reference.get("didRef", ""))
            if shared is not None:
                objects += shared.findall("STRUCTURE/DATAOBJ")
        if not objects:
            return None
        for data in objects:
            datatype = self.datatypes.get(data.get("dtref", ""))
            coded = datatype.find("CVALUETYPE") if datatype is not None else None
            if coded is None:
                return None
            try:
                width = int(coded.get("bl", "0"))
                count = int(coded.get("maxsz", "1")) if coded.get("qty") == "field" else 1
                if coded.get("qty") == "field" and coded.get("minsz") != coded.get("maxsz"):
                    return None                                   # a variable length
            except ValueError:
                return None
            bits += width * count
        return (bits + 7) // 8 if bits else None

    def _objects(self, instance) -> list:
        objects = instance.findall("SIMPLECOMPCONT/DATAOBJ") + instance.findall("SIMPLECOMPCONT/STRUCT/DATAOBJ")
        for reference in instance.findall("SIMPLECOMPCONT/DIDDATAREF"):
            shared = self.by_id.get(reference.get("didRef", ""))
            if shared is not None:
                objects += shared.findall("STRUCTURE/DATAOBJ")
        return objects

    def data_fields(self, instance) -> list[DataField]:
        """The fields of an instance's data record, one per DATAOBJ in order; [] when one has no fixed size."""
        fields, position = [], 0
        for data in self._objects(instance):
            datatype = self.datatypes.get(data.get("dtref", ""))
            coded = datatype.find("CVALUETYPE") if datatype is not None else None
            if coded is None:
                return []
            try:
                width = int(coded.get("bl", "0"))
                if coded.get("qty") == "field":
                    if coded.get("minsz") != coded.get("maxsz"):
                        return []                                 # a variable length
                    width *= int(coded.get("maxsz", "1"))
            except ValueError:
                return []
            encoding = {"sgn": "signed", "asc": "ascii", "bcd": "bcd"}.get(coded.get("enc", "uns"), "unsigned")
            if coded.get("qty") == "field" and encoding != "ascii":
                encoding = "bytes"
            texts, valid, scale, shift = {}, [], 1.0, 0.0
            for mapping in datatype.findall("TEXTMAP"):
                try:
                    low, high = int(mapping.get("s")), int(mapping.get("e"))
                except (TypeError, ValueError):
                    continue
                valid.append((low, high))
                if low == high:
                    texts[low] = _text(mapping, "TEXT/TUV")
            for comp in datatype.findall("COMP"):
                try:
                    valid.append((int(comp.get("s")), int(comp.get("e"))))
                    scale, shift = float(comp.get("f", scale)), float(comp.get("o", shift))
                except (TypeError, ValueError):
                    continue
            if texts and all(low == high for low, high in valid):
                valid = []                                      # the text table's own values
            full = (0, (1 << width) - 1) if encoding == "unsigned" else None
            if valid == [full]:
                valid = []                                      # any value: no limit
            physical = datatype.find("PVALUETYPE")
            unit = physical.get("unit", "") if physical is not None else ""
            fields.append(DataField(_name(data) or f"Field{len(fields) + 1}", position, width, encoding, valid,
                                    texts, scale, shift, unit))
            position += width
        return fields

    def services(self, variant) -> list[RawService]:
        raw = []
        instances = []
        for node in variant:
            if node.tag == "DIAGCLASS":
                template = self.reference(node.get("tmplref"), "DCLTMPL")
                instances += [(instance, template) for instance in node.findall("DIAGINST")]
            elif node.tag == "DIAGINST":
                instances.append((node, self.reference(node.get("tmplref"), "DCLTMPL")))
        for instance, class_template in instances:
            own_template = self.reference(instance.get("tmplref"), "DCLTMPL")
            class_template = own_template if own_template is not None else class_template
            for service in instance.findall("SERVICE"):
                service_template = self.reference(service.get("tmplref"), "DCLSRVTMPL")
                protocol = self.reference(service_template.get("tmplref") if service_template is not None else
                                          service.get("tmplref"), "PROTOCOLSERVICE")
                if protocol is None:
                    self.warnings.append(f"{_name(instance)}: a SERVICE without its PROTOCOLSERVICE is left out")
                    continue
                prefix = self.request_prefix(instance, class_template, protocol)
                if not prefix:
                    self.warnings.append(f"{_name(instance)}/{_name(service)}: no constant request bytes")
                    continue
                allowed = _numbers(service.get("mayBeExec"))
                if allowed is None and service_template is not None:
                    allowed = _numbers(service_template.get("mayBeExec"))
                trans = _numbers(service.get("trans"))
                if trans is None and service_template is not None:
                    trans = _numbers(service_template.get("trans"))
                pairs = list(zip(trans[0::2], trans[1::2])) if trans else []
                did = prefix[0] in (0x22, 0x2E)
                raw.append(RawService(prefix, _label(instance), allowed, pairs,
                                      self.data_length(instance) if did else None,
                                      self.data_fields(instance) if did else []))
        return raw


def load_cdd(path, variant: str | None = None) -> EcuDescription:
    """The description of a CDD file; variant: the qualifier of the VAR to read (default: the first)."""
    try:
        root = ElementTree.parse(str(path)).getroot()
    except ElementTree.ParseError as exc:
        raise CddError(f"not XML: {exc}") from None
    document = _Document(root)
    ecu = document.ecudoc.find("ECU")
    variants = ecu.findall("VAR") if ecu is not None else []
    if not variants:
        raise CddError("no ECU/VAR: the document describes no variant")
    chosen = next((var for var in variants if variant and _name(var) == variant), variants[0])
    states, _order = document.states()
    raw = document.services(chosen)
    ecu_name = _name(ecu) or Path(path).stem
    description = build_description(raw, states, f"{ecu_name} ({_name(chosen)})" if _name(chosen) else ecu_name,
                                    str(path))
    description.warnings = document.warnings + description.warnings
    if not states:
        description.warnings.append("No STATEGROUPS: every service is taken as allowed in every session")
    if len(variants) > 1:
        description.warnings.append(f"{len(variants)} variants; read: {_name(chosen) or 'the first'}")
    return description
