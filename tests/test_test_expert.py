"""TestExpert: descriptions (JSON, how states become sessions and levels, CDD, ODX), the tests generated from
them against the Dummy ECU - passing when it keeps the rules, failing where it is made not to - the pre-test and
post-test sequences around them, the NRC policy and accepted deviations, what a run covered, discovering what
the ECU has, comparing two runs, test plans and their run from the command line, a description's variants and
telling which one the ECU is, CAN Expert's test modules run with the generated tests, the transport layer's tests,
the services taken further than their availability, security access taken further, and the window - with its run control: a test or a group run from the tests'
menu, the failed tests run again, runs repeated, their progress, the tests filtered."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import argparse
import json
import tempfile
import threading
import time
import unittest
import uuid
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import can
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QToolButton

from canexpert.paths import DBC_DIR, ODX_DIR, TEST_MODULES_DIR
from canexpert.simulator.ecu import BOOT_VERSION, DummyEcu, EcuConfig
from canexpert.test_expert import cli
from canexpert.test_expert import odx as odx_loader
from canexpert.test_expert import window as window_module
from canexpert.test_expert.cdd import CddError, cdd_variants, load_cdd
from canexpert.test_expert.compare import (answer_of, compare_runs, comparison_page, load_results, previous_results,
                                           results_dict)
from canexpert.test_expert.coverage import Coverage, coverage_html, untested
from canexpert.test_expert.description import (Access, DataField, EcuDescription, RawService, RawState,
                                               build_description)
from canexpert.test_expert.discovery import (Discovery, DiscoveryOptions, DiscoveryResult, compare, discovery_page,
                                             expand, parse_ranges)
from canexpert.test_expert.dummy import dummy_description
from canexpert.test_expert.engine import PlanRun
from canexpert.test_expert.generator import Options, Suite, parse_routine_starts
from canexpert.test_expert.modules import BusFrames, Symbols, load_modules
from canexpert.test_expert.plan import Connection, KeySource, PlanError, TestPlan, is_plan_file
from canexpert.test_expert.policy import Deviation, NrcPolicy, accept_function, parse_nrcs
from canexpert.test_expert.sequences import (Attachment, Sequence, SequenceError, SequenceStep, due, parse_expect,
                                             parse_frame, parse_hex, parse_script)
from canexpert.test_expert.services import parse_memory_range
from canexpert.test_expert.tester import Tester
from canexpert.test_expert.transport import GROUP as TRANSPORT_GROUP
from canexpert.test_expert.transport import Frame, Link, gap, st_min_seconds, valid_st_min
from canexpert.test_expert.variants import Identification, identify, is_odx
from canexpert.testing.runner import Runner
from canexpert.testing.window import MemorySettings
from canexpert.uds.seed_key import xor_key

APP = QApplication.instance() or QApplication([])
DUMMY_CDD = ODX_DIR / "dummy_ecu.cdd"
DUMMY_ODX = ODX_DIR / "dummy_ecu_services.odx-d"
# A CANdela document in the structure of a real export (cantools' example.cdd, from CANdelaStudio): no state
# information on its services, data containers told apart by their class's shared proxies (the data, the
# response codes' texts), a linear type with factor, divisor and a UNIT element, a text table with a range, a
# union of a whole byte and its bits (STRUCT, GAPDATAOBJ). Written for these tests, not copied.
OLD_STYLE_CDD = """<?xml version='1.0' encoding='iso-8859-1'?>
<CANDELA dtdvers='2.0.5'><ECUDOC doctype='inst'>
<STATEGROUPS><STATEGROUP><QUAL>Session</QUAL><STATE><QUAL>Default</QUAL></STATE></STATEGROUP></STATEGROUPS>
<DATATYPES>
 <LINCOMP id='dt.volt'><QUAL>Voltage</QUAL><CVALUETYPE bl='8' bo='21' enc='uns' qty='atom' sz='no'/>
  <PVALUETYPE bl='64' enc='dbl' df='flt' qty='atom' sz='no'><UNIT>V</UNIT></PVALUETYPE><COMP f='1' o='0' div='10'/></LINCOMP>
 <TEXTTBL id='dt.onoff'><QUAL>offOn</QUAL><CVALUETYPE bl='8' bo='21' enc='uns' qty='atom' sz='no'/>
  <TEXTMAP s='0' e='0'><TEXT><TUV>off</TUV></TEXT></TEXTMAP><TEXTMAP s='1' e='255'><TEXT><TUV>on</TUV></TEXT></TEXTMAP></TEXTTBL>
 <IDENT id='dt.byte'><QUAL>Byte</QUAL><CVALUETYPE bl='8' bo='21' enc='uns' qty='atom' sz='no'/></IDENT>
 <IDENT id='dt.bit'><QUAL>Bit</QUAL><CVALUETYPE bl='1' bo='21' enc='uns' qty='atom' sz='no'/></IDENT>
 <IDENT id='dt.number'><QUAL>Bcd</QUAL><CVALUETYPE bl='16' bo='21' enc='bcd' qty='atom' sz='no'/></IDENT>
</DATATYPES>
<PROTOCOLSERVICES>
 <PROTOCOLSERVICE id='ps.read'><QUAL>RDBI</QUAL><REQ><CONSTCOMP id='c1' bl='8' v='34'/><STATICCOMP id='s1' bl='16'/></REQ>
  <POS><CONSTCOMP id='c2' bl='8' v='98'/><SIMPLEPROXYCOMP id='p.data' dest='data'/></POS>
  <NEG><SIMPLEPROXYCOMP id='p.rc' dest='resCode'/></NEG></PROTOCOLSERVICE>
</PROTOCOLSERVICES>
<DCLTMPLS><DCLTMPL id='t.did'><QUAL>DATA</QUAL><DCLSRVTMPL id='st.read' tmplref='ps.read'><QUAL>Read</QUAL></DCLSRVTMPL>
 <SHSTATIC id='sh.did'><QUAL>DID</QUAL><STATICCOMPREF idref='s1'/></SHSTATIC>
 <SHPROXY id='sp.data' dest='data'><QUAL>DATA</QUAL><PROXYCOMPREF idref='p.data'/></SHPROXY>
 <SHPROXY id='sp.rc' dest='resCode'><QUAL>RC</QUAL><PROXYCOMPREF idref='p.rc'/></SHPROXY></DCLTMPL></DCLTMPLS>
<ECU><QUAL>Bench</QUAL><VAR><QUAL>COMMON</QUAL><DIAGCLASS tmplref='t.did'><QUAL>DATA</QUAL>
 <DIAGINST><QUAL>Status</QUAL><SERVICE tmplref='st.read' phys='1' func='0'><QUAL>Read</QUAL></SERVICE>
  <STATICVALUE shstaticref='sh.did' v='4660'/>
  <SIMPLECOMPCONT shproxyref='sp.rc'><SPECDATAOBJ spec='rc'><QUAL>NRC</QUAL>
   <TEXTTBL><CVALUETYPE bl='8' enc='uns' qty='atom' sz='no'/><TEXTMAP s='49' e='49'><TEXT><TUV>out</TUV></TEXT></TEXTMAP></TEXTTBL>
  </SPECDATAOBJ></SIMPLECOMPCONT>
  <SIMPLECOMPCONT shproxyref='sp.data'>
   <DATAOBJ spec='no' dtref='dt.volt'><QUAL>Supply</QUAL></DATAOBJ>
   <DATAOBJ spec='no' dtref='dt.onoff'><QUAL>Lamp</QUAL></DATAOBJ>
   <UNION><QUAL>Status</QUAL><DATAOBJ spec='no' dtref='dt.byte'><QUAL>StatusByte</QUAL></DATAOBJ>
    <STRUCT><GAPDATAOBJ bl='3'><QUAL>Unused</QUAL></GAPDATAOBJ><DATAOBJ spec='no' dtref='dt.bit'><QUAL>Confirmed</QUAL></DATAOBJ></STRUCT></UNION>
   <GAPDATAOBJ bl='8'><QUAL>Reserved</QUAL></GAPDATAOBJ>
   <DATAOBJ spec='no' dtref='dt.number'><QUAL>Build</QUAL></DATAOBJ>
  </SIMPLECOMPCONT></DIAGINST></DIAGCLASS></VAR></ECU></ECUDOC></CANDELA>
