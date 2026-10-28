"""Authenticated encrypted credential file, with a separate restricted key file."""
import csv
from functools import lru_cache
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

from cryptography.fernet import Fernet, InvalidToken


@lru_cache(maxsize=1)
def windows_user_sid():
    result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                            capture_output=True, text=True, check=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    return next(csv.reader(io.StringIO(result.stdout)))[1]


def restrict_new_file(path):
    if os.name == "nt":
        try:
            subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                            f"*{windows_user_sid()}:(F)"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError("Cannot restrict credential file ACL; nothing will be written") from None
    else:
        os.chmod(path, 0o600)


def read_private_file(path):
    # Refuse symlinks and non-private POSIX files, including imported key files.
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("Credential and key paths must be regular files, not symlinks")
    if os.name != "nt" and (info.st_mode & 0o077 or info.st_uid not in (os.getuid(), 0)):
        raise RuntimeError("Credential/key file permissions must be 0600 or stricter, owned by the collector user or root")
    if info.st_size > 2_000_000:
        raise RuntimeError("Credential file is unexpectedly large")
    return path.read_bytes()


class EncryptedFileBackend:
    def __init__(self, vault, keyfile):
        self.vault, self.keyfile = Path(vault), Path(keyfile)
        if self.vault.resolve() == self.keyfile.resolve():
            raise RuntimeError("Encrypted password file and key file must have different paths")

    def _cipher(self, create=False):
        if not self.keyfile.exists():
            if not create or self.vault.exists():
                raise RuntimeError("Encryption key is missing; restore the matching key, do not regenerate it")
            self.keyfile.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Exclusive creation prevents accidental overwrite of existing keys.
            descriptor = os.open(self.keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                restrict_new_file(self.keyfile)
                with os.fdopen(descriptor, "wb") as handle:
                    descriptor = None
                    handle.write(Fernet.generate_key())
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                # This key was created exclusively here and no vault was written.
                # Remove an incomplete key so a failed ACL setup can be retried.
                if descriptor is not None:
                    os.close(descriptor)
                    descriptor = None
                self.keyfile.unlink(missing_ok=True)
                raise
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        try:
            return Fernet(read_private_file(self.keyfile).strip())
        except ValueError:
            raise RuntimeError("Encryption key has an invalid format") from None

    def _load(self, create=False):
        if not self.vault.exists():
            if create:
                self._cipher(create=True)
            return {"version": 1, "services": {}}
        try:
            value = json.loads(self._cipher().decrypt(read_private_file(self.vault)))
            if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("services"), dict):
                raise ValueError()
            for accounts in value["services"].values():
                if not isinstance(accounts, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in accounts.items()):
                    raise ValueError()
            return value
        except (InvalidToken, ValueError, TypeError):
            raise RuntimeError("Cannot decrypt credential file: wrong key, damaged file or invalid content") from None

    def _save(self, value):
        ciphertext = self._cipher().encrypt(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        self.vault.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=".credentials-", dir=self.vault.parent)
        try:
            restrict_new_file(Path(temporary))
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(ciphertext)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.vault)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if os.path.exists(temporary):
                os.unlink(temporary)

    def get_password(self, service, name):
        return self._load()["services"].get(service, {}).get(name)

    def set_password(self, service, name, password):
        from monitor import collector_lock
        with collector_lock(str(self.vault) + ".write"):
            data = self._load(create=True)
            data["services"].setdefault(service, {})[name] = password
            self._save(data)

    def delete_password(self, service, name):
        from monitor import collector_lock
        with collector_lock(str(self.vault) + ".write"):
            data = self._load()
            if name not in data["services"].get(service, {}):
                raise RuntimeError("Credential not found")
            del data["services"][service][name]
            self._save(data)
