"""Set/delete/check protected credentials. Passwords are accepted only at a hidden prompt."""
import argparse
import getpass
import sys
import warnings

from monitor import load_config
from password_auth import account, get_password, _file_password, secure_backend


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["set", "delete", "status"])
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--username", help="Default: ssh_user from config")
    parser.add_argument("--headend", help="Omit for shared password; otherwise a configured SSH hostname")
    args = parser.parse_args()
    cfg = load_config(args.config)
    username = args.username or cfg["ssh_user"]
    hostname = args.headend.lower() if args.headend else None
    if hostname and hostname not in {h["ssh_host"].lower() for h in cfg["headends"]}:
        parser.error("Unknown headend hostname")
    storage = cfg.get("credential_storage", "native")
    if storage == "encrypted-file":
        from encrypted_store import EncryptedFileBackend
        backend = EncryptedFileBackend(cfg["credential_file"], cfg["credential_key_file"])
    else:
        backend = secure_backend()
    service, name = cfg["credential_service"], account(username, hostname)
    if args.action == "set":
        if not sys.stdin.isatty():
            raise RuntimeError("Use an interactive terminal for hidden password input")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Headend password: ")
            confirmation = getpass.getpass("Repeat password: ")
        if not password or password != confirmation:
            raise RuntimeError("Passwords are empty or do not match; nothing stored")
        try:
            backend.set_password(service, name, password)
        except RuntimeError:
            if storage == "encrypted-file":
                raise
            raise RuntimeError("Native credential store could not save the password") from None
        except Exception:
            raise RuntimeError("Credential store could not save the password; check file paths and permissions") from None
        get_password.cache_clear()
        _file_password.cache_clear()
        print(f"Stored in {storage} credential store: {service} / {name}")
    else:
        try:
            if args.action == "delete":
                backend.delete_password(service, name)
                get_password.cache_clear()
                _file_password.cache_clear()
                print("Credential deleted")
            else:
                present = backend.get_password(service, name) is not None
                print(f"{service} / {name}: {'present' if present else 'not set'}")
        except RuntimeError:
            if storage == "encrypted-file":
                raise
            raise RuntimeError("Native credential operation failed") from None
        except Exception:
            raise RuntimeError("Credential operation failed; check storage paths and permissions") from None
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, getpass.GetPassWarning) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