"""
TRANSPORT = {"request_id": 0x7E0, "response_id": 0x7E8, "timeout": 1.0, "extended": False, "address_byte": None,
             "padding": 0xCC, "block_size": 0, "st_min": 0}


def key(level, seed):
    return xor_key(0xA5)(seed)


def spin_until(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(.01)
    return False


class DescriptionTest(unittest.TestCase):
    STATES = {1: RawState("session", "Default"), 2: RawState("session", "Programming"), 3: RawState("session", "Extended"),
              4: RawState("security", "Locked"), 5: RawState("security", "Unlocked")}

    def test_states_become_sessions_and_levels(self):
        raw = [RawService(b"\x10\x01", "Default", [1, 2, 3, 4, 5], [(1, 1), (2, 1), (3, 1)]),
               RawService(b"\x10\x03", "Extended", [1, 2, 3, 4, 5], [(1, 3), (2, 3), (3, 3)]),
               RawService(b"\x10\x02", "Programming", [1, 2, 3, 4, 5], [(3, 2), (2, 2)]),
               RawService(b"\x27\x01", "Level 1", [3, 4, 5]),
               RawService(b"\x27\x02", "Level 1", [3, 4, 5], [(4, 5)]),
               RawService(b"\x22\xf1\x90", "VIN", None, length=17),
               RawService(b"\x2e\xf1\x90", "VIN", [3, 5]),
               RawService(b"\x31\x01\xff\x00", "Erase", [2, 5])]
        d = build_description(raw, self.STATES, "ECU")
        self.assertEqual({s.id: s.name for s in d.sessions.values()}, {1: "Default", 3: "Extended", 2: "Programming"})
        self.assertEqual(d.sessions[2].entered_from, {3}, "programming only from extended")
        self.assertEqual(d.sessions[3].entered_from, set(), "extended from anywhere")
        self.assertEqual(d.security_levels, {1: "Level 1"})
        self.assertEqual(d.services[0x27].access, Access({3}, set()), "locked or not: no level needed")
        self.assertEqual(d.dids[0xF190].read, Access())
        self.assertEqual(d.dids[0xF190].length, 17)
        self.assertEqual(d.dids[0xF190].write, Access({3}, {1}), "only the unlocked state: level 1")
        self.assertEqual(d.routines[0xFF00].sub_functions[1], Access({2}, {1}))
        self.assertEqual(d.services[0x2E].access, Access({3}, {1}))

    def test_io_control_from_its_services(self):
        raw = [RawService(b"\x10\x01", "Default", [1, 2, 3, 4, 5], [(1, 1), (2, 1), (3, 1)]),
               RawService(b"\x10\x03", "Extended", [1, 2, 3, 4, 5], [(1, 3), (2, 3), (3, 3)]),
               RawService(b"\x22\x01\x01", "Lamp", None, length=1),
               RawService(b"\x2f\x01\x01\x00", "Lamp_ReturnControlToECU", [3, 4, 5]),
               RawService(b"\x2f\x01\x01\x03", "Lamp_ShortTermAdjustment", [3, 4, 5])]
        d = build_description(raw, self.STATES, "ECU")
        self.assertEqual(d.dids[0x0101].io, Access({3}, set()))
        self.assertEqual(d.dids[0x0101].io_parameters, {0x00, 0x03}, "an instance per control parameter")
        self.assertEqual(d.dids[0x0101].length, 1, "IO control does not change the DID's own data")
        self.assertEqual(d.services[0x2F].access, Access({3}, set()))

    def test_json_round_trip_and_the_default_session(self):
        d = dummy_description()
        self.assertEqual(d.dids[0x0101].io, Access({0x03}, set()), "a DID that follows a signal is an output")
        self.assertEqual(d.dtcs, {0x010100: "P0101-00", 0xC10000: "U0100-00"})
        self.assertIn("2 DTCs", d.summary())
        again = EcuDescription.from_dict(d.to_dict())
        self.assertEqual(again.to_dict(), d.to_dict())
        self.assertEqual((again.dids[0x0101].io, again.dtcs), (d.dids[0x0101].io, d.dtcs))
        path = Path(tempfile.mkdtemp()) / "ecu.json"
        d.save(path)
        self.assertEqual(EcuDescription.load(path).dids.keys(), d.dids.keys())
        only_dids = build_description([RawService(b"\x22\xf1\x90")], {}, "ECU")
        self.assertIn(1, only_dids.sessions)
        self.assertTrue(only_dids.warnings)


class CddTest(unittest.TestCase):
    def test_the_dummy_ecus_cdd_describes_the_dummy_ecu(self):
        cdd, dummy = load_cdd(DUMMY_CDD), dummy_description()
        self.assertEqual(cdd.warnings, [])
        self.assertEqual({s: (x.access, x.sub_functions) for s, x in cdd.services.items()},
                         {s: (x.access, x.sub_functions) for s, x in dummy.services.items()})
        self.assertEqual({d: (x.name, x.length, x.read, x.write, x.fields, x.io) for d, x in cdd.dids.items()},
                         {d: (x.name, x.length, x.read, x.write, x.fields, x.io) for d, x in dummy.dids.items()},
                         "the fields too: text tables, ranges, scales, units, text")
        self.assertEqual(cdd.routines, dummy.routines)
        self.assertEqual({s: x.entered_from for s, x in cdd.sessions.items()},
                         {s: x.entered_from for s, x in dummy.sessions.items()})
        self.assertEqual(cdd.sessions[3].name, "Extended session", "the displayed name")

    def test_the_file_is_written_by_the_tool(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("make_dummy_cdd", Path(__file__).resolve().parents[1] / "tools" /
                                                      "make_dummy_cdd.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        written = module.Writer(dummy_description(EcuConfig())).write()
        self.assertEqual(written.strip(), DUMMY_CDD.read_text(encoding="utf-8").strip(),
                         "ODX/dummy_ecu.cdd is what tools/make_dummy_cdd.py writes")

    def test_a_document_as_candelastudio_exports_it(self):
        path = Path(tempfile.mkdtemp()) / "old.cdd"
        path.write_text(OLD_STYLE_CDD, encoding="iso-8859-1")
        d = load_cdd(path)
        entry = d.dids[0x1234]
        self.assertEqual(entry.length, 6, "volt, lamp, status (a union: one byte), a reserved byte, a BCD number")
        fields = {item.name: item for item in entry.fields}
        self.assertEqual(list(fields), ["Supply", "Lamp", "StatusByte", "Build"], "not the response codes' texts")
        self.assertEqual((fields["Supply"].scale, fields["Supply"].unit), (0.1, "V"), "factor / divisor, the UNIT")
        self.assertEqual(fields["Lamp"].texts, {0: "off"})
        self.assertEqual(fields["Lamp"].limits(), [(0, 0), (1, 255)], "the text table's ranges")
        self.assertEqual((fields["Build"].position, fields["Build"].encoding), (32, "bcd"), "after the gap")
        self.assertEqual(fields["Supply"].check(bytes.fromhex("7B0001001234")), (True, "12.3 V"))
        self.assertIn("no mayBeExec", " ".join(d.warnings))
        self.assertEqual(d.dids[0x1234].read, Access(), "allowed in every session: nothing says otherwise")
        kwp = Path(tempfile.mkdtemp()) / "kwp.cdd"
        kwp.write_text(OLD_STYLE_CDD.replace("v='34'", "v='33'").replace("v='98'", "v='97'"), encoding="iso-8859-1")
        self.assertIn("KWP2000", load_cdd(kwp).warnings[0], "read by local identifier: KWP2000, not UDS")

    def test_what_a_cdd_can_hold(self):
        text = DUMMY_CDD.read_text(encoding="utf-8")
        root = ElementTree.fromstring(text.split("?>", 1)[1])
        protocol = next(p for p in root.iter("PROTOCOLSERVICE") if p.findtext("QUAL") == "ReadDataByIdentifier")
        protocol.find("REQ").find("CONSTCOMP").set("v", "0x22")                 # a hexadecimal constant
        instance = next(i for i in root.iter("DIAGINST") if i.findtext("QUAL") == "VIN")
        instance.append(ElementTree.fromstring('<SERVICE tmplref="nowhere" mayBeExec="(1)"/>'))
        path = Path(tempfile.mkdtemp()) / "edited.cdd"
        path.write_text(ElementTree.tostring(root, encoding="unicode"), encoding="utf-8")
        d = load_cdd(path)
        self.assertIn(0xF190, d.dids)
        self.assertTrue(any("without its PROTOCOLSERVICE" in warning for warning in d.warnings))
        broken = Path(tempfile.mkdtemp()) / "broken.cdd"
        broken.write_text("<CANDELA/>", encoding="utf-8")
        with self.assertRaises(CddError):
            load_cdd(broken)


class DataFieldTest(unittest.TestCase):
    def test_values_and_their_limits(self):
        temperature = DataField("Temperature", 0, 16, "signed", [(-400, 1500)], scale=0.1, unit="degC")
        self.assertEqual(temperature.check(bytes.fromhex("00d7")), (True, "21.5 degC"))
        self.assertEqual(temperature.check(bytes.fromhex("07d0")),
                         (False, "200 degC: not a valid value (-40 degC to 150 degC)"))
        self.assertEqual(temperature.coded(bytes.fromhex("ff00")), -256, "two's complement")
        session = DataField("Session", 0, 8, texts={1: "Default", 3: "Extended"})
        self.assertEqual(session.check(b"\x03"), (True, "Extended (3)"))
        self.assertFalse(session.check(b"\x02")[0], "not in the text table")
        self.assertEqual(DataField("VIN", 0, 136, "ascii").check(b"WVWZZZ1KZAW000001"), (True, '"WVWZZZ1KZAW000001"'))
        self.assertFalse(DataField("Name", 0, 32, "ascii").check(b"AB\x01\x02")[0])
        self.assertEqual(DataField("Name", 0, 32, "ascii").check(b"AB\x00\x00"), (True, '"AB"'), "padded")
        date = DataField("Day", 8, 16, "bcd")
        self.assertEqual(date.coded(bytes.fromhex("202409")), 2409)
        self.assertEqual(date.check(bytes.fromhex("20240A")), (False, "not a BCD number"))
        self.assertEqual(date.encode(bytes(3), 1234).hex(), "001234")
        nibble = DataField("Nibble", 4, 8)
        self.assertEqual((nibble.encode(bytes.fromhex("ffff"), 0x12).hex(), nibble.coded(bytes.fromhex("f12f"))),
                         ("f12f", 0x12))
        self.assertEqual(DataField("Short", 0, 32).check(b"\x01"), (False, "not in the record"))
        described = dummy_description()
        again = EcuDescription.from_dict(json.loads(json.dumps(described.to_dict())))
        self.assertEqual(again.dids[0x0110].fields, described.dids[0x0110].fields)
        self.assertEqual(described.dids[0x0110].fields[0].valid, [(600, 1200)])
        self.assertEqual(parse_routine_starts("0201; FF00: 44 00 01"), {0x0201: b"", 0xFF00: b"\x44\x00\x01"})
        with self.assertRaises(ValueError):
            parse_routine_starts("0201: zz")


class OdxTest(unittest.TestCase):
    def test_services_states_and_transitions_from_odxtools(self):
        default, extended = SimpleNamespace(short_name="Default"), SimpleNamespace(short_name="Extended")
        locked, unlocked = SimpleNamespace(short_name="Locked"), SimpleNamespace(short_name="Unlocked")

        def service(prefix, name, states=(), transitions=()):
            request = SimpleNamespace(coded_const_prefix=lambda: bytearray(prefix))
            return SimpleNamespace(short_name=name, request=request, pre_condition_states=list(states),
                                   state_transitions=[SimpleNamespace(source_state=a, target_state=b) for a, b in transitions])
        layer = SimpleNamespace(short_name="ECU", state_charts=[
            SimpleNamespace(short_name="Session", semantic="SESSION", states=[default, extended]),
            SimpleNamespace(short_name="Security", semantic="SECURITY", states=[locked, unlocked])], services=[
            service(b"\x10\x01", "Default", (), [(extended, default)]),
            service(b"\x10\x03", "Extended", (), [(default, extended)]),
            service(b"\x27\x01", "Seed", [extended]), service(b"\x27\x02", "Key", [extended], [(locked, unlocked)]),
            service(b"\x2e\xf1\x90", "WriteVIN", [extended, unlocked])])
        with patch.object(odx_loader, "load_database", return_value=SimpleNamespace(ecus=[layer], base_variants=[])):
            d = odx_loader.load_odx("ecu.odx")
        self.assertEqual(sorted(d.sessions), [1, 3])
        self.assertEqual(d.dids[0xF190].write, Access({3}, {1}))
        self.assertEqual(d.services[0x27].access, Access({3}, set()))

    def test_a_dids_fields(self):
        def limit(value):
            return SimpleNamespace(value=value)

        def dop(bits, base, compu=None, constr=None, unit=None):
            return SimpleNamespace(diag_coded_type=SimpleNamespace(bit_length=bits, base_data_type=base),
                                   compu_method=compu, internal_constr=constr, unit=unit)
        texts = SimpleNamespace(category="CompuCategory.TEXTTABLE", compu_internal_to_phys=SimpleNamespace(
            compu_scales=[SimpleNamespace(lower_limit=limit(1), upper_limit=limit(1), compu_const=SimpleNamespace(vt="Default")),
                          SimpleNamespace(lower_limit=limit(3), upper_limit=limit(3), compu_const=SimpleNamespace(vt="Extended"))]))
        linear = SimpleNamespace(category="LINEAR", compu_internal_to_phys=SimpleNamespace(compu_scales=[
            SimpleNamespace(compu_rational_coeffs=SimpleNamespace(numerators=[-40, 0.5], denominators=[1]))]))
        parameters = [SimpleNamespace(short_name="SID", parameter_type="CODED-CONST", byte_position=0),
                      SimpleNamespace(short_name="DID", parameter_type="CODED-CONST", byte_position=1),
                      SimpleNamespace(short_name="Session", parameter_type="VALUE", byte_position=3, bit_position=0,
                                      dop=dop(8, "DataType.A_UINT32", texts)),
                      SimpleNamespace(short_name="Temperature", parameter_type="VALUE", byte_position=4, bit_position=4,
                                      dop=dop(4, "DataType.A_UINT32", linear, SimpleNamespace(lower_limit=limit(0),
                                                                                              upper_limit=limit(9)),
                                              SimpleNamespace(display_name="degC"))),
                      SimpleNamespace(short_name="Name", parameter_type="VALUE", byte_position=5,
                                      dop=dop(32, "DataType.A_ASCIISTRING"))]
        service = SimpleNamespace(positive_responses=[SimpleNamespace(parameters=parameters)])
        fields = odx_loader.did_fields(service)
        self.assertEqual([(f.name, f.position, f.bits, f.encoding) for f in fields],
                         [("Session", 0, 8, "unsigned"), ("Temperature", 8, 4, "unsigned"), ("Name", 16, 32, "ascii")])
        self.assertEqual(fields[0].texts, {1: "Default", 3: "Extended"})
        self.assertEqual((fields[1].scale, fields[1].shift, fields[1].valid, fields[1].unit), (0.5, -40.0, [(0, 9)], "degC"))
        broken = SimpleNamespace(positive_responses=[SimpleNamespace(parameters=[
            SimpleNamespace(short_name="X", parameter_type="VALUE", byte_position=None, dop=dop(8, "A_UINT32"))])])
        self.assertEqual(odx_loader.did_fields(broken), [], "a field without its place: none at all")

    def test_the_dummy_ecus_odx_describes_the_dummy_ecu(self):
        odx, dummy = odx_loader.load_odx(DUMMY_ODX), dummy_description()
        self.assertEqual(odx.warnings, [])
        self.assertEqual(odx.name, "Application", "the first ECU variant")
        self.assertEqual({s: (x.access, x.sub_functions) for s, x in odx.services.items()},
                         {s: (x.access, x.sub_functions) for s, x in dummy.services.items()})
        self.assertEqual({d: (x.name, x.length, x.read, x.write, x.fields, x.io) for d, x in odx.dids.items()},
                         {d: (x.name, x.length, x.read, x.write, x.fields, x.io) for d, x in dummy.dids.items()},
                         "read through odxtools: states by their IDs, names without _Read, ASCII texts")
        self.assertEqual(odx.routines, dummy.routines)
        self.assertEqual(odx.dtcs, dummy.dtcs, "its DTC-DOP: trouble codes and their texts")
        self.assertEqual({s: (x.name, x.entered_from) for s, x in odx.sessions.items()},
                         {s: (x.name, x.entered_from) for s, x in dummy.sessions.items()})

    def test_the_odx_file_is_written_by_the_tool(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("make_dummy_odx", Path(__file__).resolve().parents[1] / "tools" /
                                                      "make_dummy_odx.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        written = module.Writer(dummy_description(EcuConfig())).write()
        self.assertEqual(written.strip(), DUMMY_ODX.read_text(encoding="utf-8").strip(),
                         "ODX/dummy_ecu_services.odx-d is what tools/make_dummy_odx.py writes")

    def test_by_extension(self):
        self.assertEqual(odx_loader.load_description(DUMMY_CDD).name, "DummyECU (CommonDiagnostics)")


class VariantTest(unittest.TestCase):
    """A file's variants (a CDD's VARs, ODX ECU variants): reading the one chosen, and asking the ECU which it is."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def two_variants(self):
        start, end = OLD_STYLE_CDD.index("<VAR>"), OLD_STYLE_CDD.index("</VAR>") + len("</VAR>")
        boot = OLD_STYLE_CDD[start:end].replace("<QUAL>COMMON</QUAL>", "<QUAL>BOOT</QUAL>").replace("v='4660'", "v='4661'")
        path = self.folder / "two.cdd"
        path.write_text(OLD_STYLE_CDD[:end] + boot + OLD_STYLE_CDD[end:], encoding="iso-8859-1")
        return path

    def test_a_cdds_variants(self):
        path = self.two_variants()
        self.assertEqual(cdd_variants(path), ["COMMON", "BOOT"])
        first = load_cdd(path)
        self.assertEqual((first.variants, first.variant), (["COMMON", "BOOT"], "COMMON"), "the first, unless chosen")
        self.assertIn(0x1234, first.dids)
        boot = odx_loader.load_description(path, "BOOT")
        self.assertEqual(boot.variant, "BOOT")
        self.assertEqual((0x1235 in boot.dids, 0x1234 in boot.dids), (True, False))
        with self.assertRaises(CddError):
            load_cdd(path, "NONE")
        boot.save(self.folder / "boot.json")
        again = EcuDescription.load(self.folder / "boot.json")
        self.assertEqual((again.variants, again.variant), (["COMMON", "BOOT"], "BOOT"), "kept in a JSON description")

    def test_an_odx_files_variants(self):
        application = odx_loader.load_odx(DUMMY_ODX)
        self.assertEqual(application.variants, ["Application", "Bootloader", "DummyECU"], "ECU variants, then the base")
        self.assertEqual(application.variant, "Application")
        boot = odx_loader.load_description(DUMMY_ODX, "Bootloader")
        self.assertEqual(boot.variant, "Bootloader")
        self.assertIn(0x19, application.services)
        self.assertNotIn(0x19, boot.services, "the bootloader has no fault memory: NOT-INHERITED-DIAG-COMMS")
        self.assertIn(0x0201, application.routines)
        self.assertNotIn(0x0201, boot.routines)
        self.assertLess(len(Suite(boot).cases), len(Suite(application).cases))
        with self.assertRaises(ValueError):
            odx_loader.load_odx(DUMMY_ODX, "Nothing")

    def test_identification_values(self):
        told = Identification(0xF195, {"App": "APP-1.0.0", "Boot": "42 4F 4F 54"})
        self.assertEqual(Identification.from_dict(told.to_dict()), told)
        self.assertEqual(told.to_dict()["did"], "F195")
        self.assertEqual(Identification.from_dict(None), Identification())
        self.assertTrue(told.matches("APP-1.0.0", b"APP-1.0.0"))
        self.assertTrue(told.matches("42 4F 4F 54", b"BOOT"), "or its bytes, in hex")
        self.assertFalse(told.matches("APP-1.0.0", b"APP-2.0.0"))
        self.assertEqual((is_odx(DUMMY_ODX), is_odx(DUMMY_CDD), is_odx("found.json"), is_odx("")),
                         (True, False, False, False))

    def test_asking_the_ecu(self):
        bench = Bench(self)
        tester = Tester(bench.tester_bus, TRANSPORT, 0x7DF)
        variant, detail = identify(DUMMY_ODX, tester)
        self.assertEqual(variant, "Application", detail)
        self.assertIn("22 F1 95", detail, "the ECU-VARIANT-PATTERN's request")
        bench.ecu.state.bootloader = True
        self.assertEqual(identify(DUMMY_ODX, tester)[0], "Bootloader")
        told = Identification(0xF195, {"App": "APP-1.0.0", "Boot": BOOT_VERSION.hex(" ")})
        self.assertEqual(identify(DUMMY_CDD, tester, told), ("Boot", "F195 answers BOOTLOADER"), "a CDD: the plan's DID")
        bench.ecu.state.bootloader = False
        self.assertEqual(identify(DUMMY_CDD, tester, told)[0], "App")
        variant, detail = identify(DUMMY_CDD, tester)
        self.assertEqual(variant, None)
        self.assertIn("names no DID", detail)
        variant, detail = identify(DUMMY_CDD, tester, Identification(0x1234, {"App": "01"}))
        self.assertEqual(variant, None)
        self.assertIn("22 1234", detail)
        variant, detail = identify(DUMMY_CDD, tester, Identification(0xF195, {"Other": "APP-9"}))
        self.assertEqual(variant, None)
        self.assertIn("no variant expects it", detail)

    def test_in_a_plan_and_from_the_command_line(self):
        told = Identification(0xF195, {"Application": "APP-1.0.0"})
        plan = TestPlan("Variants", description=str(DUMMY_ODX), variant="Bootloader", identify=True,
                        identification=told)
        path = self.folder / "variants.json"
        plan.save(path)
        again = TestPlan.load(path)
        self.assertEqual((again.variant, again.identify, again.identification), ("Bootloader", True, told))
        self.assertEqual(again.load_description().variant, "Bootloader")
        keep = "sessions.default_session_10_01"
        again.excluded = [case.name for case in Suite(odx_loader.load_odx(DUMMY_ODX)).cases if case.name != keep]
        again.save(path)
        reports = self.folder / "reports"
        with patch("sys.stdout") as out:
            code = cli.main([str(path), "--run", "--dummy-ecu", "--report-dir", str(reports)])
        printed = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertEqual(code, cli.EXIT_PASSED, printed)
        self.assertIn("the ECU is the variant Application", printed, "asked, not the plan's Bootloader")
        with patch("sys.stdout") as out:
            code = cli.main([str(DUMMY_ODX), "--run", "--dummy-ecu", "--variant", "Nothing"])
        printed = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertEqual(code, cli.EXIT_NOT_RUN)
        self.assertIn("no variant 'Nothing'", printed)
        cdd = TestPlan("CDD", description=str(self.two_variants()), identify=True)
        cdd.save(path)
        with patch("sys.stdout") as out:
            code = cli.main([str(path), "--run", "--dummy-ecu"])
        printed = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertEqual(code, cli.EXIT_NOT_RUN, "a CDD without the plan's DID: it cannot be told")
        self.assertIn("could not be told", printed)


