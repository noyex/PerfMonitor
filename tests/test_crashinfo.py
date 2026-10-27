import json
import os
import tempfile
import unittest

from perfmon import crashinfo as ci

AE_NAMED = {"AppName": "cod.exe", "AppVersion": "2.0.452.3064", "ModuleName": "amdxc64.dll",
            "ModuleVersion": "32.0.32015.7029", "ExceptionCode": "c0000005", "FaultingOffset": "000000000361d946",
            "IntegratorReportId": "1de51770-c6c3-4e09-a131-a2d0aac9e0d1"}
AE_TEXT_PL = ("Nazwa aplikacji powodującej błąd: cod.exe, wersja: 2.0.452.3064, sygnatura czasowa: 0x68dd048d\r\n"
              "Nazwa modułu powodującego błąd: ntdll.dll, wersja: 10.0.26100.1, sygnatura czasowa: 0xc99d58de\r\n"
              "Kod wyjątku: 0xc0000374\r\nPrzesunięcie błędu: 0x000000000361d946")


class ParseTest(unittest.TestCase):
    def test_app_error_named_data(self):
        f = ci.parse_app_error({"provider": "Application Error", "data": json.dumps(AE_NAMED), "message": ""})
        self.assertEqual((f["app"], f["module"], f["exception_code"]), ("cod.exe", "amdxc64.dll", 0xC0000005))
        self.assertEqual(f["report_id"], AE_NAMED["IntegratorReportId"])

    def test_app_error_positional_list(self):
        vals = ["cod.exe", "1.0", "0", "UnityPlayer.dll", "2.0", "0", "c0000409", "0x10", "0x1b4c"]
        f = ci.parse_app_error({"provider": "Application Error", "data": json.dumps(vals), "message": ""})
        self.assertEqual((f["module"], f["exception_code"]), ("UnityPlayer.dll", 0xC0000409))

    def test_app_error_bracket_fields_in_message(self):
        msg = "tekst [AppName=cod.exe; ModuleName=dxgi.dll; ExceptionCode=887a0006; FaultingOffset=00ff]"
        f = ci.parse_app_error({"provider": "Application Error", "data": None, "message": msg})
        self.assertEqual((f["module"], f["exception_code"]), ("dxgi.dll", 0x887A0006))

    def test_app_error_localized_text_only(self):
        f = ci.parse_app_error({"provider": "Application Error", "data": None, "message": AE_TEXT_PL})
        self.assertEqual((f["module"], f["exception_code"]), ("ntdll.dll", 0xC0000374))

    def test_wer_appcrash_and_bex(self):
        d = {"EventName": "APPCRASH", **{f"P{i}": v for i, v in enumerate(
            ["cod.exe", "2.0", "ts", "amdxc64.dll", "32.0", "ts", "c0000005", "00ff"], 1)},
             "StorePath": "\\\\?\\C:\\ProgramData\\Microsoft\\Windows\\WER\\ReportArchive\\AppCrash_cod.exe_x"}
        w = ci.parse_wer({"data": json.dumps(d), "message": ""})
        self.assertEqual(w["report_path"], r"C:\ProgramData\Microsoft\Windows\WER\ReportArchive\AppCrash_cod.exe_x")
        self.assertEqual(ci.wer_crash_fields(w)["exception_code"], 0xC0000005)
        d.update(EventName="BEX64", P7="00ff", P8="c0000409")
        f = ci.wer_crash_fields(ci.parse_wer({"data": json.dumps(d), "message": ""}))
        self.assertEqual((f["module"], f["exception_code"], f["offset"]), ("amdxc64.dll", 0xC0000409, "00ff"))

    def test_report_wer_utf16(self):
        with tempfile.TemporaryDirectory() as td:
            txt = ("Version=1\r\nEventType=APPCRASH\r\nFriendlyEventName=Przestał działać\r\nAppPath=D:\\g\\cod.exe\r\n"
                   "Sig[0].Name=Nazwa aplikacji\r\nSig[0].Value=cod.exe\r\nSig[3].Name=skip\r\n"
                   "Sig[1].Name=Moduł\r\nSig[1].Value=amdxc64.dll\r\nDynamicSig[1].Name=Wersja systemu\r\n"
                   "DynamicSig[1].Value=10.0\r\nUI[2]=D:\\g\\cod.exe\r\n")
            with open(os.path.join(td, "Report.wer"), "wb") as f:
                f.write(b"\xff\xfe" + txt.encode("utf-16-le"))
            r = ci.read_report_wer(td)
        self.assertEqual(r["sig"], {"Nazwa aplikacji": "cod.exe", "Moduł": "amdxc64.dll"})
        self.assertEqual(r["dynamic_sig"], {})   # index 0 missing -> pairs stop at the first gap
        self.assertEqual((r["event_type"], r["ui2"]), ("APPCRASH", "D:\\g\\cod.exe"))

    def test_report_wer_missing(self):
        self.assertIsNone(ci.read_report_wer(r"C:\definitely\not\here"))


class InterpretTest(unittest.TestCase):
    def v(self, code, module, exe="cod.exe", history=None):
        return ci.interpret(code, module, exe, history)

    def test_dxgi_device_removed(self):
        self.assertEqual(self.v(0x887A0005, "d3d12core.dll")["confidence"], "high")
        self.assertIn("GPU przestało odpowiadać", self.v(0x887A0006, None)["verdict"])

    def test_gpu_driver_module(self):
        self.assertIn("sterowniku", self.v(0xC0000005, "amdxc64.dll")["verdict"])

    def test_game_module(self):
        self.assertIn("gry/silnika", self.v(0xC0000005, "cod.exe")["verdict"])
        self.assertIn("gry/silnika", self.v(0xC0000005, "Game-Win64-Shipping.exe", "game-win64-shipping.exe")["verdict"])

    def test_unstable_cpu_ram_needs_three_modules(self):
        hist = [(0xC0000409, "a.dll"), (0xC0000005, "b.dll"), (0xC000001D, "cod.exe")]
        self.assertIn("niestabilnego CPU/RAM", self.v(0xC0000409, "a.dll", history=hist)["verdict"])
        self.assertNotIn("niestabilnego CPU/RAM", self.v(0xC0000409, "a.dll", history=hist[:2])["verdict"])

    def test_dotnet_heap_system(self):
        self.assertIn(".NET", self.v(0xE0434352, "KERNELBASE.dll")["verdict"])
        self.assertIn("sterty", self.v(0xC0000374, "ntdll.dll")["verdict"])
        self.assertEqual(self.v(0xC0000005, "ntdll.dll")["confidence"], "low")

    def test_every_verdict_has_steps(self):
        for code, mod in ((None, None), (0x1234, "x.dll"), (0xC0000006, "cod.exe"), (0xC0000005, "rtsshooks64.dll")):
            r = self.v(code, mod)
            self.assertTrue(r["verdict"] and r["next_steps"] and r["confidence"] in ("high", "medium", "low"))

    def test_finding_hang_vs_crash(self):
        self.assertIn("przestał odpowiadać", ci.finding({"hang": True, "interpretation": {}}, "cod.exe")[0])
        t, d = ci.finding({"module": "amdxc64.dll", "exception_code": "0xC0000005", "exception_name": "ACCESS_VIOLATION",
                           "interpretation": self.v(0xC0000005, "amdxc64.dll")}, "cod.exe")
        self.assertIn("amdxc64.dll", t)
        self.assertIn("Kroki", d)


if __name__ == "__main__":
    unittest.main()
