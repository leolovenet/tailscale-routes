"""
Unit tests for tailscale-routes.py

Run with:
    /usr/bin/python3 -m unittest tests/test_tailscale_routes.py -v
"""

import importlib
import json
import logging
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Import the module under test.
# The file is named with a hyphen so we use importlib.
# ---------------------------------------------------------------------------

def _import_tailscale_routes():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "tailscale_routes",
        os.path.join(os.path.dirname(__file__), "..", "tailscale-routes.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

tr = _import_tailscale_routes()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_logger(name="tailscale-routes"):
    """Return (or re-use) the module logger so load_routes can find it."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


# Ensure logger exists before any test that needs it.
_make_logger()


# ===========================================================================
# 1. test_load_routes
# ===========================================================================

class TestLoadRoutes(unittest.TestCase):
    """Tests for load_routes() - normal cases."""

    def _write_routes(self, lines, newline="\n"):
        """Write a temporary routes file and return its path."""
        tf = tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                         delete=False, newline="")
        tf.write(newline.join(lines) + newline)
        tf.close()
        return tf.name

    def tearDown(self):
        # Nothing persistent to clean; temp files are unlinked per test.
        pass

    def test_filters_comments(self):
        path = self._write_routes([
            "# this is a comment",
            "10.0.0.0/8",
            "# another comment",
        ])
        try:
            result = tr.load_routes(path)
            self.assertEqual(result, {"10.0.0.0/8"})
            self.assertNotIn("# this is a comment", result)
        finally:
            os.unlink(path)

    def test_filters_empty_lines(self):
        path = self._write_routes([
            "",
            "192.168.0.0/16",
            "",
            "   ",   # whitespace-only
        ])
        try:
            result = tr.load_routes(path)
            self.assertIn("192.168.0.0/16", result)
            self.assertEqual(len(result), 1)
        finally:
            os.unlink(path)

    def test_strips_carriage_return(self):
        """Windows-style \r\n endings must not corrupt the CIDR."""
        path = self._write_routes(["172.16.0.0/12", "10.0.0.0/8"],
                                   newline="\r\n")
        try:
            result = tr.load_routes(path)
            self.assertIn("172.16.0.0/12", result)
            self.assertIn("10.0.0.0/8", result)
        finally:
            os.unlink(path)

    def test_normalizes_cidr(self):
        """ip_network(strict=False) should normalize host bits."""
        path = self._write_routes(["10.0.0.1/8"])   # host bits set
        try:
            result = tr.load_routes(path)
            # strict=False normalises to network address
            self.assertIn("10.0.0.0/8", result)
            self.assertNotIn("10.0.0.1/8", result)
        finally:
            os.unlink(path)

    def test_deduplicates(self):
        path = self._write_routes(["10.0.0.0/8", "10.0.0.0/8", "10.0.0.0/8"])
        try:
            result = tr.load_routes(path)
            self.assertEqual(len(result), 1)
        finally:
            os.unlink(path)

    def test_returns_set(self):
        path = self._write_routes(["192.168.1.0/24"])
        try:
            result = tr.load_routes(path)
            self.assertIsInstance(result, set)
        finally:
            os.unlink(path)

    def test_multiple_valid_routes(self):
        cidrs = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
        path = self._write_routes(cidrs)
        try:
            result = tr.load_routes(path)
            self.assertEqual(result, set(cidrs))
        finally:
            os.unlink(path)


# ===========================================================================
# 2. test_load_routes_invalid
# ===========================================================================

class TestLoadRoutesInvalid(unittest.TestCase):
    """Invalid CIDRs are skipped and a warning is logged."""

    def test_invalid_cidr_skipped(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                         delete=False) as tf:
            tf.write("10.0.0.0/8\n")
            tf.write("not-a-cidr\n")
            tf.write("256.256.256.256/24\n")
            tf.write("192.168.0.0/16\n")
            path = tf.name
        try:
            result = tr.load_routes(path)
            self.assertIn("10.0.0.0/8", result)
            self.assertIn("192.168.0.0/16", result)
            self.assertNotIn("not-a-cidr", result)
            self.assertNotIn("256.256.256.256/24", result)
        finally:
            os.unlink(path)

    def test_invalid_cidr_logs_warning(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                         delete=False) as tf:
            tf.write("bad-cidr\n")
            path = tf.name
        try:
            logger = logging.getLogger("tailscale-routes")
            with self.assertLogs(logger, level="WARNING") as cm:
                tr.load_routes(path)
            # At least one warning mentioning the bad value
            self.assertTrue(
                any("bad-cidr" in msg for msg in cm.output),
                f"Expected warning about 'bad-cidr', got: {cm.output}",
            )
        finally:
            os.unlink(path)

    def test_only_invalid_returns_empty_set(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                         delete=False) as tf:
            tf.write("garbage\n")
            path = tf.name
        try:
            result = tr.load_routes(path)
            self.assertEqual(result, set())
        finally:
            os.unlink(path)

    def test_missing_file_returns_empty_set(self):
        result = tr.load_routes("/tmp/nonexistent_routes_file_xyz.txt")
        self.assertEqual(result, set())


# ===========================================================================
# 3. test_parse_gateway
# ===========================================================================

NETSTAT_NORMAL = """\
Routing tables

Internet:
Destination        Gateway            Flags        Netif
default            192.168.1.1        UGScg        en0
default            100.96.0.1         UGScg        utun3
127                127.0.0.1          UCS          lo0
"""

NETSTAT_ONLY_UTUN = """\
Routing tables

Internet:
Destination        Gateway            Flags        Netif
default            100.96.0.1         UGScg        utun3
"""

NETSTAT_EMPTY = """\
Routing tables

Internet:
Destination        Gateway            Flags        Netif
"""


class TestParseGateway(unittest.TestCase):
    """Tests for get_gateway() with mocked subprocess.run."""

    def _mock_run(self, stdout):
        mock_result = MagicMock()
        mock_result.stdout = stdout
        return mock_result

    def test_returns_physical_gateway(self):
        with patch("subprocess.run", return_value=self._mock_run(NETSTAT_NORMAL)):
            gw = tr.get_gateway()
        self.assertEqual(gw, "192.168.1.1")

    def test_only_utun_returns_none(self):
        with patch("subprocess.run", return_value=self._mock_run(NETSTAT_ONLY_UTUN)):
            gw = tr.get_gateway()
        self.assertIsNone(gw)

    def test_empty_output_returns_none(self):
        with patch("subprocess.run", return_value=self._mock_run(NETSTAT_EMPTY)):
            gw = tr.get_gateway()
        self.assertIsNone(gw)

    def test_timeout_returns_none(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(
                cmd="netstat", timeout=5)):
            gw = tr.get_gateway()
        self.assertIsNone(gw)

    def test_oserror_returns_none(self):
        with patch("subprocess.run", side_effect=OSError("no such file")):
            gw = tr.get_gateway()
        self.assertIsNone(gw)

    def test_physical_gateway_before_utun(self):
        """Physical default appears first; utun entry must not override it."""
        netstat_out = (
            "Routing tables\n\nInternet:\n"
            "Destination   Gateway    Flags  Netif\n"
            "default       10.0.0.1   UGS    en1\n"
            "default       100.64.0.1 UGS    utun4\n"
        )
        with patch("subprocess.run", return_value=self._mock_run(netstat_out)):
            gw = tr.get_gateway()
        self.assertEqual(gw, "10.0.0.1")


# ===========================================================================
# 4. test_exit_node_detect
# ===========================================================================

class TestExitNodeDetect(unittest.TestCase):
    """Tests for is_exit_node_active() with mocked subprocess.run."""

    def _mock_run(self, stdout):
        mock_result = MagicMock()
        mock_result.stdout = stdout
        return mock_result

    def test_utun_in_output_returns_true(self):
        output = "   interface: utun3\n   gateway: 100.64.0.1\n"
        with patch("subprocess.run", return_value=self._mock_run(output)):
            self.assertTrue(tr.is_exit_node_active())

    def test_no_utun_returns_false(self):
        output = "   interface: en0\n   gateway: 192.168.1.1\n"
        with patch("subprocess.run", return_value=self._mock_run(output)):
            self.assertFalse(tr.is_exit_node_active())

    def test_empty_output_returns_false(self):
        with patch("subprocess.run", return_value=self._mock_run("")):
            self.assertFalse(tr.is_exit_node_active())

    def test_timeout_returns_false(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(
                cmd="route", timeout=5)):
            self.assertFalse(tr.is_exit_node_active())

    def test_oserror_returns_false(self):
        with patch("subprocess.run", side_effect=OSError("not found")):
            self.assertFalse(tr.is_exit_node_active())


# ===========================================================================
# 5. test_hot_reload_diff
# ===========================================================================

class TestHotReloadDiff(unittest.TestCase):
    """
    Tests for the set-difference logic used during hot-reload.
    The logic is: to_add = new - old, to_del = old - new.
    """

    def _diff(self, old, new):
        return new - old, old - new

    def test_partial_overlap(self):
        old = {"A", "B", "C"}
        new = {"B", "C", "D"}
        to_add, to_del = self._diff(old, new)
        self.assertEqual(to_add, {"D"})
        self.assertEqual(to_del, {"A"})

    def test_same_sets(self):
        old = {"X", "Y"}
        new = {"X", "Y"}
        to_add, to_del = self._diff(old, new)
        self.assertEqual(to_add, set())
        self.assertEqual(to_del, set())

    def test_all_new(self):
        old = set()
        new = {"A", "B", "C"}
        to_add, to_del = self._diff(old, new)
        self.assertEqual(to_add, new)
        self.assertEqual(to_del, set())

    def test_all_removed(self):
        old = {"A", "B"}
        new = set()
        to_add, to_del = self._diff(old, new)
        self.assertEqual(to_add, set())
        self.assertEqual(to_del, old)

    def test_both_empty(self):
        to_add, to_del = self._diff(set(), set())
        self.assertEqual(to_add, set())
        self.assertEqual(to_del, set())


# ===========================================================================
# 6. test_config_loading
# ===========================================================================

class TestConfigLoading(unittest.TestCase):
    """Tests for load_config() using a temporary file."""

    def _write_conf(self, content):
        tf = tempfile.NamedTemporaryFile(mode="w", suffix=".conf",
                                          delete=False)
        tf.write(content)
        tf.close()
        return tf.name

    def test_basic_key_value(self):
        path = self._write_conf(
            "ROUTES_FILE=/etc/routes.txt\n"
            "STATE_FILE=/tmp/state.json\n"
        )
        try:
            cfg = tr.load_config(path)
            self.assertEqual(cfg["ROUTES_FILE"], "/etc/routes.txt")
            self.assertEqual(cfg["STATE_FILE"], "/tmp/state.json")
        finally:
            os.unlink(path)

    def test_strips_double_quotes(self):
        path = self._write_conf('LOG_FILE="/var/log/ts.log"\n')
        try:
            cfg = tr.load_config(path)
            self.assertEqual(cfg["LOG_FILE"], "/var/log/ts.log")
        finally:
            os.unlink(path)

    def test_strips_single_quotes(self):
        path = self._write_conf("ROUTE_HELPER='/usr/local/bin/rh'\n")
        try:
            cfg = tr.load_config(path)
            self.assertEqual(cfg["ROUTE_HELPER"], "/usr/local/bin/rh")
        finally:
            os.unlink(path)

    def test_skips_comments(self):
        path = self._write_conf("# comment\nKEY=value\n")
        try:
            cfg = tr.load_config(path)
            self.assertIn("KEY", cfg)
            self.assertNotIn("# comment", cfg)
        finally:
            os.unlink(path)

    def test_skips_empty_lines(self):
        path = self._write_conf("\n\nKEY=val\n\n")
        try:
            cfg = tr.load_config(path)
            self.assertEqual(cfg, {"KEY": "val"})
        finally:
            os.unlink(path)

    def test_value_with_equals_sign(self):
        """Values may contain '=' — only the first '=' splits key/value."""
        path = self._write_conf("URL=http://example.com/path?a=1&b=2\n")
        try:
            cfg = tr.load_config(path)
            self.assertEqual(cfg["URL"], "http://example.com/path?a=1&b=2")
        finally:
            os.unlink(path)


# ===========================================================================
# 7. test_route_helper_json
# ===========================================================================

class TestRouteHelperJson(unittest.TestCase):
    """Tests for call_route_helper() with mocked subprocess.run."""

    def _mock_result(self, stdout="", returncode=0):
        m = MagicMock()
        m.stdout = stdout
        m.returncode = returncode
        return m

    def test_parse_json_output(self):
        payload = json.dumps({"total": 5, "ok": 5, "failed": 0})
        with patch("subprocess.run",
                   return_value=self._mock_result(stdout=payload)):
            success, stats = tr.call_route_helper(
                "/usr/local/bin/rh", "add",
                ["10.0.0.0/8"], "192.168.1.1"
            )
        self.assertTrue(success)
        self.assertEqual(stats["total"], 5)
        self.assertEqual(stats["failed"], 0)

    def test_empty_stdout_returns_empty_dict(self):
        with patch("subprocess.run",
                   return_value=self._mock_result(stdout="")):
            success, stats = tr.call_route_helper(
                "/usr/local/bin/rh", "del", ["10.0.0.0/8"]
            )
        self.assertTrue(success)
        self.assertEqual(stats, {})

    def test_returncode_0_is_success(self):
        with patch("subprocess.run",
                   return_value=self._mock_result(stdout="{}", returncode=0)):
            success, _ = tr.call_route_helper(
                "/usr/local/bin/rh", "add", ["10.0.0.0/8"], "gw"
            )
        self.assertTrue(success)

    def test_returncode_1_is_success(self):
        """returncode <= 1 is treated as success (partial failure allowed)."""
        with patch("subprocess.run",
                   return_value=self._mock_result(stdout="{}", returncode=1)):
            success, _ = tr.call_route_helper(
                "/usr/local/bin/rh", "add", ["10.0.0.0/8"], "gw"
            )
        self.assertTrue(success)

    def test_returncode_2_is_failure(self):
        with patch("subprocess.run",
                   return_value=self._mock_result(stdout="{}", returncode=2)):
            success, _ = tr.call_route_helper(
                "/usr/local/bin/rh", "add", ["10.0.0.0/8"], "gw"
            )
        self.assertFalse(success)

    def test_subprocess_timeout_returns_false(self):
        import subprocess
        with patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="sudo", timeout=30)):
            success, stats = tr.call_route_helper(
                "/usr/local/bin/rh", "add", ["10.0.0.0/8"], "gw"
            )
        self.assertFalse(success)
        self.assertEqual(stats, {})

    def test_oserror_returns_false(self):
        with patch("subprocess.run", side_effect=OSError("permission denied")):
            success, stats = tr.call_route_helper(
                "/usr/local/bin/rh", "add", ["10.0.0.0/8"], "gw"
            )
        self.assertFalse(success)
        self.assertEqual(stats, {})

    def test_invalid_json_returns_false(self):
        with patch("subprocess.run",
                   return_value=self._mock_result(stdout="not-json")):
            success, stats = tr.call_route_helper(
                "/usr/local/bin/rh", "add", ["10.0.0.0/8"], "gw"
            )
        self.assertFalse(success)
        self.assertEqual(stats, {})

    def test_gateway_appended_to_cmd(self):
        """When gateway is provided it must appear in the command."""
        captured = []

        def fake_run(cmd, **kwargs):
            captured.append(cmd)
            return self._mock_result(stdout="{}")

        with patch("subprocess.run", side_effect=fake_run):
            tr.call_route_helper("/rh", "add", ["10.0.0.0/8"], "10.1.1.1")

        self.assertIn("10.1.1.1", captured[0])

    def test_no_gateway_not_in_cmd(self):
        """When gateway is None it must NOT be appended."""
        captured = []

        def fake_run(cmd, **kwargs):
            captured.append(cmd)
            return self._mock_result(stdout="{}")

        with patch("subprocess.run", side_effect=fake_run):
            tr.call_route_helper("/rh", "del", ["10.0.0.0/8"])

        self.assertEqual(captured[0], ["sudo", "/rh", "del"])


if __name__ == "__main__":
    unittest.main()