EXAMPLE_MODULE = TEST_MODULES_DIR / "dummy_ecu_checks.py"
# A module whose setup leaves the ECU in the extended session for its test cases, and whose clean-ups are logged.
HOOKS_MODULE = '''"""Hooks and state"""

def setup(t):
    t.require(DSC(0x03), "extended session")

def before_each(t):
    if t.result.name.endswith("needs_power"):
        t.block("no power")
    if t.result.name.endswith("before_fails"):
        t.require(False, "a precondition")
    t.log("before each")

def after_each(t):
    t.log("after each")
    if t.result.name.endswith("after_fails"):
        t.check(False, "the clean-up of the case")

def teardown(t):
    t.check(False, "a clean-up that does not work")

@testcase("Still in the extended session")
def still_extended(t):
    session = RDBI(0xF186)
    t.check_equal(session.data, bytes([3]), "the setup's session, not the default one")

@testcase
def needs_power(t):
    t.check(True, "never reached")

@testcase
def before_fails(t):
    t.check(True, "never reached")

@testcase
def after_fails(t):
    t.check(True, "done")
'''


class ModulesTest(unittest.TestCase):
    """CAN Expert's test modules in a TestExpert run: groups after the generated tests, with their hooks."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def module(self, text, name="hooks.py"):
        path = self.folder / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_modules_become_groups(self):
        modules = load_modules([EXAMPLE_MODULE, self.folder / "missing.py", self.module("def broken(:\n", "bad.py")])
        self.assertEqual([loaded.module is not None for loaded in modules], [True, False, False])
        self.assertIn("SyntaxError", modules[2].error)
        suite = Suite(dummy_description(), modules=modules)
        groups = suite.groups()
        self.assertEqual(list(groups)[-3:], ["Module: Dummy ECU checks", "Module: missing.py", "Module: bad.py"],
                         "after the generated tests")
        self.assertEqual([case.name for case in groups["Module: Dummy ECU checks"]],
                         ["module_dummy_ecu_checks.identification", "module_dummy_ecu_checks.unknown_identifier",
                          "module_dummy_ecu_checks.security_access", "module_dummy_ecu_checks.fault_memory",
                          "module_dummy_ecu_checks.engine_data"])
        self.assertEqual(groups["Module: missing.py"][0].name, "module_missing.read")
        twice = Suite(dummy_description(), modules=load_modules([EXAMPLE_MODULE, EXAMPLE_MODULE]))
        self.assertIn("Module: Dummy ECU checks (dummy_ecu_checks.py)", twice.groups())
        self.assertEqual(len({case.name for case in twice.cases}), len(twice.cases), "names stay unique")

    def test_the_example_module_in_a_run(self):
        bench = Bench(self, EcuConfig(lockout_seconds=1))            # its application frames too
        plan = TestPlan("Modules", modules=[str(EXAMPLE_MODULE), str(self.folder / "missing.py")],
                        symbols=[str(DBC_DIR / "dummy_ecu.dbc")])
        run = PlanRun(plan, dummy_description(), bench.tester_bus,
                      names=["sessions.default_session_10_01"] + [case.name for case in Suite(
                          dummy_description(), modules=load_modules(plan.module_paths())).cases
                          if case.name.startswith("module_")])
        report = run.run()
        verdicts = {case.name: case.verdict for case in report.cases}
        self.assertEqual(verdicts.pop("module_missing.read"), "failed", "a module that cannot be read")
        self.assertEqual(set(verdicts.values()), {"passed"}, failures(report))
        engine = next(case for case in report.cases if case.name == "module_dummy_ecu_checks.engine_data")
        self.assertIn("the temperature is between -40 and 150 degC", [step.description for step in engine.steps],
                      "decoded with the plan's DBC")
        self.assertIn("Dummy ECU checks: teardown", [step.description for step in engine.steps])
        facts = dict(run.facts())
        self.assertEqual(facts["Test modules"], "dummy_ecu_checks.py, missing.py (not read)")
        self.assertEqual(facts["Symbol databases"], "dummy_ecu.dbc")
        self.assertIn("module_dummy_ecu_checks.identification", run.suite.coverage.dids[(0xF190, 1, "read")].tests,
                      "what a module asked counts for the coverage")

    def test_a_modules_hooks(self):
        bench = Bench(self)
        plan = TestPlan("Hooks", modules=[str(self.module(HOOKS_MODULE))])
        run = PlanRun(plan, dummy_description(), bench.tester_bus,
                      names=["testerpresent.testerpresent_3e"] + [f"module_hooks.{name}" for name in (
                          "still_extended", "needs_power", "before_fails", "after_fails")])
        report = run.run()
        cases = {case.name: case for case in report.cases}
        extended = cases["module_hooks.still_extended"]
        self.assertEqual(extended.verdict, "passed", failures(report))
        self.assertEqual([step.description for step in extended.steps][:3],
                         ["Hooks and state: setup", "extended session", "before each"])
        self.assertEqual((cases["module_hooks.needs_power"].verdict, cases["module_hooks.needs_power"].error),
                         ("blocked", "no power"))
        before = cases["module_hooks.before_fails"]
        self.assertEqual(before.verdict, "failed")
        self.assertNotIn("never reached", [step.description for step in before.steps], "the case is not run")
        after = cases["module_hooks.after_fails"]
        self.assertEqual(after.verdict, "failed", "after_each's failures fail the case, as in CAN Expert")
        teardown = next(step for step in after.steps if step.description == "a clean-up that does not work")
        self.assertEqual(teardown.verdict, "warn", "the teardown's: a warning")
        self.assertEqual(cases["testerpresent.testerpresent_3e"].verdict, "passed")

        failing = self.module(HOOKS_MODULE.replace("DSC(0x03)", "DSC(0x7E)"), "failing.py")
        plan.modules = [str(failing)]
        run = PlanRun(plan, dummy_description(), bench.tester_bus,
                      names=["module_failing.still_extended", "module_failing.after_fails"])
        report = run.run()
        self.assertEqual([case.verdict for case in report.cases], ["blocked", "blocked"], "the setup failed")
        self.assertIn("the module's setup failed: extended session", report.cases[1].error)
        self.assertEqual(report.cases[1].steps[-2].description, "Hooks and state: teardown", "it still ends")

        skipping = self.module(HOOKS_MODULE.replace('t.require(DSC(0x03), "extended session")',
                                                    't.skip("not on this bench")'), "skipping.py")
        plan.modules = [str(skipping)]
        report = PlanRun(plan, dummy_description(), bench.tester_bus, names=["module_skipping.still_extended"]).run()
        self.assertEqual((report.cases[0].verdict, report.cases[0].error), ("skipped", "not on this bench"))

    def test_a_stopped_run_still_ends_the_module(self):
        bench = Bench(self)
        plan = TestPlan("Stopped", modules=[str(self.module(HOOKS_MODULE))])

        def on_event(kind, data):
            if kind == "verdict" and data.name == "module_hooks.still_extended":
                run.stop()
        run = PlanRun(plan, dummy_description(), bench.tester_bus, on_event=on_event,
                      names=["module_hooks.still_extended", "module_hooks.before_fails", "module_hooks.after_fails"])
        report = run.run()
        self.assertTrue(report.stopped)
        self.assertEqual(report.cases[-1].error, "stopped", "its last test case was not reached")
        self.assertIn("Hooks and state: teardown", [step.description for step in report.teardown.steps],
                      "the run's end ends the module")

    def test_frames_and_symbols(self):
        message = can.Message(arbitration_id=0x300, data=bytes(8), timestamp=time.time() - 5)
        tester = SimpleNamespace(bus=SimpleNamespace(recv=lambda timeout=None: message))
        arrived, frame = BusFrames(tester).get(0.1)
        self.assertIs(frame, message)
        self.assertAlmostEqual(time.perf_counter() - arrived, 5, delta=0.5, msg="when it came, not when it was read")
        message.timestamp = 1000.0                                  # an adapter's own clock
        self.assertAlmostEqual(BusFrames(tester).get(0.1)[0], time.perf_counter(), delta=0.5)
        tester.bus.recv = lambda timeout=None: None
        with self.assertRaises(Exception):
            BusFrames(tester).get(0.1)
        symbols = Symbols([DBC_DIR / "dummy_ecu.dbc", self.folder / "missing.dbc"])
        name, signals = symbols.decode(0x300, bytes(8))
        self.assertEqual(name, "EngineData")
        self.assertIn("Temperature", signals)
        self.assertTrue(symbols.errors, "the missing file")
        self.assertEqual(Symbols().decode(0x300, bytes(8)), ("", {}))

    def test_in_a_plan_and_from_the_command_line(self):
        plans = self.folder / "plans"
        plans.mkdir()
        module = self.module(HOOKS_MODULE)
        plan = TestPlan("Modules", modules=[str(module)], symbols=[str(DBC_DIR / "dummy_ecu.dbc")])
        plan.save(plans / "modules.json")
        plan.modules = [plan.relative(module)]
        plan.save()
        self.assertEqual(TestPlan.load(plans / "modules.json").modules, ["../hooks.py"])
        moved = self.folder / "elsewhere" / "deeper"
        moved.mkdir(parents=True)
        plan.save(moved / "modules.json")
        again = TestPlan.load(moved / "modules.json")
        self.assertEqual(again.module_paths(), [module.resolve()], "relative paths follow the plan")
        self.assertEqual(again.symbol_paths(), [(DBC_DIR / "dummy_ecu.dbc").resolve()])
        keep = "sessions.default_session_10_01"
        suite = Suite(dummy_description(), TestPlan().make_options(), modules=load_modules([EXAMPLE_MODULE]))
        cli_plan = TestPlan("CLI", excluded=[case.name for case in suite.cases if case.name != keep
                                             and not case.name.startswith("module_dummy_ecu_checks.")])
        cli_plan.options["s3_test"] = False
        path = self.folder / "cli.json"
        cli_plan.save(path)
        with patch("sys.stdout") as out:
            code = cli.main([str(path), "--run", "--dummy-ecu", "--module", str(EXAMPLE_MODULE), "--symbols",
                             str(DBC_DIR / "dummy_ecu.dbc"), "--report-dir", str(self.folder / "reports")])
        printed = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertEqual(code, cli.EXIT_PASSED, printed)
        self.assertIn("Running 6 tests", printed)
        self.assertIn("PASSED   Module: Dummy ECU checks: Engine data is broadcast with a plausible temperature",
                      printed)


def transport_names(suite):
    return [case.name for case in suite.groups()[TRANSPORT_GROUP]]


class TransportTest(unittest.TestCase):
    """ISO 15765-2: the ECU taking segmented requests and sending segmented answers."""

    def run_on(self, test_config, transport=None, description=None):
        bench = Bench(self, test_config)
        suite = Suite(description or dummy_description(test_config), Options(key=key, s3_test=False))
        suite.tester = Tester(bench.tester_bus, transport or TRANSPORT, 0x7DF)
        names = transport_names(suite)
        return names, Runner(suite.module(names), send=suite.tester.send_frame).run(names)

    def test_the_dummy_ecu_keeps_the_rules(self):
        names, report = self.run_on(EcuConfig(broadcast_interval=0))
        self.assertEqual(len(names), 12)
        self.assertEqual(report.verdict, "passed", failures(report))
        self.assertIn("transport_layer_iso_15765_2.a_new_request_during_a_segmented_one", names)

    def test_extended_addressing_no_padding_and_no_block_limit(self):
        config = EcuConfig(broadcast_interval=0, address_byte=0x55, padding=None, block_size=0, st_min=0)
        transport = {**TRANSPORT, "address_byte": 0x55, "padding": None}
        names, report = self.run_on(config, transport)
        self.assertEqual(report.verdict, "passed", failures(report))

    def test_an_ecu_that_ignores_flow_control_is_found(self):
        from canexpert.simulator import ecu as ecu_module
        from canexpert.uds import isotp

        def careless(bus, request_id, payload, response_id, extended=False, address_byte=None, padding=None):
            """Every frame at once, whatever the tester's flow control says."""
            payload = bytes(payload)
            if len(payload) <= 7:
                return isotp.isotp_send(bus, request_id, payload, response_id, extended, address_byte, padding)
            first, rest = payload[:6], payload[6:]
            isotp._send_frame(bus, request_id, bytes([0x10 | len(payload) >> 8, len(payload) & 0xFF]) + first,
                              extended, address_byte, padding)
            for number, offset in enumerate(range(0, len(rest), 7)):
                isotp._send_frame(bus, request_id, bytes([0x20 | (number + 1) & 0x0F]) + rest[offset:offset + 7],
                                  extended, address_byte, padding)
        with patch.object(ecu_module, "isotp_send", careless):
            names, report = self.run_on(EcuConfig(broadcast_interval=0))
        verdicts = {case.title.split(": ", 1)[1]: case.verdict for case in report.cases}
        for title in ("The tester's block size", "The tester's STmin", "A flow control WAIT",
                      "A flow control overflow", "A reserved flow status", "No flow control"):
            self.assertEqual(verdicts[title], "failed", title)
        self.assertEqual(verdicts["A segmented request: flow control and answer"], "passed", "it takes requests well")

    def test_a_reserved_stmin_is_found(self):
        from canexpert.simulator import ecu as ecu_module
        with patch.object(ecu_module.DummyEcu, "_continue_to_send",
                          lambda ecu: ecu._send_frame(b"\x30\x00\x80")):
            names, report = self.run_on(EcuConfig(broadcast_interval=0))
        case = next(case for case in report.cases if case.title.endswith("A segmented request: flow control and answer"))
        self.assertEqual(case.verdict, "failed")
        self.assertIn("its STmin (80) is not a reserved value", [step.description for step in case.failures()])

    def test_what_the_description_allows(self):
        description = dummy_description()
        self.assertNotIn(TRANSPORT_GROUP, Suite(description, Options(transport=False)).groups())
        for did in list(description.dids):
            if (description.dids[did].length or 0) >= 11:
                del description.dids[did]
        titles = [case.title.split(": ", 1)[1] for case in Suite(description).groups()[TRANSPORT_GROUP]]
        self.assertEqual(len(titles), 6, "no DID answered in several frames: the ECU is not tried as a sender")
        self.assertNotIn("No flow control", titles)
        description.services.pop(0x22)
        self.assertEqual(TransportTestsRequest(description), b"\x3e\x00" + bytes(7),
                         "without a DID to read: a TesterPresent too long (answered 0x13)")

    def test_frames_and_their_timing(self):
        self.assertEqual([valid_st_min(value) for value in (0x00, 0x7F, 0x80, 0xF0, 0xF1, 0xF9, 0xFA, 0xFF)],
                         [True, True, False, False, True, True, False, False])
        self.assertEqual([st_min_seconds(value) for value in (0x32, 0xF5, 0x90)], [0.05, 0.0005, 0.127])
        earlier, later = Frame(b"\x21", 1.000, 100.000, 8), Frame(b"\x22", 1.040, 100.050, 8)
        self.assertAlmostEqual(gap(earlier, later), 0.050, msg="the longer of the two clocks")
        self.assertAlmostEqual(gap(Frame(b"\x21", 1.0, 0.0, 8), Frame(b"\x22", 1.03, 0.0, 8)), 0.03)
        link = Link(SimpleNamespace(transport=TRANSPORT, bus=None, functional_id=0x7DF))
        first, consecutive = link.segments(bytes(range(1, 21)))
        self.assertEqual(first, bytes([0x10, 20, 1, 2, 3, 4, 5, 6]))
        self.assertEqual([frame[0] for frame in consecutive], [0x21, 0x22])
        self.assertEqual(consecutive[1], bytes([0x22, 14, 15, 16, 17, 18, 19, 20]))
        extended = Link(SimpleNamespace(transport={**TRANSPORT, "address_byte": 0x55}, bus=None, functional_id=None))
        self.assertEqual(extended.room, 6)
        self.assertEqual(extended.segments(bytes(8))[0], bytes([0x10, 8]) + bytes(5))


