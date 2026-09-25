# -*- coding: utf-8 -*-
"""Регрессия механизма Дувра: коннектор не должен нести service, из которого Valhalla
сама выводит destination_only, — иначе первый проход A* исключает обязательный подъезд
к парому. Явный access=* при этом обязан сохраниться (он и есть законность проезда)."""
import sys, unittest
sys.path.insert(0, sys.argv.pop(1) if len(sys.argv) > 1 else ".")
from promote import promote_line

def tags(line):
    t = [f for f in line.split() if f.startswith("T")][0][1:]
    return dict(kv.split("=", 1) for kv in t.split(","))

class PromoteTest(unittest.TestCase):
    def test_dover_parking_aisle_loses_auto_destonly(self):
        # w167792665 как он есть в connectors.opl
        l, is_way, ren = promote_line("w167792665 v1 dV c0 t i0 u Thighway=service,lit=yes,oneway=yes,service=parking_aisle Nn1,n2\n")
        t = tags(l)
        self.assertTrue(is_way and ren)
        self.assertEqual(t["highway"], "tertiary")
        self.assertNotIn("service", t)
        self.assertEqual(t["wedrive:service"], "parking_aisle")
        self.assertEqual(t["oneway"], "yes")
    def test_driveway_and_drive_through(self):
        for v in ("driveway", "drive-through"):
            l, _, ren = promote_line("w1 v1 Thighway=service,service=%s Nn1,n2\n" % v)
            self.assertTrue(ren); self.assertNotIn("service", tags(l))
    def test_explicit_access_is_kept(self):
        l, _, ren = promote_line("w2 v1 Thighway=service,service=driveway,access=customers Nn1,n2\n")
        t = tags(l)
        self.assertTrue(ren); self.assertEqual(t["access"], "customers")
    def test_other_service_untouched(self):
        for v in ("alley", "slipway", "emergency_access", "ferry_access"):
            l, _, ren = promote_line("w3 v1 Thighway=service,service=%s Nn1,n2\n" % v)
            self.assertFalse(ren); self.assertEqual(tags(l)["service"], v)
    def test_plain_connector_only_promoted(self):
        l, _, ren = promote_line("w4 v1 Thighway=unclassified,name=X Nn1,n2\n")
        self.assertFalse(ren); self.assertEqual(tags(l), {"highway": "tertiary", "name": "X"})
    def test_nodes_pass_through(self):
        l, is_way, ren = promote_line("n5 v1 x1.0 y2.0\n")
        self.assertEqual((l, is_way, ren), ("n5 v1 x1.0 y2.0\n", False, False))

if __name__ == "__main__":
    unittest.main(verbosity=1)
