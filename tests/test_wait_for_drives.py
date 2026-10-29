"""wait-for-drives.sh: a service starts only once every volume it needs is mounted (fake `mount` on PATH)."""
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "wait-for-drives.sh"


class Wrapper(unittest.TestCase):
    def run_with_mount(self, mount_lines, *cmd, wait=None, env_extra=None):
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "mount"
            fake.write_text("#!/bin/sh\nprintf '%s'\n" % "".join(l + "\\n" for l in mount_lines)); fake.chmod(0o755)
            env = {**os.environ, "PATH": f"{td}:/usr/bin:/bin", "WAIT_FOR_DRIVES_STEP": "1", **(env_extra or {})}
            p = subprocess.Popen(["/bin/bash", str(WRAPPER), *cmd], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if wait:
                time.sleep(wait); p.terminate()
            out, err = p.communicate(timeout=20)
            return p.returncode, out, err

    def test_mounted_volume_execs_the_program(self):
        rc, out, _ = self.run_with_mount(["/dev/disk7s1 on /Volumes/Media1 (apfs, local, journaled)"], "Media1", "/bin/echo", "started", "x")
        self.assertEqual((rc, out.strip()), (0, "started x"))

    def test_missing_volume_waits_and_logs(self):
        rc, out, _ = self.run_with_mount(["/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)"], "Media1", "/bin/echo", "NO", wait=2.5)
        self.assertEqual(rc, 143)                                                   # launchd's SIGTERM ends the wait
        self.assertIn("waiting for Media1", out); self.assertNotIn("NO", out.replace("waiting", ""))

    def test_every_listed_volume_is_required(self):
        lines = ["/dev/disk7s1 on /Volumes/Media1 (apfs, local, journaled)"]
        rc, out, _ = self.run_with_mount(lines, "Media1,Media2", "/bin/echo", "NO", wait=2.5)
        self.assertEqual(rc, 143); self.assertIn("waiting for Media2", out)

    def test_a_similar_name_is_not_a_match(self):
        rc, out, _ = self.run_with_mount(["/dev/disk9s1 on /Volumes/Media1 1 (apfs, local, journaled)"], "Media1", "/bin/echo", "NO", wait=2.5)
        self.assertEqual(rc, 143)                                                   # "Media1 1" is the shadow-mount case, not Media1

    def test_a_cap_starts_the_program_without_the_missing_drive(self):
        rc, out, _ = self.run_with_mount(["/dev/disk7s1 on /Volumes/Media1 (apfs, local, journaled)"], "Media1,Backups",
                                         "/bin/echo", "started", env_extra={"WAIT_FOR_DRIVES_MAX_S": "2"})
        self.assertEqual(rc, 0)
        self.assertIn("still missing Backups after 2s — starting anyway (cap 2s)", out)
        self.assertNotIn("drives mounted", out)
        self.assertEqual(out.strip().splitlines()[-1], "started")

    def test_no_program_is_a_usage_error(self):
        rc, _, err = self.run_with_mount([], "Media1")
        self.assertEqual(rc, 64); self.assertIn("no program", err)


if __name__ == "__main__":
    unittest.main()
