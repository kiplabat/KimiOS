"""0.8.0: the signed-in user may open the devices people plug in to be controlled, the programs that control
them are in the image, and KimiLM's toolbox is made ready in the background."""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RULES = REPO / "system_files/usr/lib/udev/rules.d/70-kimios-devices.rules"
UNIT = REPO / "system_files/usr/lib/systemd/user/kimios-toolbox.service"
BUILD = REPO / "build_files/build.sh"


def rules():
    return [ln for ln in RULES.read_text().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


class DeviceAccess(unittest.TestCase):
    def test_rules_come_before_the_seat_rules_that_apply_uaccess(self):
        self.assertLess(int(RULES.name.split("-")[0]), 73)

    def test_every_rule_only_adds_uaccess(self):
        for ln in rules():
            self.assertTrue(ln.endswith('TAG+="uaccess"'), ln)
            self.assertNotIn("MODE=", ln)          # never opened to every user
            self.assertNotIn("GROUP=", ln)
            self.assertEqual(ln.count('"') % 2, 0, ln)
            for key in re.findall(r'(\w+(?:\{\w+\})?)(==|\+=|=)"', ln):
                self.assertIn(key[0].split("{")[0], {"SUBSYSTEM", "KERNEL", "KERNELS", "ENV", "ATTR", "ATTRS", "TAG"}, ln)

    def test_phones_serial_ports_boards_and_displays_are_covered(self):
        text = "\n".join(rules())
        self.assertIn("*:ff4201:*", text)                         # adb
        self.assertIn('KERNEL=="ttyACM[0-9]*"', text)
        self.assertIn('KERNEL=="ttyUSB[0-9]*"', text)
        self.assertRegex(text, r"idVendor\}==\"[^\"]*2e8a")       # Raspberry Pi
        self.assertIn('SUBSYSTEM=="i2c-dev"', text)               # ddcutil
        self.assertNotRegex(text, r'SUBSYSTEM=="hidraw"(?!.*idVendor)')   # never every keyboard's raw input


    def test_game_controllers_only_from_controller_makers(self):
        """0.16.0: raw reports of game controllers, never of a keyboard maker's devices (045e, 046d)."""
        pads = [ln for ln in rules() if "hidraw" in ln and ("054c" in ln.lower())]
        self.assertEqual(len(pads), 2)                    # USB and Bluetooth
        for ln in pads:
            self.assertNotRegex(ln.lower(), r"045e|046d")
        self.assertTrue(any(ln.startswith('KERNEL=="hidraw*", KERNELS==') for ln in pads))


class ImageHasTheTools(unittest.TestCase):
    def test_device_programs_are_installed(self):
        text = BUILD.read_text()
        for pkg in ("android-tools", "ddcutil", "libimobiledevice-utils", "usbmuxd"):
            self.assertRegex(text, rf"\b{re.escape(pkg)}\b")

    def test_the_toolbox_unit_is_enabled_for_every_user_and_idempotent(self):
        self.assertIn("systemctl --global enable kimios-toolbox.service", BUILD.read_text())
        unit = UNIT.read_text()
        self.assertIn("grep -qw kimilm-tools || toolbox create -y kimilm-tools", unit)
        self.assertIn("WantedBy=default.target", unit)
        self.assertIn("Restart=on-failure", unit)


if __name__ == "__main__":
    unittest.main()
