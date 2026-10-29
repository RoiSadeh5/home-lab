"""scripts/tailscale-selfcheck.sh (7 Oct 2026): the server notices its own Tailscale switched off, reconnects after the grace,
alerts only when that fails. Fake scutil / tailscale / ntfy commands — nothing here touches the live tunnel or sends a push."""
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "tailscale-selfcheck.sh"
T0 = 1_800_000_000

FAKE_SCUTIL = """#!/bin/bash
cat "$FAKE/vpn"
"""
FAKE_TAILSCALE = """#!/bin/bash
case "$1" in
  status) [ "$(cat "$FAKE/backend")" = SILENT ] && { echo 'CLIError error 3' >&2; exit 1; }
          printf '{\\n  "Version": "1.102.4",\\n  "BackendState": "%s",\\n  "Self": {}\\n}\\n' "$(cat "$FAKE/backend")" ;;
  up) shift; echo "$*" >> "$FAKE/up.calls"
      mode=$(cat "$FAKE/up_mode")
      if [ "$mode" = flags ] && [ "$*" = "--accept-routes --timeout=60s" ]; then
        printf 'Error: changing settings via '"'"'tailscale up'"'"' requires mentioning all\\nnon-default flags. To proceed, either re-run your command with --reset or\\nuse the command below to explicitly mention the current value of\\nall non-default settings:\\n\\n\\ttailscale up --accept-routes --ssh\\n' >&2
        exit 1
      fi
      if [ "$mode" = login ]; then
        printf '\\nTo authenticate, visit:\\n\\n\\thttps://login.tailscale.com/a/0123456789\\n\\ntimeout waiting for Tailscale service to enter a Running state; check health with "tailscale status"\\n' >&2
        exit 1
      fi
      if [ "$mode" = fail ]; then echo 'timeout waiting for Tailscale service to enter a Running state' >&2; exit 1; fi
      echo Connected > "$FAKE/vpn"; echo Running > "$FAKE/backend" ;;
esac
"""
FAKE_RECORDER = """#!/bin/bash
printf '%s\\n' "$*" >> "$FAKE/{name}.calls"
"""


class TailscaleSelfcheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = pathlib.Path(self.tmp.name)
        for name, body in (("scutil", FAKE_SCUTIL), ("tailscale", FAKE_TAILSCALE),
                           ("incident", FAKE_RECORDER.replace("{name}", "incident")),
                           ("notify", FAKE_RECORDER.replace("{name}", "notify"))):
            p = self.d / name
            p.write_text(body)
            p.chmod(0o755)
        self.set(vpn="Connected", backend="Running", up_mode="ok")

    def tearDown(self):
        self.tmp.cleanup()

    def set(self, **kw):
        for k, v in kw.items():
            (self.d / k).write_text(v + "\n")

    def run_at(self, offset, boot=0):
        env = dict(os.environ, FAKE=str(self.d), TS_CLI=str(self.d / "tailscale"), TS_SCUTIL=str(self.d / "scutil"),
                   TS_STATE=str(self.d / "state"), TS_LOG=str(self.d / "selfcheck.log"),
                   TS_INCIDENT=str(self.d / "incident"), TS_NOTIFY=str(self.d / "notify"),
                   TS_NOW=str(T0 + offset), TS_BOOT=str(boot), TS_SETTLE_S="0", NTFY_DISABLED="1")
        r = subprocess.run(["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def calls(self, name):
        p = self.d / f"{name}.calls"
        return p.read_text().splitlines() if p.exists() else []

    def state(self):
        return dict(line.split("=", 1) for line in (self.d / "state").read_text().split())  if (self.d / "state").exists() else {}

    def log(self):
        p = self.d / "selfcheck.log"
        return p.read_text() if p.exists() else ""

    def test_connected_and_running_is_silent(self):
        self.run_at(0)
        self.assertEqual(self.state().get("down_since", ""), "")
        self.assertEqual((self.calls("up"), self.calls("incident"), self.calls("notify"), self.log()), ([], [], [], ""))

    def test_switched_off_waits_the_grace_then_reconnects_quietly(self):
        self.set(vpn="Disconnected")
        self.run_at(0)
        self.assertEqual(self.state()["down_since"], str(T0))
        self.assertIn("DOWN (vpn=Disconnected backend=not asked)", self.log())
        self.run_at(300)                                   # inside the 10 min grace: hands off
        self.assertEqual(self.calls("up"), [])
        self.run_at(600)
        self.assertEqual(self.calls("up"), ["--accept-routes --timeout=60s"])
        self.assertEqual(self.calls("incident"), [])
        self.assertEqual(self.calls("notify"), [])         # 11 Oct 2026: a self-heal is the log only (owner: only problems)
        self.assertIn("reconnected automatically after 10 min", self.log())
        self.assertEqual(self.state(), {"down_since": "", "alerted": "0", "tried": "0"})
        self.run_at(900)                                   # healthy again: nothing more
        self.assertEqual((len(self.calls("up")), len(self.calls("notify"))), (1, 0))

    def test_the_status_cli_is_not_asked_while_the_tunnel_is_down(self):
        # any `tailscale` CLI call starts the tunnel — inside the grace the script must not make one
        self.set(vpn="Disconnected", backend="Stopped")
        self.run_at(0); self.run_at(300)
        self.assertEqual(self.calls("up"), [])
        self.assertNotIn("backend=Stopped", self.log())

    def test_failed_reconnect_alerts_once_retries_and_resolves(self):
        self.set(vpn="Disconnected", up_mode="fail")
        self.run_at(0); self.run_at(600)
        inc = self.calls("incident")
        self.assertEqual(len(inc), 1)
        self.assertTrue(inc[0].startswith("tailscale-self active Tailscale בשרת (Mac Studio)"))
        self.assertIn("Connect", inc[0])
        self.assertEqual(self.state()["alerted"], "1")
        self.run_at(900)                                   # retried, not re-alerted
        self.assertEqual((len(self.calls("up")), len(self.calls("incident"))), (2, 1))
        self.set(vpn="Connected", backend="Running")       # someone clicked Connect
        self.run_at(1200)
        self.assertEqual(len(self.calls("incident")), 2)
        self.assertTrue(self.calls("incident")[1].startswith("tailscale-self resolved Tailscale בשרת (Mac Studio)"))
        self.assertEqual(self.calls("notify"), [])
        self.assertIn("UP again after 20 min (2 reconnect attempts before)", self.log())
        self.assertEqual(self.state(), {"down_since": "", "alerted": "0", "tried": "0"})

    def test_a_cli_that_does_not_answer_is_not_an_outage(self):
        # 6 Oct 2026: the friend app saw "CLIError 3" from the CLI under launchd — the VPN status alone decides then
        self.set(vpn="Connected", backend="SILENT")
        self.run_at(0); self.run_at(600); self.run_at(1200)
        self.assertEqual((self.calls("up"), self.calls("incident"), self.calls("notify"), self.log()), ([], [], [], ""))

    def test_needs_login_is_not_retried_with_up(self):
        self.set(vpn="Connected", backend="NeedsLogin")
        self.run_at(0); self.run_at(600); self.run_at(900)
        self.assertEqual(self.calls("up"), [])
        self.assertEqual(len(self.calls("incident")), 1)
        self.assertIn("Log in", self.calls("incident")[0])

    def test_non_default_flags_are_taken_from_the_cli_suggestion(self):
        self.set(vpn="Disconnected", up_mode="flags")
        self.run_at(0); self.run_at(600)
        self.assertEqual(self.calls("up"), ["--accept-routes --timeout=60s", "--accept-routes --ssh --timeout=60s"])
        self.assertIn("reconnected automatically", self.log())
        self.assertEqual((self.calls("notify"), self.calls("incident")), ([], []))

    def test_a_login_link_never_reaches_the_log_or_the_push(self):
        self.set(vpn="Disconnected", up_mode="login")
        self.run_at(0); self.run_at(600)
        self.assertNotIn("http", self.log())
        self.assertNotIn("http", "\n".join(self.calls("incident")))
        self.assertIn("timeout waiting for Tailscale", self.log())

    def test_an_outage_from_before_the_last_boot_restarts_the_grace(self):
        self.set(vpn="Disconnected")
        self.run_at(0)
        self.run_at(7200, boot=T0 + 7000)                  # rebooted since: the app connects only after login
        self.assertEqual(self.calls("up"), [])
        self.assertEqual(self.state()["down_since"], str(T0 + 7200))


if __name__ == "__main__":
    unittest.main()