SERVICE_GROUPS = ("Download and upload", "Memory by address", "Periodic data", "ResponseOnEvent", "Communication",
                  "Input/output control", "Fault memory")


class ServicesTest(unittest.TestCase):
    """Download, memory, periodic data, ResponseOnEvent and CommunicationControl taken further."""

    OPTIONS = {"destructive": True, "download": "10000:300", "memory": "10000:10", "transport": False}

    def run_groups(self, config=None, description=None, **options):
        config = config or EcuConfig(lockout_seconds=1, require_erase=False)       # its frames too
        bench = Bench(self, config)
        suite = Suite(description or dummy_description(config), Options(key=key, s3_test=False,
                                                                          **{**self.OPTIONS, **options}))
        suite.tester = Tester(bench.tester_bus, TRANSPORT, 0x7DF)
        names = [case.name for case in suite.cases if suite.group_of[case.name] in SERVICE_GROUPS]
        report = Runner(suite.module(names), send=suite.tester.send_frame).run(names)
        return {case.title.split(": ", 1)[1]: case for case in report.cases}, report

    def test_the_dummy_ecu_passes(self):
        cases, report = self.run_groups()
        self.assertEqual(report.verdict, "passed", failures(report))
        self.assertEqual({title: case.verdict for title, case in cases.items() if case.verdict != "passed"}, {})
        for title in ("A download started, then refused out of order", "WriteMemoryByAddress of the same bytes",
                      "WriteMemoryByAddress while locked", "Periodic data of F201", "An event on a DID that changes",
                      "CommunicationControl stops the ECU's own frames", "IO control of 0100, which has none",
                      "IO control of 0101 Temperature", "The ECU's DTCs are the description's"):
            self.assertIn(title, cases)
        started = [step.description for step in cases["A download started, then refused out of order"].steps]
        self.assertIn("a second RequestDownload while one runs: NRC 0x22", started)
        self.assertIn("0100 changes by itself", [step.description for step in cases["An event on a DID that changes"].steps])

    def test_what_they_need(self):
        titles = [case.title for case in Suite(dummy_description(), Options(transport=False)).cases]
        self.assertFalse([title for title in titles if "download started" in title or "plan's memory" in title
                          or title.startswith("Memory by address: WriteMemoryByAddress")],
                         "without the plan's ranges, or not destructive")
        self.assertIn("Download and upload: RequestDownload of a format the ECU does not take", titles)
        self.assertEqual(parse_memory_range(" 0x10000 : 300 "), (0x10000, 0x300))
        self.assertIsNone(parse_memory_range(""))
        for wrong in ("10000", "zz:1", "10000:0", "100000000:1"):
            with self.assertRaises(ValueError, msg=wrong):
                parse_memory_range(wrong)
        plan = TestPlan()
        plan.options["memory"] = "F000"
        with self.assertRaises(PlanError):
            plan.check()
        with patch("sys.stdout"), tempfile.TemporaryDirectory() as folder:
            plan.save(Path(folder) / "plan.json")
            self.assertEqual(cli.main([str(Path(folder) / "plan.json"), "--run", "--dummy-ecu"]), cli.EXIT_NOT_RUN)

    def test_an_ecu_that_breaks_the_rules_is_found(self):
        from canexpert.simulator import ecu as ecu_module
        config = EcuConfig(lockout_seconds=1, require_erase=False, forced_nrcs=[{"sid": 0x36, "nrc": 0x22}])
        with patch.object(ecu_module.DummyEcu, "_service_28", lambda ecu, request: bytes([0x68, request[1]])), \
                patch.object(ecu_module.DummyEcu, "_send_periodic", lambda ecu, now: None):
            cases, report = self.run_groups(config)
        self.assertEqual(cases["TransferData and RequestTransferExit before RequestDownload"].verdict, "failed")
        self.assertEqual(cases["CommunicationControl stops the ECU's own frames"].verdict, "failed",
                         "its frames go on")
        self.assertEqual(cases["Periodic data of F201"].verdict, "failed", "no periodic data")
        self.assertEqual(cases["RequestDownload of a format the ECU does not take"].verdict, "passed")

    def test_io_control_and_dtcs_that_differ_are_found(self):
        from canexpert.simulator import ecu as ecu_module
        config = EcuConfig(lockout_seconds=1, require_erase=False)
        description = dummy_description(config)
        del description.dtcs[0xC10000]
        description.dtcs[0x123456] = "not in the ECU"
        with patch.object(ecu_module.DummyEcu, "_service_2f", lambda ecu, request: bytes([0x7F, 0x2F, 0x31])):
            cases, report = self.run_groups(config, description)
        dtcs = cases["The ECU's DTCs are the description's"]
        self.assertEqual({step.description: step.detail for step in dtcs.failures()},
                         {"every DTC the ECU supports is in the description": "not described: U0100-00",
                          "every DTC of the description is supported": "not supported: 123456 not in the ECU"})
        self.assertEqual(cases["IO control of 0101 Temperature"].verdict, "failed")
        self.assertEqual(cases["IO control of 0100, which has none"].verdict, "passed")

    def test_without_frames_of_its_own(self):
        cases, report = self.run_groups(EcuConfig(lockout_seconds=1, require_erase=False, broadcast_interval=0),
                                        destructive=False)
        self.assertEqual(cases["CommunicationControl stops the ECU's own frames"].verdict, "skipped")
        self.assertEqual(report.verdict, "passed", failures(report))


TWO_LEVELS = EcuConfig(lockout_seconds=1.5, security_levels=[{"level": 0x03, "seed_length": 4, "key_mask": 0x5A}],
                       dids=[*EcuConfig().dids, {"did": 0x0300, "data": "0102", "writable": False, "level": 3}])


def two_keys(level, seed):
    return bytes(byte ^ (0xA5 if level == 0x01 else 0x5A) for byte in seed)


class SecurityTest(unittest.TestCase):
    """Seeds that do not repeat, keys of the wrong length, levels apart, a lockout outlasting a reset."""

    def run_security(self, config=TWO_LEVELS, **options):
        bench = Bench(self, config)
        options = {"key": two_keys, "lockout": True, "destructive": True, "lockout_seconds": 1.5, "reset_time": 0.6,
                   "transport": False, **options}
        suite = Suite(dummy_description(config), Options(s3_test=False, **options))
        suite.tester = Tester(bench.tester_bus, TRANSPORT, 0x7DF)
        names = [case.name for case in suite.cases if suite.group_of[case.name] == "Security access"]
        report = Runner(suite.module(names), send=suite.tester.send_frame).run(names)
        return {case.title.split(": ", 1)[1]: case for case in report.cases}, report

    def test_the_dummy_ecu_passes(self):
        cases, report = self.run_security()
        self.assertEqual(report.verdict, "passed", failures(report))
        for title in ("Level 0x01: seeds do not repeat", "Level 0x03: a key of the wrong length",
                      "Level 0x01: the lockout outlasts an ECU reset", "Levels 0x01 and 0x03 are apart"):
            self.assertIn(title, cases)
        apart = [step.description for step in cases["Levels 0x01 and 0x03 are apart"].steps]
        self.assertIn("22 0300 (level 0x03 only) is still refused: NRC 0x33", apart)
        self.assertIn("level 0x03 unlocked: 22 0300 is read", apart)
        length = cases["Level 0x01: a key of the wrong length"].steps
        self.assertIn("the key with a byte too many (5 bytes): NRC 0x13", [step.description for step in length])

    def test_what_they_need(self):
        titles = [case.title for case in Suite(dummy_description(), Options(key=None)).cases]
        self.assertIn("Security access: Level 0x01: seeds do not repeat", titles, "no key needed")
        self.assertFalse([title for title in titles if "wrong length" in title or "outlasts" in title
                          or "are apart" in title], "without a key, lockout and destructive tests, or a second level")
        self.assertEqual(NrcPolicy().accepted("key_wrong_length"), (0x13, 0x35),
                         "taken for a wrong key: accepted with a note")
        quick = Suite(dummy_description(), Options(key=key, lockout=True, destructive=True, lockout_seconds=1,
                                                   reset_time=1.0))
        self.assertFalse([case for case in quick.cases if "outlasts" in case.title],
                         "a delay no longer than the reset: nothing left to check after it")

    def test_an_ecu_that_breaks_them_is_found(self):
        from canexpert.simulator import ecu as ecu_module

        def forgetful_reset(ecu):
            ecu.state.locked_until = 0.0                      # the lockout forgotten
            original_reset(ecu)
        original_reset = ecu_module.DummyEcu._reset
        with patch.object(ecu_module, "os", SimpleNamespace(urandom=lambda length: b"\x12" * length)), \
                patch.object(ecu_module.DummyEcu, "_reset", forgetful_reset), \
                patch.object(ecu_module.DummyEcu, "_check_did_access", lambda ecu, did: None):
            cases, report = self.run_security()
        for title in ("Level 0x01: seeds do not repeat", "Level 0x01: the lockout outlasts an ECU reset",
                      "Levels 0x01 and 0x03 are apart"):
            self.assertEqual(cases[title].verdict, "failed", title)
        self.assertEqual(cases["Level 0x01: a key of the wrong length"].verdict, "passed")


def TransportTestsRequest(description):
    from canexpert.test_expert.transport import TransportTests
    return TransportTests(Suite(description, Options(transport=False))).request


class Bench:
    def __init__(self, test, config=None):
        channel = "te-" + str(uuid.uuid4())
        self.tester_bus = can.Bus(interface="virtual", channel=channel)
        ecu_bus = can.Bus(interface="virtual", channel=channel)
        self.ecu = DummyEcu(ecu_bus, config or EcuConfig(lockout_seconds=1),        # its own frames too
                            log=lambda text: None)
        stop = threading.Event()
        threading.Thread(target=self.ecu.serve, args=(stop,), daemon=True).start()
        self.channel = channel

        def close():
            stop.set()
            time.sleep(0.05)
            self.tester_bus.shutdown()
            ecu_bus.shutdown()
        test.addCleanup(close)

    def run(self, description, names=None, sequences=(), base_dir=None, policy=None, deviations=(), **options):
        options.setdefault("key", key)
        options.setdefault("s3_test", False)
        suite = Suite(description, Options(**options), sequences, base_dir, policy)
        suite.tester = Tester(self.tester_bus, TRANSPORT, 0x7DF)
        runner = Runner(suite.module(names), send=suite.tester.send_frame, accept=accept_function(deviations))
        return suite, runner.run(names)


def failures(report):
    return {case.title: [step.description for step in case.failures()] for case in report.cases
            if case.verdict != "passed"}


class AgainstTheDummyEcuTest(unittest.TestCase):
    def test_the_dummy_ecu_keeps_every_rule(self):
        bench = Bench(self)
        suite, report = bench.run(dummy_description(bench.ecu.config))
        self.assertEqual(failures(report), {})
        groups = set(suite.groups())
        self.assertLessEqual({"Sessions", "TesterPresent", "Services", "Service availability", "NRC order",
                              "Message length", "Sub-functions", "Data identifiers", "Security access", "Routines",
                              "Fault memory", "Communication", "Functional addressing", "Timing"}, groups)

    def test_the_destructive_tests_and_the_lockout(self):
        bench = Bench(self)
        suite, report = bench.run(load_cdd(DUMMY_CDD), destructive=True, lockout=True, lockout_seconds=1)
        self.assertEqual(failures(report), {})
        titles = [case.title for case in report.cases]
        self.assertIn("ECU reset: ECUReset (11 01)", titles)
        self.assertTrue(any(title.startswith("Security access: Level1: lockout") for title in titles))
        self.assertTrue(any(title.startswith("Data identifiers: Write") for title in titles))

    def test_without_a_key_the_unlocking_is_skipped(self):
        bench = Bench(self)
        _suite, report = bench.run(dummy_description(bench.ecu.config), key=None)
        skipped = {case.title: case.error for case in report.cases if case.verdict == "skipped"}
        self.assertEqual(set(skipped.values()), {"no key source set for SecurityAccess"})
        self.assertIn("Data identifiers: Values of CalibrationId (0200)", skipped, "a DID read once unlocked")
        self.assertIn("Routines: EraseMemory (FF00): stop and results before a start", skipped)
        self.assertEqual({title: steps for title, steps in failures(report).items() if title not in skipped}, {})
        security = next(case for case in report.cases if case.title.startswith("Security access"))
        self.assertIn("No key source", [step.description for step in security.steps][-1])

    def test_what_is_wrong_is_found(self):
        config = EcuConfig(broadcast_interval=0, forced_nrcs=[{"sid": 0x85, "nrc": 0x22}], p2_ms=50)
        bench = Bench(self, config)
        description = dummy_description(EcuConfig())
        description.dids[0xF190].length = 16                                  # the description says otherwise
        _suite, report = bench.run(description)
        found = failures(report)
        self.assertIn("Communication: ControlDTCSetting (85)", found)
        self.assertIn("Read VIN (F190)", "".join(found))
        self.assertTrue(any("17 bytes" in step.detail or "20 bytes" in step.detail
                            for case in report.cases for step in case.failures()))


