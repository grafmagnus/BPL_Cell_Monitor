import io
import json
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import paramiko
import password_auth as auth
import credentials
from monitor import load_config, ssh_output


class PasswordTests(unittest.TestCase):
    def setUp(self):
        auth.get_password.cache_clear()
        auth.secure_backend.cache_clear()
        self.cfg = {"credential_service": "test-only", "ssh_user": "root", "headend_auth": "keyring",
                    "jumphost": "NMS3_Infra", "connect_timeout_seconds": 3, "timeout_seconds": 5}
        self.settings = {"hostname": "10.1.2.3", "port": "22", "hostkeyalias": "002518047a06.eui"}

    def tearDown(self):
        auth.get_password.cache_clear()
        auth.secure_backend.cache_clear()

    def test_plaintext_backend_is_refused(self):
        backend = type("PlaintextKeyring", (), {"__module__": "keyrings.alt.file", "priority": 1})()
        with patch("keyring.get_keyring", return_value=backend), self.assertRaisesRegex(RuntimeError, "refused"):
            auth.secure_backend()

    def test_native_backend_is_selected_from_chain(self):
        backend = type("NativeTest", (), {"__module__": "keyring.backends.Windows", "priority": 1})()
        with patch("keyring.get_keyring", return_value=Mock(backends=[backend])):
            self.assertIs(auth.secure_backend(), backend)

    def test_per_host_override_then_shared_fallback(self):
        backend = Mock()
        backend.get_password.side_effect = ["host-secret", None, "shared-secret"]
        with patch("password_auth.secure_backend", return_value=backend):
            self.assertEqual(auth.get_password("svc", "root", "he-one"), "host-secret")
            self.assertEqual(auth.get_password("svc", "root", "he-two"), "shared-secret")
        self.assertEqual([c.args for c in backend.get_password.call_args_list],
                         [("svc", "root@he-one"), ("svc", "root@he-two"), ("svc", "root@*")])

    def test_backend_errors_do_not_disclose_exception_text(self):
        backend = Mock()
        backend.get_password.side_effect = RuntimeError("TOP_SECRET_TEST")
        with patch("password_auth.secure_backend", return_value=backend), self.assertRaises(RuntimeError) as error:
            auth.get_password("svc", "root", "he")
        self.assertNotIn("TOP_SECRET_TEST", str(error.exception))

    def test_password_backend_dispatch(self):
        with patch("password_auth.password_ssh_output", return_value="table") as call:
            self.assertEqual(ssh_output("he", self.cfg, "10.1.2.3", "root"), "table")
        call.assert_called_once_with("he", self.cfg, "10.1.2.3", "root")

    def test_jump_argv_contains_no_headend_password(self):
        command = auth.jump_command(self.settings, self.cfg)
        self.assertEqual(command[-3:], ["-W", "10.1.2.3:22", "NMS3_Infra"])
        self.assertNotIn("password", " ".join(command).lower())
        cfg = dict(self.cfg, jumphost=None)
        settings = dict(self.settings, proxyjump="jump-a,jump-b")
        command = auth.jump_command(settings, cfg)
        self.assertEqual(command[command.index("-J")+1], "jump-a")
        self.assertEqual(command[-1], "jump-b")

    def test_socket_bridge_roundtrip_without_network_or_ssh(self):
        script = "import os; data=os.read(0,65536); os.write(1,data)"
        sock = auth.NativeSSHSocket([sys.executable, "-u", "-c", script])
        try:
            sock.settimeout(5)
            sock.sendall(b"bridge probe")
            received = b""
            while len(received) < len(b"bridge probe"):
                chunk = sock.recv(100)
                if not chunk:
                    break
                received += chunk
            self.assertEqual(received, b"bridge probe")
        finally:
            sock.close()
            sock.close()
        self.assertIsNotNone(sock.process.poll())

    def test_password_connect_enforces_hostkey_and_drains_both_streams(self):
        client, tunnel, timer = Mock(), Mock(), Mock()
        channel = Mock()
        channel.recv_ready.side_effect = [True, False]
        channel.recv.return_value = b"table output"
        channel.recv_stderr_ready.side_effect = [True, False]
        channel.recv_stderr.return_value = b"server message"
        channel.exit_status_ready.return_value = True
        channel.recv_exit_status.return_value = 0
        client.exec_command.return_value = (Mock(), Mock(channel=channel), Mock())
        with patch("password_auth.get_password", return_value="TOP_SECRET_TEST"), patch(
                "password_auth.effective_config", return_value=self.settings), patch(
                "paramiko.SSHClient", return_value=client), patch(
                "password_auth.NativeSSHSocket", return_value=tunnel), patch(
                "password_auth.threading.Timer", return_value=timer):
            self.assertEqual(auth.password_ssh_output("he", self.cfg, "10.1.2.3", "root"), "table output")
        self.assertIsInstance(client.set_missing_host_key_policy.call_args.args[0], paramiko.RejectPolicy)
        options = client.connect.call_args.kwargs
        self.assertEqual(options["hostname"], "002518047a06.eui")
        self.assertEqual(options["password"], "TOP_SECRET_TEST")
        self.assertFalse(options["look_for_keys"])
        self.assertFalse(options["allow_agent"])
        client.close.assert_called()
        tunnel.close.assert_called()
        timer.cancel.assert_called_once()

    def test_connection_errors_are_redacted_and_resources_closed(self):
        client, tunnel = Mock(), Mock()
        client.connect.side_effect = paramiko.SSHException("TOP_SECRET_TEST")
        with patch("password_auth.get_password", return_value="TOP_SECRET_TEST"), patch(
                "password_auth.effective_config", return_value=self.settings), patch(
                "paramiko.SSHClient", return_value=client), patch(
                "password_auth.NativeSSHSocket", return_value=tunnel), patch(
                "password_auth.threading.Timer"), self.assertRaises(RuntimeError) as error:
            auth.password_ssh_output("he", self.cfg, "10.1.2.3", "root")
        self.assertNotIn("TOP_SECRET_TEST", str(error.exception))
        tunnel.close.assert_called()
        client.close.assert_called()

    def test_credential_setup_hidden_prompt_and_no_secret_output(self):
        backend = Mock()
        cfg = dict(self.cfg, headends=[{"ssh_host": "he"}])
        captured = io.StringIO()
        with patch("sys.argv", ["credentials.py", "set"]), patch("credentials.load_config", return_value=cfg), patch(
                "credentials.secure_backend", return_value=backend), patch(
                "sys.stdin.isatty", return_value=True), patch(
                "credentials.getpass.getpass", side_effect=["TOP_SECRET_TEST", "TOP_SECRET_TEST"]), patch("sys.stdout", captured):
            credentials.main()
        backend.set_password.assert_called_once_with("test-only", "root@*", "TOP_SECRET_TEST")
        self.assertNotIn("TOP_SECRET_TEST", captured.getvalue())

    def test_redirected_input_cannot_store_a_password(self):
        backend = Mock()
        cfg = dict(self.cfg, headends=[{"ssh_host": "he"}])
        with patch("sys.argv", ["credentials.py", "set"]), patch("credentials.load_config", return_value=cfg), patch(
                "credentials.secure_backend", return_value=backend), patch(
                "sys.stdin.isatty", return_value=False), self.assertRaises(RuntimeError):
            credentials.main()
        backend.set_password.assert_not_called()

    def test_plaintext_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for extra in [{"password": "synthetic"}, {"headend_password": "synthetic"}, {"headend_auth": "plaintext"}]:
                path.write_text(json.dumps(dict(headends=[{"name": "he", "ssh_host": "he"}], **extra)))
                with self.subTest(extra=extra), self.assertRaises(ValueError):
                    load_config(path)

    def test_mismatched_setup_does_not_write(self):
        backend = Mock()
        cfg = dict(self.cfg, headends=[{"ssh_host": "he"}])
        with patch("sys.argv", ["credentials.py", "set"]), patch("credentials.load_config", return_value=cfg), patch(
                "credentials.secure_backend", return_value=backend), patch(
                "sys.stdin.isatty", return_value=True), patch(
                "credentials.getpass.getpass", side_effect=["one", "two"]), self.assertRaises(RuntimeError):
            credentials.main()
        backend.set_password.assert_not_called()

    def test_real_ssh_password_handshake_and_command_over_local_socket_pair(self):
        # Real Paramiko SSH handshake with a generated host key, no network hosts
        # and no access to the user's actual credential store.
        command_seen, finished = threading.Event(), threading.Event()
        class Server(paramiko.ServerInterface):
            def check_auth_password(self, username, password):
                return paramiko.AUTH_SUCCESSFUL if (username, password) == ("root", "synthetic") else paramiko.AUTH_FAILED
            def get_allowed_auths(self, username):
                return "password"
            def check_channel_request(self, kind, chanid):
                return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
            def check_channel_exec_request(self, channel, command):
                if command != b"ppcdinfo unittable":
                    return False
                command_seen.set()
                return True
        server_socket, client_socket = socket.socketpair()
        transport = paramiko.Transport(server_socket)
        host_key = paramiko.RSAKey.generate(2048)
        transport.add_server_key(host_key)
        problems = []
        def serve():
            try:
                transport.start_server(server=Server())
                channel = transport.accept(5)
                if channel is None or not command_seen.wait(5):
                    raise RuntimeError("No command received")
                channel.sendall(b"ethmac | id | cell | age |\n---+---+---+---+\n")
                channel.send_exit_status(0)
                channel.shutdown_write()
                finished.wait(5)
            except Exception as exc:
                problems.append(type(exc).__name__)
            finally:
                transport.close()
        with tempfile.TemporaryDirectory() as directory:
            known = Path(directory) / "known_hosts"
            host_keys = paramiko.HostKeys()
            host_keys.add("test.eui", host_key.get_name(), host_key)
            host_keys.save(str(known))
            settings = dict(self.settings, hostkeyalias="test.eui", userknownhostsfile=str(known).replace("\\", "/"), proxyjump="none")
            cfg = dict(self.cfg, jumphost=None)
            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            try:
                with patch("password_auth.get_password", return_value="synthetic"), patch(
                        "password_auth.effective_config", return_value=settings), patch(
                        "password_auth.socket.create_connection", return_value=client_socket):
                    output = auth.password_ssh_output("test.eui", cfg, "10.1.2.3", "root")
                self.assertIn("ethmac | id", output)
            finally:
                finished.set()
                worker.join(5)
                transport.close()
                transport.join(1)
                client_socket.close()
                server_socket.close()
            self.assertFalse(problems, problems)


if __name__ == "__main__":
    unittest.main()
