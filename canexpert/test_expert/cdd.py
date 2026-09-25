"""
CANdela diagnostic descriptions (.cdd, CANdelaStudio's XML) as TestExpert descriptions.

The chain from a variant to request bytes is the one CANdelaStudio writes:

    ECUDOC/ECU/VAR -> DIAGCLASS (tmplref: a DCLTMPL) -> DIAGINST -> SERVICE (tmplref: a DCLSRVTMPL of that
    DCLTMPL) -> the DCLSRVTMPL's tmplref: a PROTOCOLSERVICE -> its REQ: CONSTCOMP (v, a constant) and STATICCOMP
    (its value: the instance's STATICVALUE for the DCLTMPL's SHSTATIC that names the component)

States are ECUDOC/STATEGROUPS/STATEGROUP (spec "session" or "security") / STATE, numbered from 1 across all
groups; a SERVICE's mayBeExec="(1,2,4)" lists the states it may be executed in, its trans="(1,2,3,2)" the
(from, to) transitions it causes; older documents have neither, and their services are taken as allowed in every
state.

A DID's data record is its instance's data container - the SIMPLECOMPCONT whose shared proxy (a SHPROXY of the
class template) has dest "data", not the one of the response codes' texts - with its DATAOBJs in order, gaps
(GAPDATAOBJ, bl bits), structures (STRUCT) and unions (UNION: the first view of the same bits); an iteration or
a field of variable size gives it no fixed layout. Each data object's type (DATATYPES, or defined in place): its
coded value's CVALUETYPE (bl bits, enc uns/sgn/asc/bcd, qty field with minsz/maxsz), a text table's TEXTMAP s/e
and TEXT, a linear type's COMP - f, o and div (physical = (f * coded + o) / div), and s/e where it limits the
coded values - and the unit its PVALUETYPE gives (a UNIT element).

A document whose sessions are 81, 85... and whose services read data by local identifier is KWP2000 (ISO
14230), not UDS: it is read, with a warning, since TestExpert's tests are UDS's.
"""
from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from pathlib import Path

from canexpert.test_expert.description import DataField, EcuDescription, RawService, RawState, build_description


class CddError(ValueError):
    """Not a CANdela document this loader can read."""


