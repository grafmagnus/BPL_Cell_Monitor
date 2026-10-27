import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import dns.resolver

from monitor import (Poll, load_config, load_headends, open_database, poll_headend,
                     resolve_headend, ssh_command, store_cycle)

HOST = "002518047a06.eui"
HEADER = "ethmac | id | cell | age |\n---+---+---+---+\n"


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.csv = self.root / "devices.csv"
        self.csv.write_text(
            "identification;deviceUniqueIdAnchor;deviceWanIpAddress;unitType;deviceName\n"
            f"{HOST};00:25:18:04:7A:06;10.1.1.99;HE;Trafo Eins\n"
            "002518053bb8.eui;00:25:18:05:3B:B8;10.1.1.98;RP;Repeater\n", encoding="utf-8")
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"headends_csv": "devices.csv"}))
        self.cfg = load_config(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def test_csv_filter_and_defaults(self):
        self.assertEqual(len(self.cfg["headends"]), 1)
        he = self.cfg["headends"][0]
        self.assertEqual((he["name"], he["dns_name"], he["ssh_user"]), (HOST, HOST, "root"))
        self.assertEqual(self.cfg["jumphost"], "NMS3_Infra")
        self.assertEqual(self.cfg["dns_server"], "10.40.8.117")
        self.assertEqual(he["label"], "Trafo Eins")

    def test_csv_mismatch_duplicate_and_no_he(self):
        original = self.csv.read_text(encoding="utf-8")
        for text in [original.replace(HOST, "002518047a07.eui"),
                     original + original.splitlines()[1] + "\n",
                     original.replace(";HE;", ";RP;")]:
            self.csv.write_text(text, encoding="utf-8")
            with self.assertRaises(ValueError):
                load_headends(self.csv)

    def test_resolver_uses_only_explicit_server_and_absolute_name(self):
        with patch("dns.resolver.Resolver") as factory:
            resolver = factory.return_value
            resolver.resolve.return_value = [SimpleNamespace(address="10.113.92.38")]
            self.assertEqual(resolve_headend(HOST, self.cfg), "10.113.92.38")
            factory.assert_called_once_with(configure=False)
            self.assertEqual(resolver.nameservers, ["10.40.8.117"])
            resolver.resolve.assert_called_once_with(HOST + ".", "A", search=False, lifetime=5)

    def test_no_answer_ambiguous_answer_and_nxdomain(self):
        with patch("dns.resolver.Resolver") as factory:
            resolver = factory.return_value
            for addresses in [[], ["10.1.1.1", "10.1.1.2"]]:
                resolver.resolve.return_value = [SimpleNamespace(address=a) for a in addresses]
                with self.assertRaises(RuntimeError):
                    resolve_headend(HOST, self.cfg)
            resolver.resolve.side_effect = dns.resolver.NXDOMAIN()
            with self.assertRaisesRegex(RuntimeError, "NXDOMAIN"):
                resolve_headend(HOST, self.cfg)

    def test_ssh_routes_to_dns_ip_and_keeps_hostname_identity(self):
        command = ssh_command(HOST, self.cfg, "10.113.92.38", "root")
        self.assertEqual(command[command.index("-J")+1], "NMS3_Infra")
        self.assertEqual(command[command.index("-l")+1], "root")
        self.assertIn("HostName=10.113.92.38", command)
        self.assertIn(f"HostKeyAlias={HOST}", command)
        self.assertEqual(command[-2:], [HOST, "ppcdinfo unittable"])
        self.cfg["jumphost"] = "alternate_jump"
        self.assertIn("alternate_jump", ssh_command(HOST, self.cfg))

    def test_dns_failure_never_uses_csv_ip_or_calls_ssh(self):
        self.cfg.update(retries=0)
        with patch("monitor.resolve_headend", side_effect=RuntimeError("DNS timeout")), patch("monitor.ssh_output") as ssh:
            result = poll_headend(self.cfg["headends"][0], self.cfg)
        ssh.assert_not_called()
        self.assertEqual(result.status, "error")
        self.assertIsNone(result.resolved_ip)

    def test_poll_requeries_dns_on_retry_and_records_ip(self):
        self.cfg.update(retries=1, retry_delay_seconds=0)
        with patch("monitor.resolve_headend", side_effect=["10.1.1.1", "10.1.1.2"]) as resolve, patch(
                "monitor.ssh_output", side_effect=[RuntimeError("SSH failed"), HEADER]) as ssh:
            result = poll_headend(self.cfg["headends"][0], self.cfg)
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual((result.status, result.resolved_ip, result.dns_name), ("ok", "10.1.1.2", HOST))
        self.assertEqual(ssh.call_args.args, (HOST, self.cfg, "10.1.1.2", "root"))
        db = open_database(self.root / "new.db")
        try:
            store_cycle(db, [result], self.cfg)
            self.assertEqual(db.execute("SELECT dns_name,resolved_ip FROM polls").fetchone(), (HOST, "10.1.1.2"))
        finally:
            db.close()

    def test_v1_database_migrates_without_losing_poll(self):
        path = self.root / "old.db"
        db = sqlite3.connect(path)
        db.executescript("""CREATE TABLE polls(cycle_id INTEGER NOT NULL,headend TEXT NOT NULL,
            status TEXT NOT NULL,observed_at REAL NOT NULL,attempts INTEGER NOT NULL,error TEXT,
            row_count INTEGER NOT NULL,duplicate_count INTEGER NOT NULL,PRIMARY KEY(cycle_id,headend));
            INSERT INTO polls VALUES(1,'old','error',10,1,'old failure',0,0);
            PRAGMA user_version=1;""")
        db.close()
        db = open_database(path)
        try:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT headend,error,dns_name,resolved_ip FROM polls").fetchone(),
                             ("old", "old failure", None, None))
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
