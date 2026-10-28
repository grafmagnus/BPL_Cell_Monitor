"""Protected credential storage and password SSH over an OpenSSH jump tunnel.

Secrets never appear in argv, environment variables, config, logs or SQLite.
"""
from functools import lru_cache
from pathlib import Path
import os
import shlex
import signal
import socket
import subprocess
import threading
import time


NATIVE_BACKENDS = {
    "keyring.backends.Windows", "keyring.backends.macOS",
    "keyring.backends.SecretService", "keyring.backends.kwallet",
}


class NativeSSHSocket:
    """Portable socket/stdio bridge (Windows cannot select() on SSH pipes)."""
    def __init__(self, command):
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, bufsize=0, **options)
        self.socket, self.relay = socket.socketpair()
        self._closed = False
        self._close_lock = threading.Lock()
        threading.Thread(target=self._outbound, daemon=True).start()
        threading.Thread(target=self._inbound, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()

    def __getattr__(self, name):
        return getattr(self.socket, name)

    def _outbound(self):
        try:
            while data := self.relay.recv(65536):
                # FileIO.write may be partial, particularly for Windows pipes.
                view = memoryview(data)
                while view:
                    count = self.process.stdin.write(view)
                    if not count:
                        return
                    view = view[count:]
        except (OSError, ValueError):
            pass

    def _inbound(self):
        try:
            while data := os.read(self.process.stdout.fileno(), 65536):
                self.relay.sendall(data)
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.relay.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _stderr(self):
        try:
            # Drain diagnostics without logging them or blocking the jump process.
            while os.read(self.process.stderr.fileno(), 65536):
                pass
        except (OSError, ValueError):
            pass

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            for endpoint in (self.socket, self.relay):
                try:
                    endpoint.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                endpoint.close()
            if self.process.poll() is None:
                try:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                    else:
                        os.killpg(self.process.pid, signal.SIGKILL)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                if self.process.poll() is None:
                    self.process.kill()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                try:
                    pipe.close()
                except OSError:
                    pass


@lru_cache(maxsize=1)
def secure_backend():
    try:
        import keyring
        backend = keyring.get_keyring()
        candidates = getattr(backend, "backends", [backend])
        for candidate in candidates:
            if type(candidate).__module__ in NATIVE_BACKENDS and candidate.priority > 0:
                return candidate
    except Exception:
        raise RuntimeError("Native credential store unavailable or locked; check the collector user/session") from None
    raise RuntimeError("No supported native credential store; plaintext and third-party fallback stores are refused")


def account(username, hostname=None):
    return f"{username}@{hostname.lower() if hostname else '*'}"


@lru_cache(maxsize=2048)
def get_password(service, username, hostname):
    backend = secure_backend()
    try:
        password = backend.get_password(service, account(username, hostname))
        if password is None:
            password = backend.get_password(service, account(username))
    except Exception:
        raise RuntimeError("Credential store read failed or access denied") from None
    if not password:
        raise RuntimeError(f"No headend password stored for {username}@{hostname}; run credentials.py set")
    return password


@lru_cache(maxsize=2048)
def _file_password(vault, keyfile, service, username, hostname):
    from encrypted_store import EncryptedFileBackend
    backend = EncryptedFileBackend(vault, keyfile)
    password = backend.get_password(service, account(username, hostname))
    if password is None:
        password = backend.get_password(service, account(username))
    if not password:
        raise RuntimeError(f"No headend password stored for {username}@{hostname}; run credentials.py set")
    return password


def configured_password(cfg, username, hostname):
    if cfg.get("credential_storage", "native") == "encrypted-file":
        return _file_password(cfg["credential_file"], cfg["credential_key_file"],
                              cfg["credential_service"], username, hostname)
    return get_password(cfg["credential_service"], username, hostname)


def effective_config(destination, cfg, resolved_ip, username):
    from monitor import ssh_command
    command = ssh_command(destination, cfg, resolved_ip, username)
    # -G evaluates configuration without opening an SSH connection.
    try:
        result = subprocess.run(["ssh", "-G"] + command[1:-1], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=cfg["connect_timeout_seconds"], check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Cannot evaluate OpenSSH configuration for password connection") from None
    if result.returncode:
        raise RuntimeError("OpenSSH configuration evaluation failed")
    settings = {}
    for line in result.stdout.decode("utf-8", "strict").splitlines():
        key, _, value = line.partition(" ")
        settings[key] = value.strip()
    return settings


def jump_command(settings, cfg):
    """Return native SSH argv or None for direct access; never carry passwords."""
    jump = cfg.get("jumphost") or settings.get("proxyjump")
    if jump and jump != "none":
        chain = jump.split(",")
        command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                   "-o", f"ConnectTimeout={cfg['connect_timeout_seconds']}",
                   "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2"]
        if len(chain) > 1:
            command += ["-J", ",".join(chain[:-1])]
        target = settings["hostname"]
        if ":" in target:
            target = f"[{target}]"
        return command + ["-W", f"{target}:{int(settings['port'])}", chain[-1]]
    if settings.get("proxycommand", "none") != "none":
        raise RuntimeError("Password mode requires jumphost/ProxyJump or direct access; custom ProxyCommand is unsupported")
    return None


def password_ssh_output(destination, cfg, resolved_ip, username):
    try:
        import paramiko
    except ImportError:
        raise RuntimeError("Password SSH requires: python -m pip install -r requirements-collector.txt") from None
    password = configured_password(cfg, username, destination)
    settings = effective_config(destination, cfg, resolved_ip, username)
    client, sock, channel, timer = paramiko.SSHClient(), None, None, None
    deadline = time.monotonic() + cfg["timeout_seconds"]

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("Password SSH exceeded its time limit")
        return value

    def abort():
        # Mark the transport closed before releasing its socket, avoiding noisy
        # reader errors on successful connections; also interrupts banner/auth.
        try:
            client.close()
        except Exception:
            pass
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    try:
        for key in ("globalknownhostsfile", "userknownhostsfile"):
            for filename in shlex.split(settings.get(key, "")):
                path = Path(filename).expanduser()
                if path.is_file():
                    client.load_host_keys(str(path))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        command = jump_command(settings, cfg)
        if command:
            sock = NativeSSHSocket(command)
        else:
            sock = socket.create_connection((settings["hostname"], int(settings["port"])),
                                            timeout=min(remaining(), cfg["connect_timeout_seconds"]))
        timer = threading.Timer(remaining(), abort)
        timer.daemon = True
        timer.start()
        identity = settings.get("hostkeyalias")
        if not identity or identity == "none":
            identity = settings["hostname"]
        client.connect(hostname=identity, port=int(settings["port"]), username=username,
                       password=password, sock=sock, allow_agent=False, look_for_keys=False,
                       timeout=min(remaining(), cfg["connect_timeout_seconds"]),
                       banner_timeout=remaining(), auth_timeout=remaining(), channel_timeout=remaining())
        _, stdout, _ = client.exec_command("ppcdinfo unittable", timeout=remaining())
        channel = stdout.channel
        output, errors = bytearray(), bytearray()
        while True:
            remaining()
            if channel.recv_ready():
                output.extend(channel.recv(65536))
            if channel.recv_stderr_ready():
                errors.extend(channel.recv_stderr(65536))
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            time.sleep(0.01)
        code = channel.recv_exit_status()
        if code:
            # Server output is deliberately not logged in password mode.
            raise RuntimeError(f"Headend command failed with exit {code}")
        return output.decode("utf-8", "strict")
    except paramiko.AuthenticationException:
        raise RuntimeError("Headend password authentication failed") from None
    except paramiko.BadHostKeyException:
        raise RuntimeError("Headend host key changed; verify known_hosts") from None
    except Exception as exc:
        # Never include library exception text, which could contain credentials.
        if isinstance(exc, TimeoutError) or time.monotonic() >= deadline:
            raise TimeoutError("Password SSH exceeded its time limit") from None
        raise RuntimeError(f"Password SSH failed ({type(exc).__name__}); check known_hosts, jump access and target") from None
    finally:
        if timer is not None:
            timer.cancel()
        if channel is not None:
            channel.close()
        abort()