# CVALUETYPE's enc -> DataField's encoding; one not known is taken as bytes (not checked as a number).
ENCODINGS = {"uns": "unsigned", "sgn": "signed", "asc": "ascii", "bcd": "bcd"}
# Services only KWP2000 has (by local identifier, ECU identification...), and only UDS.
KWP_ONLY = {0x12, 0x13, 0x17, 0x18, 0x1A, 0x20, 0x21, 0x30, 0x32, 0x33, 0x3B, 0x81, 0x82}
UDS_ONLY = {0x19, 0x22, 0x2E, 0x2F}


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

    # --- a DID's data record ------------------------------------------------------------------------------

    def _record_elements(self, instance) -> list:
        """The elements of an instance's data record: those of its data containers (a SIMPLECOMPCONT whose
        shared proxy is the data - not a response code's texts - or that names none), and of the shared
        structures they refer to (DIDDATAREF)."""
        elements = []
        for container in instance.findall("SIMPLECOMPCONT"):
            proxy = self.by_id.get(container.get("shproxyref", ""))
            if proxy is not None and proxy.get("dest", "data") != "data":
                continue
            for element in container:
                if element.tag == "DIDDATAREF":
                    shared = self.by_id.get(element.get("didRef", ""))
                    structure = shared.find("STRUCTURE") if shared is not None else None
                    elements += list(structure) if structure is not None else []
                else:
                    elements.append(element)
        return elements

    def record(self, instance) -> tuple[int | None, list[DataField]]:
        """(bytes, fields) of an instance's data record; (None, []) when it has none, or no fixed layout; (None,
        fields) when its last field is text or bytes of a length of their own."""
        fields = []
        bits = self._walk(self._record_elements(instance), fields, 0)
        if fields and fields[-1].variable:
            return None, fields
        return ((bits + 7) // 8, fields) if bits else (None, [])

    def _walk(self, elements, fields, position) -> int | None:
        """Add the fields of elements from bit position on; the position after them, or None (no fixed
        layout)."""
        for element in elements:
            tag = element.tag
            if tag in ("NAME", "QUAL", "DESC"):
                continue
            if fields and fields[-1].variable:
                return None                                       # nothing is known after a field of any length
            if tag in ("DATAOBJ", "SPECDATAOBJ"):
                field = self._field(element, position)
                if field is None:
                    return None
                fields.append(field)
                position += field.bits
            elif tag == "GAPDATAOBJ":
                try:
                    position += int(element.get("bl", ""))
                except ValueError:
                    return None
            elif tag in ("STRUCT", "UNION"):
                members = [child for child in element if child.tag not in ("NAME", "QUAL", "DESC")]
                if tag == "UNION":
                    members = members[:1]                         # the first view of the same bits
                if not members:
                    return None
                position = self._walk(members, fields, position)
                if position is None:
                    return None
            else:
                return None           # an iteration, a multiplexer, data up to the end: no fixed layout
        return position

    def _field(self, data, position) -> DataField | None:
        """The DataField of a data object at bit position, or None when its size is not fixed."""
        datatype = self.datatypes.get(data.get("dtref", ""))
        if datatype is None:                                      # a type defined in place
            datatype = next((child for child in data if child.find("CVALUETYPE") is not None), None)
        coded = datatype.find("CVALUETYPE") if datatype is not None else None
        if coded is None or coded.get("sz") == "yes":             # sz: the value says its own size
            return None
        encoding = ENCODINGS.get(coded.get("enc", "uns"), "bytes")
        try:
            width = int(coded.get("bl", "0"))
            if coded.get("qty") == "field":
                if coded.get("minsz") != coded.get("maxsz"):          # a variable length: text or bytes to the end
                    return DataField(_name(data) or "Value", position, 0, "ascii" if encoding == "ascii" else "bytes")
                width *= int(coded.get("maxsz", "1"))
        except ValueError:
            return None
        if width <= 0:
            return None
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
                factor, offset = float(comp.get("f", 1)), float(comp.get("o", 0))
                divisor = float(comp.get("div", 1)) or 1.0
            except ValueError:
                continue
            scale, shift = factor / divisor, offset / divisor
            if comp.get("s") is not None and comp.get("e") is not None:
                try:
                    valid.append((int(comp.get("s")), int(comp.get("e"))))
                except ValueError:
                    pass
        if texts and all(low == high for low, high in valid):
            valid = []                                            # the text table's own values
        if valid == [(0, (1 << width) - 1)] and encoding == "unsigned":
            valid = []                                            # any value: no limit
        physical = datatype.find("PVALUETYPE")
        unit = ((physical.findtext("UNIT") or physical.get("unit") or "") if physical is not None else "").strip()
        return DataField(_name(data) or "Value", position, width, encoding, valid, texts, scale, shift, unit)

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
                length, fields = self.record(instance) if prefix[0] in (0x22, 0x2E) else (None, [])
                raw.append(RawService(prefix, _label(instance), allowed, pairs, length, fields))
        return raw


def cdd_variants(path) -> list[str]:
    """The qualifiers of a CDD's variants (VARs), in the file's order."""
    root = ElementTree.parse(str(path)).getroot()
    ecudoc = root.find("ECUDOC") if root.tag != "ECUDOC" else root
    ecu = ecudoc.find("ECU") if ecudoc is not None else None
    return [_name(var) for var in ecu.findall("VAR")] if ecu is not None else []


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
    chosen = next((var for var in variants if variant and _name(var) == variant), None)
    if chosen is None and variant:
        raise CddError(f"no variant {variant!r}: the file has {', '.join(_name(var) for var in variants)}")
    chosen = chosen if chosen is not None else variants[0]
    states, _order = document.states()
    raw = document.services(chosen)
    ecu_name = _name(ecu) or Path(path).stem
    description = build_description(raw, states, f"{ecu_name} ({_name(chosen)})" if _name(chosen) else ecu_name,
                                    str(path))
    description.warnings = document.warnings + description.warnings
    if not states:
        description.warnings.append("No STATEGROUPS: every service is taken as allowed in every session")
    elif not any(raw_service.allowed is not None for raw_service in raw):
        description.warnings.append("The services say nothing of the states they may be executed in (no mayBeExec): "
                                    "each is taken as allowed in every session")
    sids = {raw_service.prefix[0] for raw_service in raw}
    protocol = (document.ecudoc.findtext("PROTOCOLSTANDARD") or "").strip().upper()
    kwp_sessions = any(r.prefix[0] == 0x10 and len(r.prefix) > 1 and r.prefix[1] & 0x80 for r in raw)
    if "KWP" in protocol or kwp_sessions or (sids & KWP_ONLY and not sids & UDS_ONLY):
        description.warnings.insert(0, "This looks like a KWP2000 (ISO 14230) description, not a UDS one: sessions "
                                       "81, 85..., data by local identifier (21, 3B), ECU identification (1A). "
                                       "TestExpert tests UDS (ISO 14229-1): many of its tests do not apply")
    description.variants = [_name(var) for var in variants]
    description.variant = _name(chosen)
    return description