class SequenceTest(unittest.TestCase):
    def test_step_values(self):
        self.assertEqual(parse_hex("11 01"), b"\x11\x01")
        self.assertEqual(parse_hex("0x14,0xFF ff FF"), b"\x14\xff\xff\xff")
        self.assertEqual(parse_hex("22F190"), b"\x22\xf1\x90")
        self.assertEqual(parse_expect("NRC 0x22"), ("nrc", 0x22))
        self.assertEqual(parse_expect("31"), ("nrc", 0x31))
        self.assertEqual(parse_expect("no answer"), ("none", None))
        self.assertEqual(parse_frame("12F 01 02"), (0x12F, b"\x01\x02", False))
        self.assertEqual(parse_frame("18FEF100#0102"), (0x18FEF100, b"\x01\x02", True))
        self.assertEqual(parse_frame("012F"), (0x12F, b"", True), "four digits: an extended identifier")
        self.assertEqual(parse_script(r"C:\bench\power.py:cycle"), (r"C:\bench\power.py", "cycle"))
        self.assertEqual(parse_script("power.py"), ("power.py", "run"))
        for text in ("zz", "11 1FF"):
            with self.assertRaises(SequenceError):
                parse_hex(text)
        self.assertEqual(SequenceStep("unlock", "02").problem(), "requestSeed levels are odd")
        self.assertIn("not seconds", SequenceStep("wait", "soon").problem())
        self.assertEqual(SequenceStep("reset", "").problem(), "", "a hard reset by default")
        self.assertEqual(SequenceStep("request", "10 03", "NRC 7F").problem(), "")
        sequence = Sequence("Bench", [SequenceStep("wait", "x")], [Attachment("after", "test", "a.b", "failed")])
        self.assertEqual(Sequence.from_dict(sequence.to_dict()), sequence)
        self.assertEqual(sequence.problems(), ["step 1: not seconds: 'x'"])

    def test_where_sequences_run(self):
        always = Sequence("Always", [], [Attachment("after", "each")])
        failed = Sequence("On failure", [], [Attachment("after", "each", condition="failed")])
        group = Sequence("Group", [], [Attachment("before", "group", "Sessions")])
        off = Sequence("Off", [], [Attachment("after", "each")], enabled=False)
        sequences = [always, failed, group, off]
        self.assertEqual(due(sequences, "after", "each", outcome="passed"), [always])
        self.assertEqual(due(sequences, "after", "each", outcome="failed"), [always, failed])
        self.assertEqual(due(sequences, "after", "each", outcome=None), [always], "a skipped test")
        self.assertEqual(due(sequences, "before", "group", "Sessions"), [group])
        self.assertEqual(due(sequences, "before", "group", "Timing"), [])

    def test_around_tests_against_the_dummy_ecu(self):
        bench = Bench(self)
        description = dummy_description(bench.ecu.config)
        names = ["sessions.enter_the_extended_session_10_03", "sessions.suppress_positive_response",
                 "testerpresent.testerpresent_3e", "timing.responses_within_p2"]
        folder = Path(tempfile.mkdtemp())
        (folder / "bench.py").write_text("def power(t, tester):\n    t.log('power cycled')\n\n"
                                         "def broken(t, tester):\n    return False\n", encoding="utf-8")
        sequences = [
            Sequence("Ignition on", [SequenceStep("frame", "200 01"), SequenceStep("wait", "0.05"),
                                     SequenceStep("script", "bench.py:power")], [Attachment("before", "run")]),
            Sequence("Hard reset", [SequenceStep("reset", "01")],
                     [Attachment("after", "test", "sessions.enter_the_extended_session_10_03")]),
            Sequence("Extended", [SequenceStep("session", "03"), SequenceStep("request", "22 F1 86", "positive")],
                     [Attachment("before", "group", "TesterPresent")]),
            Sequence("Wrong", [SequenceStep("request", "22 12 34", "positive")],
                     [Attachment("before", "test", "sessions.suppress_positive_response")]),
            Sequence("Cleanup", [SequenceStep("request", "22 12 34", "NRC 22"), SequenceStep("wait", "5")],
                     [Attachment("after", "group", "Timing")]),
            Sequence("After a failure", [SequenceStep("script", "bench.py:broken")],
                     [Attachment("after", "each", condition="failed")]),
            Sequence("Done", [SequenceStep("request", "3E 80", "no answer")], [Attachment("after", "run")]),
        ]
        _suite, report = bench.run(description, names, sequences, folder, reset_time=0.6)
        cases = {case.name: case for case in report.cases}
        setup = [step.description for step in report.setup.steps]
        self.assertIn("Pre-run 'Ignition on': frame 200 01 sent", setup)
        self.assertIn("Pre-run 'Ignition on': bench.py:power()", setup)
        self.assertTrue(bench.ecu.running, "the frame reached the ECU: 200 01 starts its application")
        extended = cases["sessions.enter_the_extended_session_10_03"]
        self.assertEqual(extended.verdict, "passed")
        self.assertEqual(extended.steps[-1].description,
                         "Post-test 'Hard reset': ECU reset (11 01) and the ECU back after 0.6 s")
        self.assertEqual(extended.steps[-1].verdict, "pass")
        blocked = cases["sessions.suppress_positive_response"]
        self.assertEqual(blocked.verdict, "blocked")
        self.assertEqual(blocked.error, "the pre-test sequence 'Wrong' failed")
        self.assertFalse(any(step.description.startswith("10 81") for step in blocked.steps), "not run")
        self.assertEqual(blocked.steps[-1].description, "Post-test 'After a failure': bench.py:broken()",
                         "after a test that did not pass")
        self.assertEqual(blocked.steps[-1].verdict, "warn")
        tester_present = cases["testerpresent.testerpresent_3e"]
        self.assertEqual([step.description for step in tester_present.steps[:2]],
                         ["Pre-group 'Extended': Extended session entered (10 03)",
                          "Pre-group 'Extended': 22 F1 86 answered positively"])
        self.assertEqual(tester_present.verdict, "passed")
        timing = cases["timing.responses_within_p2"]
        self.assertEqual(timing.verdict, "passed", "a post-group warning leaves the verdict")
        self.assertEqual(timing.steps[-1].verdict, "warn")
        self.assertIn("22 12 34 answered NRC 0x22", timing.steps[-1].description)
        self.assertFalse(any("wait 5 s" in step.description for step in timing.steps), "stopped at its failure")
        self.assertFalse(any("After a failure" in step.description for step in timing.steps))
        self.assertEqual(report.teardown.steps[-1].description, "Post-run 'Done': 3E 80 is not answered")
        self.assertEqual(report.verdict, "failed", "the blocked test")

    def test_a_failing_pre_group_sequence_blocks_the_group(self):
        bench = Bench(self)
        sequences = [Sequence("Unreachable", [SequenceStep("session", "7E")], [Attachment("before", "group", "Sessions")])]
        names = ["sessions.default_session_10_01", "sessions.message_length", "testerpresent.testerpresent_3e"]
        _suite, report = bench.run(dummy_description(bench.ecu.config), names, sequences)
        verdicts = {case.name: (case.verdict, case.error) for case in report.cases}
        self.assertEqual(verdicts, {
            "sessions.default_session_10_01": ("blocked", "the pre-group sequence 'Unreachable' failed"),
            "sessions.message_length": ("blocked", "the pre-group sequence 'Unreachable' failed"),
            "testerpresent.testerpresent_3e": ("passed", "")})

    def test_a_failing_pre_run_sequence_stops_the_run(self):
        bench = Bench(self)
        sequences = [Sequence("Unlock", [SequenceStep("unlock", "01")], [Attachment("before", "run")])]
        _suite, report = bench.run(dummy_description(bench.ecu.config), ["sessions.message_length"], sequences)
        self.assertEqual(report.setup.verdict, "failed", "SecurityAccess is not allowed in the default session")
        self.assertEqual(report.cases[0].verdict, "skipped")
        self.assertEqual(report.verdict, "failed")


class PolicyTest(unittest.TestCase):
    READ_ONLY = ["data_identifiers.writing_a_read_only_did"]

    def test_the_policy_and_deviations_as_values(self):
        self.assertEqual(parse_nrcs("31, 0x7F 22"), (0x31, 0x7F, 0x22))
        with self.assertRaises(ValueError):
            parse_nrcs("131")
        policy = NrcPolicy()
        self.assertEqual(policy.accepted("did_not_in_session"), (0x31,))
        self.assertEqual(policy.accepted("routine_not_in_session"), (0x31, 0x7F, 0x7E), "what TestExpert tolerated")
        policy.set("did_not_in_session", (0x31, 0x7F))
        policy.set("locked", (0x33,))                                         # the default: nothing kept
        self.assertEqual(policy.to_dict(), {"did_not_in_session": ["31", "7F"]})
        self.assertEqual(NrcPolicy.from_dict(policy.to_dict()), policy)
        deviations = [Deviation("sessions.message_length", "10 alone: incorrect length", "ticket 7"),
                      Deviation("timing.responses_within_p2", "*", "")]
        accept = accept_function(deviations)
        self.assertEqual(accept("sessions.message_length", "10 alone: incorrect length"), "ticket 7")
        self.assertIsNone(accept("sessions.message_length", "10 01 00: one byte too many"))
        self.assertEqual(accept("timing.responses_within_p2", "anything"), "")
        self.assertIsNone(accept_function([]), "no deviations: nothing to ask")

    def test_what_the_ecu_answers_instead(self):
        config = EcuConfig(broadcast_interval=0, forced_nrcs=[{"sid": 0x2E, "nrc": 0x7F}])
        bench = Bench(self, config)
        description = dummy_description(EcuConfig())
        _suite, report = bench.run(description, self.READ_ONLY)
        (case,) = report.cases
        self.assertEqual(case.verdict, "failed", "0x7F where ISO 14229-1 asks for 0x31")
        self.assertIn("expected NRC 0x31 requestOutOfRange", case.steps[0].detail)
        policy = NrcPolicy()
        policy.set("did_read_only", (0x31, 0x7F))
        _suite, report = bench.run(description, self.READ_ONLY, policy=policy)
        (case,) = report.cases
        self.assertEqual(case.verdict, "passed")
        self.assertIn("accepted by the NRC policy; ISO 14229-1 asks for 0x31 requestOutOfRange", case.steps[0].detail)
        policy.set("did_read_only", (0x22,))                  # a specification with its own code
        _suite, report = bench.run(description, self.READ_ONLY, policy=policy)
        self.assertEqual(report.cases[0].verdict, "failed")
        self.assertIn("expected NRC 0x22 conditionsNotCorrect", report.cases[0].steps[0].detail)

    def test_accepted_deviations(self):
        config = EcuConfig(broadcast_interval=0, forced_nrcs=[{"sid": 0x2E, "nrc": 0x7F}])
        bench = Bench(self, config)
        description = dummy_description(EcuConfig())
        _suite, report = bench.run(description, self.READ_ONLY)
        steps = [item.description for item in report.cases[0].steps]
        self.assertGreater(len(steps), 1, "one step for each read-only DID")
        deviation = Deviation(self.READ_ONLY[0], steps[0], "the supplier's 0x7F, agreed in ticket 42")
        _suite, report = bench.run(description, self.READ_ONLY, deviations=[deviation])
        (case,) = report.cases
        self.assertEqual([item.verdict for item in case.steps], ["accepted"] + ["fail"] * (len(steps) - 1))
        self.assertIn("accepted deviation: the supplier's 0x7F", case.steps[0].detail)
        self.assertEqual(case.verdict, "failed", "the other DIDs still fail")
        every = Deviation(self.READ_ONLY[0], "*", "")
        _suite, report = bench.run(description, self.READ_ONLY, deviations=[every])
        self.assertEqual(report.cases[0].verdict, "passed")
        self.assertEqual({item.verdict for item in report.cases[0].steps}, {"accepted"})


class CoverageTest(unittest.TestCase):
    def test_counting(self):
        coverage = Coverage()
        coverage.record(b"\x22\xf1\x90\xf1\x86", 0x03, "pass", "data_identifiers.read")
        coverage.record(b"\x2e\xf1\x90\x00", 0x03, "fail", "data_identifiers.write")
        coverage.record(b"\x31\x01\xff\x00", 0x02, "accepted", "routines.erase")
        coverage.record(b"\x3e\x00", None, "pass", "tester_present", functional=True)
        coverage.record(b"\x10\x01", 0x01, "info")                      # a log line: not a check
        self.assertEqual(coverage.services[(0x22, 0x03)].verdict(), "passed")
        self.assertEqual(coverage.dids[(0xF186, 0x03, "read")].count, 1, "each DID of the request")
        self.assertEqual(coverage.dids[(0xF190, 0x03, "write")].verdict(), "failed")
        self.assertEqual(coverage.routines[(0xFF00, 0x02)].verdict(), "accepted")
        self.assertEqual(coverage.services[(0x3E, -1)].count, 1, "before the session was known")
        self.assertEqual(coverage.functional[0x3E].count, 1)
        self.assertEqual(coverage.services[(0x10, 0x01)].count, 0)
        again = Coverage.from_dict(json.loads(json.dumps(coverage.to_dict())))
        self.assertEqual(again.to_dict(), coverage.to_dict())
        described = dummy_description()
        missing = dict(untested(coverage, described, Options()))
        self.assertEqual(missing["Service 11 ECUReset"], "ECU reset is a destructive test: tick Destructive tests")
        self.assertEqual(missing["Routine FF00 EraseMemory: started"],
                         "routines are not started (only refused where they may not run)")
        self.assertIn("DID F187 SparePartNumber: read", missing)
        page = coverage_html(coverage, described, Options())
        self.assertIn("<h2>Coverage</h2>", page)
        self.assertIn("F190 VIN</td><td>write</td>", page)

    def test_a_run_counts_what_it_checked(self):
        bench = Bench(self)
        names = ["testerpresent.testerpresent_3e", "data_identifiers.read_vin_f190",
                 "service_availability.securityaccess_27_by_session"]
        suite, report = bench.run(dummy_description(bench.ecu.config), names)
        self.assertEqual(report.verdict, "passed", failures(report))
        coverage = suite.coverage
        self.assertEqual(coverage.services[(0x3E, 0x01)].verdict(), "passed")
        self.assertEqual(coverage.services[(0x27, 0x01)].verdict(), "passed", "refused in the default session: 0x7F")
        self.assertIn("service_availability.securityaccess_27_by_session", coverage.services[(0x27, 0x03)].tests)
        self.assertEqual({key[1] for key in coverage.dids if key[0] == 0xF190}, {0x01, 0x02, 0x03})
        self.assertEqual(suite.identification[0xF195], ("systemSupplierECUSoftwareVersionNumber", "APP-1.0.0"),
                         "read at the start, with ISO's name")
        self.assertEqual(suite.identification[0xF190][1], "WVWZZZ1KZAW000001")
        self.assertIn("ECU identification: F195 systemSupplierECUSoftwareVersionNumber = APP-1.0.0",
                      [step.description for step in report.setup.steps])


class DiscoveryTest(unittest.TestCase):
    OPTIONS = DiscoveryOptions([0x01, 0x03], "0100-0102, F180-F19F", "0200-0202, FF00-FF01")

    def test_ranges(self):
        self.assertEqual(parse_ranges("F100-F1FF, 0100"), [(0xF100, 0xF1FF), (0x0100, 0x0100)])
        self.assertEqual(expand(parse_ranges("0100-0102,0101")), [0x100, 0x101, 0x102])
        for text in ("F1FF-F100", "zz", "10000"):
            with self.assertRaises(ValueError):
                parse_ranges(text)

    def discover(self, bench, description, options=None, stop=None):
        tester = Tester(bench.tester_bus, TRANSPORT, 0x7DF)
        return Discovery(tester, description, options or self.OPTIONS, stop=stop).run()

    def test_what_the_ecu_has_and_the_description_does_not_say(self):
        bench = Bench(self)
        description = dummy_description(bench.ecu.config)
        del description.dids[0xF18C]                        # forgotten
        description.dids[0xF190].length = 16                # wrong
        del description.services[0x86]                      # forgotten
        description.dids[0xF1A0] = description.dids[0xF187].__class__(0xF1A0, "Ghost", 4, Access(), None)
        result = self.discover(bench, description)
        self.assertEqual(result.entered(), [0x01, 0x03])
        self.assertTrue(result.service_found(0x86))
        self.assertEqual(result.service_sessions(0x27), {0x03}, "SecurityAccess: extended only")
        self.assertEqual(result.did_length(0xF190), 17)
        self.assertEqual(result.found_levels(), [0x01])
        self.assertTrue(result.routine_found(0x0201), "the self test: its results are asked, it is not started")
        self.assertFalse(bench.ecu.state.routines, "nothing was started")
        findings = {(finding.kind, finding.what) for finding in compare(result, description)}
        self.assertEqual(findings, {("undocumented", "Service 86 ResponseOnEvent"), ("undocumented", "DID F18C"),
                                    ("different", "DID F190 VIN"), ("missing", "DID F1A0 Ghost")})
        again = DiscoveryResult.from_dict(json.loads(json.dumps(result.to_dict())))
        self.assertEqual(again.to_dict(), result.to_dict())
        page = discovery_page(result, description)
        self.assertIn("found, not described", page)
        self.assertIn("the ECU answers 17 bytes, the description says 16", page)

    def test_testing_the_ecu_as_it_was_found(self):
        bench = Bench(self)
        result = self.discover(bench, None, DiscoveryOptions([0x01, 0x03], "0100-0102, F186-F195", "0201"))
        found = result.to_description("Found")
        self.assertEqual(found.unknown, {"writing", "sub-functions", "starting routines"})
        self.assertEqual(found.services[0x34].access, Access({0x02}), "refused everywhere asked: elsewhere")
        self.assertEqual(found.services[0x35].access.levels, {0x01}, "0x33 to the SID alone: behind a level")
        self.assertEqual(found.dids[0xF190].length, 17)
        self.assertEqual(EcuDescription.from_dict(found.to_dict()).unknown, found.unknown)
        _suite, report = bench.run(found)
        self.assertEqual(failures(report), {})
        self.assertNotIn("Data identifiers: Writing a read-only DID", [case.title for case in report.cases],
                         "which DIDs may be written is not known")

    def test_stopping(self):
        bench = Bench(self)
        stop = threading.Event()
        stop.set()
        result = self.discover(bench, dummy_description(bench.ecu.config), stop=stop)
        self.assertEqual(result.probes, 0)
        self.assertIn("Stopped before the end", result.notes[0])


class CompareTest(unittest.TestCase):
    def test_what_differs_from_run_to_run_is_left_out(self):
        self.assertEqual(answer_of("22 F1 90 -> 62 F1 90 57 ... (4 ms)"), "62 F1 90 ...", "not its data")
        self.assertEqual(answer_of("27 01 -> 67 01 0F 1D 61 52 (0 ms)"), "67 01 ...", "not a seed")
        self.assertEqual(answer_of("3E 00 -> 7E 00 (125 ms, after 2 response pending (first at 0 ms))"), "7E 00")
        self.assertEqual(answer_of("2E 01 10 -> 7F 2E 31 requestOutOfRange (0 ms) - expected NRC 0x13"),
                         "7F 2E 31 requestOutOfRange")

    def run_once(self, config, description, names):
        bench = Bench(self, config)
        suite, report = bench.run(description, names)
        return results_dict(report, description, suite.identification, suite.coverage)

    def test_two_runs(self):
        names = ["communication.controldtcsetting_85", "data_identifiers.read_vin_f190",
                 "testerpresent.testerpresent_3e"]
        before = self.run_once(EcuConfig(broadcast_interval=0), dummy_description(), names)
        changed = EcuConfig(broadcast_interval=0, forced_nrcs=[{"sid": 0x85, "nrc": 0x22}], response_delay_ms=120)
        for item in changed.dids:
            if item["did"] == 0xF195:
                item["data"] = b"APP-2.0.0".hex()
        description = dummy_description()
        del description.dids[0xF190]                          # its test is gone
        after = self.run_once(changed, description, names)
        comparison = compare_runs(before, after)
        kinds = {change.title: change.kind for change in comparison.changes}
        self.assertEqual(kinds, {"Communication: ControlDTCSetting (85)": "regression",
                                 "TesterPresent: TesterPresent (3E)": "steps",
                                 "Data identifiers: Read VIN (F190)": "gone"}, kinds)
        self.assertEqual(comparison.identification,
                         [("F190", "VIN", "WVWZZZ1KZAW000001", ""),        # no longer described: not read
                          ("F195", "systemSupplierECUSoftwareVersionNumber", "APP-1.0.0", "APP-2.0.0")])
        steps = {step.description: (step.before, step.after)
                 for step in next(change for change in comparison.changes if change.kind == "steps").steps}
        self.assertEqual(steps["3E 80: the suppress bit set, no answer"], ("pass: no answer", "pass: 7E 00"),
                         "answered after a response pending, now")
        self.assertEqual(steps["3E 00: response pending within P2, then P2*"][0], "", "a new step")
        self.assertEqual(compare_runs(after, before).of("fixed")[0].title, "Communication: ControlDTCSetting (85)")
        self.assertIn("1 regression", comparison.summary())
        page = comparison_page(comparison)
        self.assertIn("Regressions (1)", page)
        self.assertIn("APP-2.0.0", page)
        folder = Path(tempfile.mkdtemp())
        for name, run in (("before.json", before), ("after.json", after)):
            (folder / name).write_text(json.dumps(run), encoding="utf-8")
        self.assertEqual(previous_results(folder, load_results(folder / "after.json")), folder / "before.json")
        with patch("sys.stdout") as out:
            code = cli.main(["--compare", str(folder / "before.json"), str(folder / "after.json"), "--output",
                             str(folder / "comparison.html")])
        printed = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertEqual(code, cli.EXIT_FAILED, printed)
        self.assertIn("REGRESSION Communication: ControlDTCSetting (85): passed -> failed", printed)
        self.assertIn("F195 systemSupplierECUSoftwareVersionNumber: APP-1.0.0 -> APP-2.0.0", printed)
        self.assertTrue((folder / "comparison.html").exists())
        with patch("sys.stdout"):
            self.assertEqual(cli.main(["--compare", str(folder / "after.json"), str(folder / "before.json")]),
                             cli.EXIT_PASSED, "a fix is no regression")
            (folder / "other.json").write_text("{}", encoding="utf-8")
            self.assertEqual(cli.main(["--compare", str(folder / "other.json"), str(folder / "before.json")]),
                             cli.EXIT_NOT_RUN)


class PlanTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def test_a_path_on_another_drive(self):
        plan = TestPlan(path=self.folder / "plan.json")
        elsewhere = Path("D:/descriptions/ecu.cdd")
        with patch("os.path.relpath", side_effect=ValueError("path is on mount 'D:', start on mount 'C:'")):
            self.assertEqual(plan.relative(elsewhere), str(elsewhere), "no relative path across drives: absolute")
        self.assertEqual(plan.relative(self.folder / "cdd" / "ecu.cdd"), "cdd/ecu.cdd")

    def test_a_plan_as_json(self):
        (self.folder / "cdd").mkdir()
        description = self.folder / "cdd" / "ecu.cdd"
        description.write_bytes(DUMMY_CDD.read_bytes())
        plan = TestPlan("Nightly", str(description), Connection("vector", "1", 250000, 0x18DA10F1, 0x18DAF110, None,
                                                               True, None),
                        excluded=["timing.responses_within_p2"], key=KeySource("dll", 0x5A, "keys/ecu.dll", "B"),
                        sequences=[Sequence("Hard reset", [SequenceStep("reset", "01")], [Attachment("after", "run")])],
                        nrc_policy=NrcPolicy({"locked": (0x33, 0x22)}),
                        deviations=[Deviation("timing.responses_within_p2", "*", "slow gateway", "2026-09-24")])
        plan.options["destructive"] = True
        plan.discovery = DiscoveryOptions([0x01, 0x02], "F100-F1FF", "0200", False, True)
        path = self.folder / "plans" / "nightly.json"
        path.parent.mkdir()
        plan.path = path
        plan.description = plan.relative(description)
        plan.save(path)
        written = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(written["description"], "../cdd/ecu.cdd", "relative to the plan's folder")
        self.assertEqual(written["connection"]["request_id"], "0x18DA10F1")
        self.assertIsNone(written["connection"]["functional_id"])
        again = TestPlan.load(path)
        self.assertEqual(again.to_dict(), plan.to_dict())
        self.assertEqual(again.resolve(again.description), description.resolve())
        self.assertEqual(again.load_description().name, "DummyECU (CommonDiagnostics)")
        self.assertTrue(again.make_options().destructive)
        self.assertEqual(again.connection.transport()["padding"], None)
        self.assertTrue(is_plan_file(path))
        moved = self.folder / "elsewhere" / "nightly.json"
        moved.parent.mkdir()
        again.save(moved)
        self.assertEqual(json.loads(moved.read_text(encoding="utf-8"))["description"], "../cdd/ecu.cdd")
        self.assertEqual(TestPlan.load(moved).resolve("../cdd/ecu.cdd"), description.resolve())

    def test_what_is_not_a_plan(self):
        described = self.folder / "ecu.json"
        dummy_description().save(described)
        self.assertFalse(is_plan_file(described), "a description saved as JSON")
        with self.assertRaises(PlanError):
            TestPlan.load(described)
        newer = self.folder / "newer.json"
        newer.write_text(json.dumps({"format": "TestExpert plan", "version": 99}), encoding="utf-8")
        with self.assertRaises(PlanError):
            TestPlan.load(newer)
        plan = TestPlan(description="missing.cdd", path=self.folder / "plan.json")
        with self.assertRaises(PlanError):
            plan.load_description()
        self.assertEqual(TestPlan().load_description().name, "Dummy ECU", "no description: the Dummy ECU's")
        from_numbers = TestPlan.from_dict({"format": "TestExpert plan", "connection": {"request_id": 2016, "padding": "AA"}})
        self.assertEqual((from_numbers.connection.request_id, from_numbers.connection.padding), (0x7E0, 0xAA))

    def test_tests_named_and_runs_repeated_on_the_command_line(self):
        cases = Suite(dummy_description()).cases
        self.assertEqual(cli.selected(["sessions.default_session_10_01"], cases), ["sessions.default_session_10_01"])
        group = cli.selected(["sessions"], cases)
        self.assertTrue(len(group) > 1 and all(name.startswith("sessions.") for name in group))
        with self.assertRaises(PlanError):
            cli.selected(["nothing"], cases)
        path, reports = self.folder / "repeat.json", self.folder / "repeated"
        plan = TestPlan("Repeat")
        plan.options["s3_test"] = False
        plan.save(path)
        with patch("sys.stdout") as out:
            code = cli.main([str(path), "--run", "--dummy-ecu", "--test", "sessions.default_session_10_01",
                             "--repeat", "2", "--report-dir", str(reports)])
        printed = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertEqual(code, cli.EXIT_PASSED, printed)
        self.assertIn("Run 2 of 2: Running 1 tests", printed)
        self.assertIn("2 runs of 2: 2 passed, 0 did not", printed)
        self.assertEqual(sorted(item.stem[-5:] for item in reports.glob("*.html")), ["_run1", "_run2"])
        with patch("sys.stdout"):
            self.assertEqual(cli.main([str(path), "--run", "--dummy-ecu", "--test", "nothing"]), cli.EXIT_NOT_RUN)
        plan.sequences = [Sequence("Unknown DID", [SequenceStep("request", "22 12 34", "positive")],
                                   [Attachment("before", "each")])]
        plan.save(path)
        junit = self.folder / "junit.xml"
        with patch("sys.stdout") as out:
            code = cli.main([str(path), "--run", "--dummy-ecu", "--test", "sessions.default_session_10_01",
                             "--repeat", "3", "--until-failure", "--report-dir", str(reports), "--junit", str(junit)])
        printed = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertEqual(code, cli.EXIT_FAILED, printed)
        self.assertIn("1 run of 3: 0 passed, 1 did not", printed)
        self.assertTrue(junit.exists())

    def test_the_command_line(self):
        arguments = argparse.Namespace(file=None, interface="virtual", channel="7", bitrate=None)
        plan = cli.load_plan(arguments)
        self.assertEqual((plan.connection.interface, plan.connection.channel, plan.connection.bitrate),
                         ("virtual", "7", 500000))
        suite = Suite(dummy_description(), TestPlan().make_options())               # with the plan's key source
        keep = {"sessions.default_session_10_01", "testerpresent.testerpresent_3e"}
        plan = TestPlan("CLI", excluded=[case.name for case in suite.cases if case.name not in keep],
                        sequences=[Sequence("Hard reset", [SequenceStep("reset", "01")], [Attachment("after", "run")])])
        plan.options["reset_time"] = 0.6
        path = self.folder / "cli.json"
        plan.save(path)
        reports, junit = self.folder / "reports", self.folder / "ci" / "junit.xml"
        with patch("sys.stdout") as out:
            code = cli.main([str(path), "--run", "--dummy-ecu", "--report-dir", str(reports), "--junit", str(junit)])
        printed = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertEqual(code, cli.EXIT_PASSED, printed)
        self.assertIn("Running 2 tests against a Dummy ECU", printed)
        self.assertIn("PASSED   Sessions: Default session (10 01)", printed)
        self.assertIn("PASSED: 2 passed, 0 failed", printed)
        self.assertTrue(junit.exists())
        self.assertEqual(load_results(next(reports.glob("*.json")))["counts"]["passed"], 2, "the results, as JSON")
        root = ElementTree.parse(junit).getroot()
        self.assertEqual(root.find("testsuite").get("tests"), "2")
        page = next(reports.glob("*.html")).read_text(encoding="utf-8")
        self.assertIn("<th>Test plan</th>", page)
        self.assertIn("<h2>Coverage</h2>", page)
        self.assertIn("<th>F195 systemSupplierECUSoftwareVersionNumber</th><td>APP-1.0.0</td>", page)
        self.assertIn("Post-run &#x27;Hard reset&#x27;", page)

        plan.sequences = [Sequence("Unknown DID", [SequenceStep("request", "22 12 34", "positive")],
                                   [Attachment("before", "each")])]
        plan.record = True
        plan.save(path)
        with patch("sys.stdout"):
            self.assertEqual(cli.main([str(path), "--run", "--dummy-ecu", "--report-dir", str(reports), "--quiet"]),
                             cli.EXIT_FAILED, "blocked tests")
        self.assertTrue(list(reports.glob("*.blf")), "the traffic recorded beside the reports")
        plan.description = "nowhere.cdd"
        plan.save(path)
        described = self.folder / "described.json"
        description = dummy_description()
        description.dids[0xF190].length = 16
        description.save(described)
        with patch("sys.stdout") as out:
            code = cli.main([str(described), "--discover", "--dummy-ecu", "--dids", "F190", "--rids", "0201",
                             "--report-dir", str(reports), "--save-description", str(self.folder / "found.json")])
        printed = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertEqual(code, cli.EXIT_FAILED, printed)
        self.assertIn("DIFFERENT    DID F190 VIN: the ECU answers 17 bytes, the description says 16", printed)
        self.assertTrue(list(reports.glob("discovery_*.html")) and list(reports.glob("discovery_*.json")))
        self.assertEqual(EcuDescription.load(self.folder / "found.json").dids[0xF190].length, 17)
        with patch("sys.stdout"):
            self.assertEqual(cli.main([str(DUMMY_CDD), "--discover", "--dummy-ecu", "--dids", "F190", "--rids", "0201",
                                       "--report-dir", str(reports)]), cli.EXIT_PASSED, "no difference")
            self.assertEqual(cli.main([str(DUMMY_CDD), "--discover", "--dummy-ecu", "--dids", "zz"]), cli.EXIT_NOT_RUN)
        with patch("sys.stdout"):
            self.assertEqual(cli.main([str(path), "--run", "--dummy-ecu"]), cli.EXIT_NOT_RUN)
            self.assertEqual(cli.main([str(DUMMY_CDD), "--run", "--interface", "no-such-interface"]), cli.EXIT_NOT_RUN)


class DeeperTest(unittest.TestCase):
    """Values against the description's fields, writes at and beyond their limits, routines started and in the
    wrong order, S3, and responses pending."""

    def test_the_dummy_ecu_keeps_them(self):
        bench = Bench(self, EcuConfig(s3_timeout=1.0, self_test_seconds=0.5))
        suite, report = bench.run(dummy_description(bench.ecu.config), destructive=True, start_routines="0201",
                                  s3_test=True, s3_seconds=1.0)
        self.assertEqual(failures(report), {})
        steps = {case.title: [step.description for step in case.steps] for case in report.cases}
        self.assertIn("Speed out of range (1201 rpm): refused, NRC 0x31",
                      steps["Data identifiers: Write IdleSpeedTarget (0110): its limits, and out of them"])
        self.assertEqual(steps["Routines: Start SelfTest (0201)"],
                         ["started (31 01)", "its results (31 03)", "stopped (31 02)", "its results after the stop"])
        self.assertIn("Routines: SelfTest (0201): stop and results before a start", steps)
        self.assertIn("after S3 without a request, the default session",
                      steps["Timing: S3: the Extended session ends without requests"])
        self.assertIn("Session: a valid value", steps["Data identifiers: Values of ActiveDiagnosticSession (F186)"])

    def test_what_is_wrong_is_found(self):
        config = EcuConfig(broadcast_interval=0, s3_timeout=3.0, self_test_seconds=0.5)
        for item in config.dids:
            if item["did"] == 0x0110:
                item["data"], item["valid"] = "0100", []           # 256 rpm, and any value taken
        bench = Bench(self, config)
        bench.ecu.state.routines[0x0201] = {"status": 0x00, "until": None}     # results kept from before
        description = dummy_description(EcuConfig())
        names = [case.name for case in Suite(description, Options(destructive=True, s3_test=True)).cases
                 if "0110" in case.title or "S3: the" in case.title]
        _suite, report = bench.run(description, names, destructive=True, s3_test=True, s3_seconds=1.0)
        found = {case.title: [(step.description, step.detail) for step in case.failures()] for case in report.cases}
        self.assertIn(("Speed: a valid value", "256 rpm: not a valid value (600 rpm to 1200 rpm)"),
                      found["Data identifiers: Values of IdleSpeedTarget (0110)"])
        self.assertEqual([step for step, _detail in found["Data identifiers: Write IdleSpeedTarget (0110): its "
                                                          "limits, and out of them"]],
                         ["Speed out of range (1201 rpm): refused, NRC 0x31", "nothing written"])
        self.assertTrue(found["Timing: S3: the Extended session ends without requests"], "S3 3 s, not 1 s")

    def test_responses_pending(self):
        names = ["testerpresent.testerpresent_3e"]
        bench = Bench(self, EcuConfig(broadcast_interval=0, response_delay_ms=120))
        _suite, report = bench.run(dummy_description(bench.ecu.config), names)
        (case,) = report.cases
        self.assertEqual(case.verdict, "passed", failures(report))
        pending = [step for step in case.steps if "response pending within P2" in step.description]
        self.assertTrue(pending and all(step.verdict == "pass" for step in pending))
        suppressed = next(step for step in case.steps if step.description.startswith("3E 80"))
        self.assertIn("after a response pending the answer is sent", suppressed.detail)
        slow = Bench(self, EcuConfig(broadcast_interval=0, response_delay_ms=700, pending_interval=0.5, p2_star_ms=300))
        _suite, report = slow.run(dummy_description(slow.ecu.config), names)
        failed = [step.detail for step in report.cases[0].failures()]
        self.assertTrue(failed and all("(P2* 300 ms)" in detail for detail in failed), failed)


class WindowTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = patch.object(window_module, "TEST_EXPERT_DIR", Path(folder.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.folder = Path(folder.name)
        self.settings = MemorySettings()
        self.window = window_module.TestExpertWindow(self.settings)
        self.addCleanup(self.window.close)
        self.window.s3_test.setChecked(False)                                # a few seconds of waiting

    def test_a_description_and_its_tests(self):
        self.assertEqual(self.window.description.name, "Dummy ECU", "the Dummy ECU without a file")
        count = len(self.window.suite.cases)
        self.window.destructive.setChecked(True)
        self.assertGreater(len(self.window.suite.cases), count)
        self.assertIsNotNone(self.window.open_description(DUMMY_CDD))
        kept = json.loads(self.settings.value("test_expert/plan_state"))
        self.assertEqual(kept["description"], str(DUMMY_CDD.resolve()), "kept for the next start")
        self.assertIn("DummyECU", self.window.description_label.text())
        bad = Path(tempfile.mkdtemp()) / "bad.cdd"
        bad.write_text("not xml", encoding="utf-8")
        self.assertIsNone(self.window.open_description(bad))
        self.assertIn("could not be read", self.window.log.toPlainText())

    def test_a_run_against_the_dummy_ecu(self):
        bench = Bench(self)
        self.assertIsNone(self.window.run(), "not connected")
        self.assertTrue(self.window.connect_ecu(can.Bus(interface="virtual", channel=bench.channel)))
        self.addCleanup(self.window.disconnect_ecu)
        self.window.request_id.setValue(0x7E0)
        self.window.response_id.setValue(0x7E8)
        self.window.record.setChecked(True)
        first = next(iter(self.window._items))
        self.window._items[first].setCheckState(0, 0)                         # unticked: not run
        self.assertIsNotNone(self.window.run())
        self.assertTrue(spin_until(lambda: self.window.report is not None and self.window.run_action.isEnabled()),
                        self.window.log.toPlainText())
        report = self.window.report
        self.assertEqual(report.verdict, "passed", failures(report))
        self.assertNotIn(first, [case.name for case in report.cases])
        html, xml, results = self.window.report_paths
        self.assertEqual(results.suffix, ".json", "the results, for comparing runs")
        self.assertEqual(html.parent, self.folder / "reports")
        self.assertTrue(xml.exists())
        self.assertTrue(list((self.folder / "reports").glob("traffic_*.blf")), "the traffic was recorded")
        self.assertIn("PASSED", self.window.status.text())
        self.assertIn("Services", self.window.coverage_view.toPlainText())
        self.assertIn("ECU: F195 systemSupplierECUSoftwareVersionNumber = APP-1.0.0", self.window.log.toPlainText())

    def test_variants_in_the_window(self):
        window = self.window
        self.assertTrue(window.variant_box.isHidden(), "the Dummy ECU's own description: one variant")
        window.open_description(DUMMY_ODX)
        self.assertFalse(window.variant_box.isHidden())
        self.assertEqual([window.variant_combo.itemText(i) for i in range(window.variant_combo.count())],
                         ["Application", "Bootloader", "DummyECU"])
        self.assertTrue(window.told_btn.isHidden(), "an ODX file says how its variants are told apart")
        application = len(window.suite.cases)
        window.choose_variant("Bootloader")
        self.assertEqual((window.description.variant, window.variant_combo.currentText()), ("Bootloader",) * 2)
        self.assertLess(len(window.suite.cases), application)
        self.assertIsNone(window.identify_variant(), "not connected")
        self.assertIn("Connect to the ECU first", window.log.toPlainText())
        window.identify_box.setChecked(True)
        plan = window.plan()
        self.assertEqual((plan.variant, plan.identify), ("Bootloader", True))
        other = window_module.TestExpertWindow(self.settings)
        self.addCleanup(other.close)
        self.assertEqual(other.description.variant, "Bootloader", "kept for the next start")
        self.assertTrue(other.identify_box.isChecked())

        bench = Bench(self)
        window.request_id.setValue(0x7E0)
        window.response_id.setValue(0x7E8)
        self.assertTrue(window.connect_ecu(can.Bus(interface="virtual", channel=bench.channel)))
        self.addCleanup(window.disconnect_ecu)
        self.assertEqual(window.description.variant, "Application", "asked when connecting")
        self.assertIn("The ECU is the variant Application", window.log.toPlainText())
        bench.ecu.state.bootloader = True
        self.assertEqual(window.identify_variant(), "Bootloader")
        self.assertEqual(len(window.suite.cases), len(Suite(odx_loader.load_odx(DUMMY_ODX, "Bootloader"),
                                                            window.options()).cases))

        window.open_description(VariantTest.two_variants(self))
        self.assertFalse(window.told_btn.isHidden(), "a CDD: the plan says how")
        with patch.object(window_module.IdentificationDialog, "exec_", return_value=False):
            self.assertIsNone(window.identify_variant(), "no DID given")
        window.identification = Identification(0xF195, {"COMMON": "BOOTLOADER"})
        self.assertEqual(window.identify_variant(), "COMMON")
        self.assertEqual(window.plan().identification, window.identification)

    def test_modules_in_the_window(self):
        window = self.window
        count = len(window.suite.cases)
        window.modules_tab.add_modules([EXAMPLE_MODULE, EXAMPLE_MODULE])
        self.assertEqual(window.modules_tab.modules(), [str(EXAMPLE_MODULE.resolve())], "once")
        self.assertEqual(len(window.suite.cases), count + 5)
        group = window._group_items["Module: Dummy ECU checks"]
        self.assertEqual(group.text(0), "Module: Dummy ECU checks (5)")
        self.assertEqual(group.child(1).text(0), "An unknown identifier is refused with requestOutOfRange")
        self.assertEqual(group.toolTip(0), str(EXAMPLE_MODULE.resolve()))
        self.assertIn("Dummy ECU checks (5 test cases)", window.modules_tab.module_list.item(0).text())
        window.modules_tab.add_symbols([DBC_DIR / "dummy_ecu.dbc"])
        window._items["module_dummy_ecu_checks.fault_memory"].setCheckState(0, Qt.Unchecked)
        path = self.folder / "plans" / "modules.json"
        path.parent.mkdir()
        window.save_plan_as(path)
        saved = TestPlan.load(path)
        self.assertEqual(saved.module_paths(), [EXAMPLE_MODULE.resolve()])
        self.assertIn("module_dummy_ecu_checks.fault_memory", saved.excluded)
        other = window_module.TestExpertWindow(self.settings)
        self.addCleanup(other.close)
        self.assertEqual(other.modules_tab.symbols(), [str((DBC_DIR / "dummy_ecu.dbc").resolve())], "kept")
        self.assertEqual(other._items["module_dummy_ecu_checks.fault_memory"].checkState(0), Qt.Unchecked)

        bench = Bench(self, EcuConfig(lockout_seconds=1))
        window.request_id.setValue(0x7E0)
        window.response_id.setValue(0x7E8)
        self.assertTrue(window.connect_ecu(can.Bus(interface="virtual", channel=bench.channel)))
        self.addCleanup(window.disconnect_ecu)
        names = [name for name in window._items if name.startswith("module_") and not name.endswith("fault_memory")]
        self.assertIsNotNone(window.run(names))
        self.assertTrue(spin_until(lambda: window.report is not None and window.run_action.isEnabled()),
                        window.log.toPlainText())
        self.assertEqual(window.report.verdict, "passed", failures(window.report))
        self.assertEqual(len(window.report.cases), 4)
        self.assertEqual(window._items["module_dummy_ecu_checks.engine_data"].text(window_module.COL_VERDICT),
                         "passed")

        window.modules_tab.module_list.item(0).setSelected(True)
        window.modules_tab._remove(window.modules_tab.module_list)
        self.assertEqual(len(window.suite.cases), count)

    def test_io_control_and_dtcs_in_the_description_tab(self):
        tree = self.window.description_tree
        branches = {tree.topLevelItem(index).text(0): tree.topLevelItem(index) for index in range(tree.topLevelItemCount())}
        self.assertIn("DTCs (2)", branches)
        self.assertEqual(branches["DTCs (2)"].child(0).text(0), "010100 P0101-00")
        dids = next(item for text, item in branches.items() if text.startswith("DIDs"))
        temperature = next(dids.child(index) for index in range(dids.childCount())
                           if dids.child(index).text(0).startswith("0101 "))
        self.assertIn("IO control: 03", temperature.text(1))

    def test_the_download_and_memory_settings(self):
        window = self.window
        window.destructive.setChecked(True)
        window.memory.setText("F000")
        self.assertIn("background", window.memory.styleSheet(), "shown as wrong")
        self.assertEqual(window.plan().options["memory"], "", "not taken while it cannot be read")
        window.memory.setText("F000:10")
        window.download.setText("10000:300")
        window.rebuild_tests()
        self.assertEqual(window.memory.styleSheet(), "")
        plan = window.plan()
        self.assertEqual((plan.options["memory"], plan.options["download"]), ("F000:10", "10000:300"))
        self.assertIn("download_and_upload.a_download_started_then_refused_out_of_order", window._items)
        window.close()                                                       # kept when it closes
        other = window_module.TestExpertWindow(self.settings)
        self.addCleanup(other.close)
        self.assertEqual(other.memory.text(), "F000:10", "kept")

    def test_run_control(self):
        window = self.window
        window.transport.setChecked(False)
        bench = Bench(self, EcuConfig(lockout_seconds=1, forced_nrcs=[{"sid": 0x3E, "nrc": 0x22}]))
        window.request_id.setValue(0x7E0)
        window.response_id.setValue(0x7E8)
        self.assertTrue(window.connect_ecu(can.Bus(interface="virtual", channel=bench.channel)))
        self.addCleanup(window.disconnect_ecu)

        def finished():
            return spin_until(lambda: window.run_action.isEnabled() and not (window.thread and window.thread.is_alive()),
                              60)

        window.filter_edit.setText("testerpresent")
        self.assertFalse(window._group_items["TesterPresent"].isHidden())
        self.assertTrue(window._group_items["Sessions"].isHidden(), "filtered out")
        window.filter_edit.setText("")
        self.assertFalse(window._group_items["Sessions"].isHidden())

        test = window._items["sessions.default_session_10_01"]
        run_test = next(action for action in window.sequence_menu(test).actions() if action.text() == "Run this test")
        run_test.trigger()
        self.assertTrue(finished(), window.log.toPlainText())
        self.assertEqual([case.name for case in window.report.cases], ["sessions.default_session_10_01"])
        self.assertEqual((window.progress.value(), window.progress.maximum()), (1, 1))
        self.assertFalse(window.rerun_action.isEnabled(), "nothing failed")

        group = window._group_items["TesterPresent"]
        run_group = next(action for action in window.sequence_menu(group).actions()
                         if action.text().startswith("Run this group"))
        run_group.trigger()
        self.assertTrue(finished(), window.log.toPlainText())
        failed = [case.name for case in window.report.cases if case.verdict == "failed"]
        self.assertTrue(failed, "TesterPresent refused with 0x22")
        self.assertTrue(window.rerun_action.isEnabled())
        window.not_passed_box.setChecked(True)
        shown = [name for name, item in window._items.items() if not item.isHidden()]
        self.assertEqual(shown, failed, "only what did not pass")
        window.not_passed_box.setChecked(False)

        window.rerun_action.trigger()
        self.assertTrue(finished(), window.log.toPlainText())
        self.assertEqual([case.name for case in window.report.cases], failed, "only the failed ones again")

        reports = self.folder / "reports"
        before = set(reports.glob("*.html"))
        self.assertIsNotNone(window.run(["sessions.default_session_10_01"], repeat=3))
        self.assertTrue(finished(), window.log.toPlainText())
        self.assertEqual(window._run_index, 3)
        self.assertIn("Run 3 of 3", window.log.toPlainText())
        self.assertIn("3 runs of 3: 3 passed, 0 did not", window.log.toPlainText())
        self.assertEqual(sorted(path.stem[-5:] for path in set(reports.glob("*.html")) - before),
                         ["_run1", "_run2", "_run3"], "each run its reports")

        self.assertIsNotNone(window.run(failed[:1], repeat=5, until_failure=True))
        self.assertTrue(finished(), window.log.toPlainText())
        self.assertIn("1 run of 5: 0 passed, 1 did not - stopped at the first that failed", window.log.toPlainText())

        window.repeat.setValue(4)
        window.until_failure.setChecked(True)
        plan = window.plan()
        self.assertEqual((plan.repeat, plan.until_failure), (4, True))
        again = TestPlan.from_dict(plan.to_dict())
        self.assertEqual((again.repeat, again.until_failure), (4, True))

    def test_the_identification_dialog(self):
        from canexpert.test_expert.variant_dialog import IdentificationDialog
        dialog = IdentificationDialog(["A", "B"], Identification(0xF1A0, {"A": "01"}))
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.did.text(), "F1A0")
        dialog.table.item(1, 1).setText("02 03")
        self.assertEqual(dialog.identification(), Identification(0xF1A0, {"A": "01", "B": "02 03"}))
        dialog.did.setText("zz")
        dialog._accept()
        self.assertIn("four hex digits", dialog.problem.text())
        dialog.did.setText("12345")
        dialog._accept()
        self.assertIn("four hex digits", dialog.problem.text())

    def test_sequences_in_the_window(self):
        window = self.window
        name = "sessions.enter_the_extended_session_10_03"
        item = window._items[name]
        self.assertIsNone(window.sequence_menu(None))
        menu = window.sequence_menu(item)
        after = next(action for action in menu.actions() if action.text() == "After this test")
        hard_reset = next(action for action in after.menu().actions() if action.text() == "New: Hard reset")
        hard_reset.trigger()
        self.assertEqual(item.text(window_module.COL_SEQUENCES), "after: Hard reset")
        sequence = window.sequence_editor.sequences()[0]
        self.assertEqual((sequence.name, sequence.attachments), ("Hard reset", [Attachment("after", "test", name)]))
        window.sequence_editor.add_step("wait", "0.5")
        self.assertEqual(window.sequence_editor.sequences()[0].steps[-1], SequenceStep("wait", "0.5"))
        window.rebuild_tests()
        self.assertEqual(window.suite.sequences, window.sequence_editor.sequences())
        again = window_module.TestExpertWindow(self.settings)
        self.addCleanup(again.close)
        self.assertEqual(again.sequence_editor.sequences(), window.sequence_editor.sequences(), "kept in the settings")
        self.assertEqual(window._items[name].text(window_module.COL_SEQUENCES), "after: Hard reset")
        window.sequence_editor.detach("test", name)
        self.assertEqual(window._items[name].text(window_module.COL_SEQUENCES), "")

    def test_plans_in_the_window(self):
        window = self.window
        description = self.folder / "descriptions" / DUMMY_CDD.name      # beside the plan: on its drive
        description.parent.mkdir()
        description.write_bytes(DUMMY_CDD.read_bytes())
        window.open_description(description)
        window.interface.setCurrentText("virtual")
        window.channel.setEditText("bench")
        window.destructive.setChecked(True)
        window.plan_name.setText("Bench")
        name = "timing.responses_within_p2"
        window._items[name].setCheckState(0, 0)
        window.sequence_editor.add_preset("Hard reset")
        path = self.folder / "plans" / "bench.json"
        path.parent.mkdir()
        self.assertEqual(window.save_plan_as(path), path)
        self.assertEqual(window.windowTitle(), "TestExpert - Bench")
        saved = TestPlan.load(path)
        self.assertEqual(saved.excluded, [name])
        self.assertEqual(saved.description, "../descriptions/dummy_ecu.cdd", "relative to the plan's folder")
        self.assertEqual(saved.resolve(saved.description), description.resolve())
        self.assertTrue(saved.options["destructive"])
        other = window_module.TestExpertWindow(MemorySettings())
        self.addCleanup(other.close)
        self.assertEqual(other.description.name, "Dummy ECU")
        self.assertIsNotNone(other.open_plan(path))
        self.assertEqual(other.description.name, "DummyECU (CommonDiagnostics)")
        self.assertEqual((other.interface.currentText(), other.channel.currentText()), ("virtual", "bench"))
        self.assertTrue(other.destructive.isChecked())
        self.assertEqual(other._items[name].checkState(0), 0, "left out, as the plan says")
        self.assertEqual([sequence.name for sequence in other.sequence_editor.sequences()], ["Hard reset"])
        self.assertEqual(other.plan(path).to_dict(), saved.to_dict())
        other.start_routines.setText("0201: zz")
        self.assertEqual(other.plan().options["start_routines"], "", "not readable: none")
        other.start_routines.setText("0201")
        other.s3_seconds.setValue(2.5)
        self.assertEqual((other.plan().options["start_routines"], other.plan().options["s3_seconds"]), ("0201", 2.5))
        other.rebuild_tests()
        self.assertIn("Routines: Start SelfTest (0201)", [case.title for case in other.suite.cases])
        other.new_plan()
        self.assertEqual((other.description.name, other.sequence_editor.sequences(), other.plan_path),
                         ("Dummy ECU", [], None))
        bad = self.folder / "bad.json"
        bad.write_text("{}", encoding="utf-8")
        self.assertIsNone(other.open_plan(bad))
        self.assertIn("not a TestExpert plan", other.log.toPlainText())
        restarted = window_module.TestExpertWindow(self.settings)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.plan_path, path, "the plan in use when the window was left")
        self.assertEqual(restarted._items[name].checkState(0), 0)

    def test_policy_and_deviations_in_the_window(self):
        window = self.window
        editor = window.policy_editor
        row = next(row for row in range(editor.nrcs.rowCount())
                   if editor.nrcs.item(row, 0).data(Qt.UserRole) == "did_not_in_session")
        editor.nrcs.item(row, 2).setText("31, 7F")
        self.assertEqual(window.plan().nrc_policy.accepted("did_not_in_session"), (0x31, 0x7F))
        editor.nrcs.item(row, 2).setText("zz")
        self.assertEqual(window.plan().nrc_policy.accepted("did_not_in_session"), (0x31, 0x7F), "a typo is not taken")
        bench = Bench(self, EcuConfig(broadcast_interval=0, forced_nrcs=[{"sid": 0x2E, "nrc": 0x7F}]))
        self.assertTrue(window.connect_ecu(can.Bus(interface="virtual", channel=bench.channel)))
        self.addCleanup(window.disconnect_ecu)
        name = PolicyTest.READ_ONLY[0]
        self.assertIsNotNone(window.run([name]))
        self.assertTrue(spin_until(lambda: window.report is not None and window.run_action.isEnabled()))
        self.assertEqual(window.report.cases[0].verdict, "failed")
        item = window._items[name]
        step = item.child(0)
        self.assertEqual(step.text(1), "fail")
        menu = window.sequence_menu(step)
        self.assertEqual([action.text() for action in menu.actions()], ["Accept this deviation..."])
        test, description = step.data(0, Qt.UserRole)[1:]
        window.accept_deviation(test, description, step, comment="ticket 42")
        self.assertEqual(step.text(1), "accepted")
        self.assertEqual(editor.deviations()[0].comment, "ticket 42")
        self.assertEqual(editor.table.item(0, 0).text(), "Data identifiers: Writing a read-only DID")
        self.assertEqual([action.text() for action in window.sequence_menu(step).actions()],
                         ["No longer accept this deviation"])
        window.report = None
        window.run([name])
        self.assertTrue(spin_until(lambda: window.report is not None and window.run_action.isEnabled()))
        self.assertEqual(window.report.cases[0].verdict, "failed", "only the first DID's failure is accepted")
        self.assertEqual(window._items[name].child(0).text(1), "accepted")
        self.assertEqual(window._items[name].child(1).text(1), "fail")
        before = window.tree.topLevelItem(0)
        self.assertEqual(before.text(0), "Before the tests", "the run's own steps")
        self.assertIn("P2 50 ms, as the ECU announces", [before.child(i).text(0) for i in range(before.childCount())])
        self.assertEqual([action.text() for action in window.sequence_menu(before).actions()], ["Before the run"])
        item = window._items[name]
        test_menu = [action.text() for action in window.sequence_menu(item).actions()]
        self.assertIn("Accept every failure of this test...", test_menu)
        path = self.folder / "policy.json"
        window.save_plan_as(path)
        saved = TestPlan.load(path)
        self.assertEqual(saved.nrc_policy.accepted("did_not_in_session"), (0x31, 0x7F))
        self.assertEqual([deviation.comment for deviation in saved.deviations], ["ticket 42"])
        editor.remove(test, description)
        self.assertEqual(window.plan().deviations, [])

    def test_discovery_in_the_window(self):
        window = self.window
        bench = Bench(self)
        self.assertIsNone(window.discover(DiscoveryTest.OPTIONS), "not connected")
        self.assertTrue(window.connect_ecu(can.Bus(interface="virtual", channel=bench.channel)))
        self.addCleanup(window.disconnect_ecu)
        window.description.dids[0xF190].length = 16
        self.assertIsNotNone(window.discover(DiscoveryTest.OPTIONS))
        self.assertTrue(spin_until(lambda: window.discovery_result is not None and window.run_action.isEnabled()))
        self.assertIn("1 difference with the description", window.status.text())
        self.assertIn("the ECU answers 17 bytes", window.discovery_view.browser.toPlainText())
        self.assertEqual(window.plan().discovery, DiscoveryTest.OPTIONS, "kept in the plan")
        page = window.save_discovery(self.folder / "discovery.html")
        self.assertTrue(page.exists() and page.with_suffix(".json").exists())
        found = window.use_discovered(self.folder / "found.json")
        self.assertEqual(found.name, "Dummy ECU (discovered)")
        self.assertEqual(window.description.dids[0xF190].length, 17)
        self.assertEqual(window.plan().description, str((self.folder / "found.json").resolve()))

    def test_each_run_compared_with_the_last(self):
        window = self.window
        bench = Bench(self)
        self.assertTrue(window.connect_ecu(can.Bus(interface="virtual", channel=bench.channel)))
        self.addCleanup(window.disconnect_ecu)
        names = ["communication.controldtcsetting_85", "testerpresent.testerpresent_3e"]
        for forced in ([], [{"sid": 0x85, "nrc": 0x22}]):
            bench.ecu.config.forced_nrcs = forced
            window.report = None
            window.run(names)
            self.assertTrue(spin_until(lambda: window.report is not None and window.run_action.isEnabled()))
            time.sleep(1.1)                                   # the next report gets its own second
        self.assertIn("Since the last run: 1 regression", window.log.toPlainText())
        self.assertIs(window.results.currentWidget(), window.comparison_tab, "shown: something regressed")
        self.assertIn("Regressions (1)", window.comparison_view.toPlainText())
        results = sorted((self.folder / "reports").glob("*.json"))
        self.assertEqual(len(results), 2)
        self.assertEqual(window.compare_runs(str(results[1]), str(results[0])).regressions[0].title,
                         "Communication: ControlDTCSetting (85)", "the earlier run is the first")
        self.assertTrue(window.save_comparison(self.folder / "comparison.html").exists())

    def test_the_toolbar(self):
        window = self.window
        labels = [action.text() for action in window.toolbar.actions() if not action.isSeparator()]
        self.assertEqual(len(labels), 11)
        buttons = [widget.defaultAction().iconText() for widget in window.toolbar.findChildren(QToolButton)
                   if widget.defaultAction() is not None]
        self.assertEqual(buttons, ["Description", "Open plan", "Save plan", "Connect", "Run", "Stop", "Run failed",
                                   "Discover", "Compare", "Report", "Manual"])
        self.assertEqual(window.run_action.shortcut().toString(), "F5")
        self.assertEqual(window.rerun_action.shortcut().toString(), "Ctrl+F5")
        self.assertFalse(window.rerun_action.isEnabled(), "no run yet")
        self.assertFalse(window.stop_action.isEnabled(), "nothing to stop")
        self.assertFalse(window.report_action.isEnabled(), "no report yet")
        self.assertIs(window.connect_btn.defaultAction(), window.connect_action, "the ECU tab's button is the same")
        bench = Bench(self)
        self.assertTrue(window.connect_ecu(can.Bus(interface="virtual", channel=bench.channel)))
        self.assertEqual(window.connect_action.text(), "Disconnect")
        run_menu = next(action.menu() for action in window.menuBar().actions() if action.text() == "&Run")
        self.assertEqual(run_menu.actions()[0].text(), "&Disconnect", "the menu follows")
        window.disconnect_ecu()
        self.assertEqual((window.connect_action.text(), run_menu.actions()[0].text()), ("Connect", "&Connect"))

    def test_main_smoke_test(self):
        self.assertEqual(window_module.main(["--smoke-test"]), 0)
        self.assertEqual(window_module.main([str(DUMMY_CDD), "--smoke-test"]), 0)
        path = self.folder / "plan.json"
        TestPlan(description=str(DUMMY_CDD)).save(path)
        self.assertEqual(window_module.main([str(path), "--smoke-test"]), 0)


if __name__ == "__main__":
    unittest.main()
