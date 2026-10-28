# BPL Headend Monitor

Python 3.11 oder neuer und der OpenSSH-Client. Der Collector verwendet `dnspython` sowie für gespeicherte Passwörter `keyring` und `paramiko` (`requirements-collector.txt`). Streamlit, Plotly und Pandas werden nur für das Dashboard benötigt. `requirements.txt` installiert alles zusammen. Es werden keine Geräte verändert; abgefragt wird ausschließlich `ppcdinfo unittable`.

## Headend-Passwort geschützt speichern

Das neue Konfigurationsbeispiel enthält:

```json
"headend_auth": "keyring",
"credential_service": "bpl-monitor/headends",
"ssh_user": "root"
```

Diese Werte auch in eine bereits vorhandene `config.json` übernehmen. Ohne `headend_auth` bleibt aus Kompatibilitätsgründen die bisherige Schlüsselanmeldung aktiv (`"headend_auth": "key"`). Keine Felder `password` oder `headend_password` in die Konfiguration eintragen; diese werden abgelehnt.

Nach Installation der Pakete einmalig **im interaktiven Terminal unter demselben Betriebssystemkonto wie der Collector** ausführen:

```text
python -m pip install -r requirements-collector.txt
python credentials.py set --config config.json
```

Das Passwort wird zweimal verdeckt abgefragt. Der Eintrag `root@*` gilt für alle Headends. Ein abweichendes Passwort nur für ein bestimmtes Gerät:

```text
python credentials.py set --config config.json --headend 00251805c198.eui
```

Der spezifische Eintrag `root@00251805c198.eui` hat Vorrang vor dem gemeinsamen Passwort. `--username` erlaubt einen anderen Kontonamen, der auch mit dem verwendeten `ssh_user` übereinstimmen muss. Ein gesetztes gerätespezifisches Passwort wird bei einem Anmeldefehler nicht durch einen Versuch mit dem gemeinsamen Passwort ersetzt.

Prüfen, ohne das Passwort anzuzeigen:

```text
python credentials.py status --config config.json
python credentials.py status --config config.json --headend 00251805c198.eui
```

Ändern: denselben `set`-Befehl erneut ausführen und den Collector neu starten. Entfernen: `python credentials.py delete --config config.json`, gegebenenfalls mit `--headend`. `status` prüft den exakt bezeichneten Eintrag, nicht dessen Fallback. Der laufende Collector hält geladene Passwörter im Arbeitsspeicher; Änderungen am Speicher werden nach Neustart geladen.

Unterstützt sind native Windows-Anmeldeinformationen, macOS-Schlüsselbund und die Linux-Backends Secret Service/KWallet. Drittanbieter-, Datei-Klartext- und leere Fallback-Backends werden abgelehnt. Das Passwort wird weder in JSON, SQLite, CSV noch in Kommandozeilen oder Umgebungsvariablen abgelegt. Die konkrete Verschlüsselung und Zugriffssteuerung übernimmt der Betriebssystemspeicher. Unter Linux muss der verwendete Schlüsselbund entsprechend geschützt eingerichtet und entsperrt sein. Ein gesperrter oder fehlender Speicher führt zu einer Fehlermeldung, nicht zu einer unsicheren Ersatzdatei. Prozesse mit denselben Kontorechten können je nach System und Schlüsselbundberechtigungen ebenfalls Zugriff erhalten.

**Dauerbetrieb:** Windows-Aufgabenplanung und Collector müssen unter dem Konto laufen, in dessen Anmeldeinformationsspeicher das Passwort abgelegt wurde. Unter Linux benötigt ein Secret-Service-/KWallet-Speicher eine erreichbare Benutzersitzung, häufig einschließlich D-Bus und entsperrtem Schlüsselbund. Ein reiner systemd-Systemdienst unter `bplmon` hat diese Voraussetzungen nicht automatisch. Das Dienstbeispiel daher nur nutzen, wenn der native Speicher im Dienstkontext eingerichtet und erreichbar ist; andernfalls weiter Schlüsselanmeldung verwenden oder zunächst einen passenden Betriebssystemspeicher bereitstellen. Das Passwort wird nicht mit dem ZIP ausgeliefert und muss auf jedem Zielsystem unter dem richtigen Benutzer eingerichtet werden.

**SSH-Verbindung:** In `keyring`-Betriebsart authentifiziert Paramiko ausschließlich den Headend per Passwort. Zum Jumphost öffnet der native OpenSSH-Client den Tunnel mit den bisherigen SSH-Konfigurations- und Schlüssel-/Agent-Einstellungen. Ein zusätzliches Jumphost-Passwort wird damit nicht gespeichert. Zielport, HostKeyAlias und bekannte Hostschlüssel werden aus `ssh -G` und den konfigurierten `known_hosts`-Dateien übernommen. Unbekannte oder geänderte Hostschlüssel werden abgelehnt. Eigene ProxyCommand-Skripte, Hostzertifikate und spezielle OpenSSH-Authentifizierungs-/Algorithmusvorgaben werden vom Passworttransport nicht vollständig nachgebildet; vorgesehen sind der normale ProxyJump-Pfad und bekannte Standard-Hostschlüssel. SSH-Kommandos und Fehlertexte im Passwortpfad enthalten keine Bibliotheks-Fehlerdetails, die versehentlich Geheimnisse offenlegen könnten.

Einzeltest nach der Einrichtung:

```text
python monitor.py --config config.json --headend 00251805c198.eui --once
```

Referenzen: [native Speicher und API von keyring](https://keyring.readthedocs.io/en/latest/) sowie [Paramiko-Passwortanmeldung und Hostschlüsselprüfung](https://docs.paramiko.org/en/stable/api/client.html).

## Headends aus CSV, DNS und Jumphost

`headends.csv` ist eine unveränderte Kopie von `6ae1104c53f2a32580d02f24.csv`. Von 3.663 Geräten werden ausschließlich die **126 Zeilen mit `unitType=HE`** abgefragt; 3.534 RP, zwei LEAF und eine Zeile ohne Typ werden ausgelassen. Der genannte Beispielhost `002518053bb8.eui` ist in dieser Datei ein **RP**, kein HE.

Die Konfiguration verwendet folgende Vorgaben:

```json
{
  "headends_csv": "headends.csv",
  "jumphost": "NMS3_Infra",
  "ssh_user": "root",
  "dns_server": "10.40.8.117",
  "dns_port": 53,
  "dns_timeout_seconds": 5
}
```

Alle Parameter stehen in `config.example.json`. Die CSV wird beim Programmstart geladen. `identification` muss der MAC aus `deviceUniqueIdAnchor` entsprechen: etwa `002518047a06.eui` zu `00:25:18:04:7a:06`. Headends werden unter diesem stabilen Hostnamen gespeichert, unabhängig von IP- oder Gerätenamenänderungen. Widersprüchliche oder doppelte HE-Einträge verhindern den Start.

Vor jedem SSH-Versuch wird der absolute `.eui.`-Name als IPv4-A-Record ausschließlich über den eingestellten DNS-Server abgefragt. Es gibt keinen Rückgriff auf den Systemresolver oder `deviceWanIpAddress`. Die CSV-IP ist nur eine Inventarreferenz. Ohne Antwort, bei Timeout oder bei mehreren unterschiedlichen A-Records gilt die Abfrage als fehlgeschlagen und wird entsprechend der Retry-Konfiguration wiederholt. DNS wird auch beim Retry erneut abgefragt. Der DNS-Server muss vom Collector-Rechner erreichbar sein, gegebenenfalls über VPN; DNS-Abfragen laufen direkt vom Collector, SSH läuft über den Jumphost.

OpenSSH erhält `-J NMS3_Infra`, `-l root`, `-o HostName=<DNS-IP>` und `-o HostKeyAlias=<hostname.eui>`. So bleibt der Hostname für `~/.ssh/config` und `known_hosts` stabil, während die Verbindung über den Jumphost zur aktuell ermittelten IP geht. Der Jump-Alias wird weiterhin aus der vorhandenen SSH-Konfiguration gelesen. Hostschlüssel müssen für die `.eui`-Namen geprüft und hinterlegt sein; bereits nur unter einer IP hinterlegte Schlüssel reichen bei dieser Namensbindung nicht aus. `jumphost: null` lässt die ProxyJump-Einstellung aus der SSH-Konfiguration gelten.

`polls.dns_name` und `polls.resolved_ip` dokumentieren Name und tatsächlich verwendete IP je Abfrage und sind im Headend-Status des Dashboards sichtbar. Bestehende Datenbanken werden beim Collectorstart automatisch von Schema 1 auf Schema 2 ergänzt; alte Daten bleiben erhalten. Die neuen `.eui`-Headendnamen ersetzen keine früher manuell gewählten Namen in alten Messungen. Ein solcher Wechsel der Benennung kann in Vergleichen als Headendwechsel erscheinen.

Das mitgelieferte Beispiel nutzt vier parallele Abfragen, mindestens eine Sekunde zwischen SSH-Verbindungsstarts und 25 Sekunden SSH-Zeitlimit. Dies ersetzt die frühere, für einen einzelnen Jumphost zu aggressive Vorgabe von 32 Verbindungen. Jeder SSH-Retry durchläuft dieselbe Startbegrenzung. Retrypausen steigen exponentiell von der konfigurierten Basis bis 30 Sekunden plus bis zu einer Sekunde Zufallsversatz. Bei vielen langsamen/ausgefallenen Headends kann ein Durchlauf damit länger als fünf Minuten dauern; Überläufe werden protokolliert und Startplätze übersprungen. Das Intervall bleibt ein Zielabstand, keine garantierte Maximaldauer bei Ausfällen.

### SSH-Abbruch vor der Anmeldung (`kex_exchange_identification`)

Ein Abbruch mit `Connection closed by remote host` beweist weder einen falschen Schlüssel noch eine bestimmte Servereinstellung. Die vorherigen 32 parallelen Verbindungen können ein Verbindungs-/Anmeldelimit des Jumphosts ausgelöst haben; auch eine falsche Jump-Adresse, Serverrichtlinien oder ein Netzwerkgerät kommen infrage. OpenSSH kann zusätzliche Verbindungen über [MaxStartups](https://man.openbsd.org/sshd_config#MaxStartups) bereits vor der Anmeldung ablehnen.

Bei bestehender Installation die folgenden Werte in der eigenen `config.json` ändern; das Ersetzen von `config.example.json` allein ändert keine vorhandene Konfiguration:

```json
"workers": 4,
"ssh_start_interval_seconds": 1,
"retry_delay_seconds": 5
```

Den laufenden Collector beenden und zunächst ein Headend mit dem aktualisierten Skript einzeln prüfen:

```text
python monitor.py --config config.json --headend 00251805c198.eui --once
```

Der Einzeltest verwendet eine getrennte Datenbank `bpl.diagnostic.sqlite3` im selben Verzeichnis wie die reguläre Datenbank. So verändert eine eingeschränkte Testabfrage keine Zustände der gesamten Flotte. Ohne `--headend` wird wieder die normale Datenbank verwendet.

Die effektive Jumphost-Konfiguration lässt sich ohne SSH-Verbindung anzeigen:

```text
ssh -G NMS3_Infra
```

Im Ergebnis insbesondere `hostname`, `user`, `port` und etwaige weitere Proxy-Einstellungen prüfen. `10.40.8.117` ist als DNS-Server vorgegeben; das macht diese IP nicht automatisch zum richtigen SSH-Jumphost. Sie kann aber absichtlich beide Aufgaben erfüllen. Ein einzelner manueller Versuch mit `ssh -vvv NMS3_Infra` zeigt, ob bereits die Verbindung zum Jumphost scheitert. Wenn der Einzeltest funktioniert und der Flottenlauf scheitert, die Parallelität weiter auf eins reduzieren und die SSH-Serverlogs auf abgewiesene Verbindungsstarts prüfen.

Alternativ bleibt eine manuelle `headends`-Liste möglich, sofern `headends_csv` entfernt wird. Einträge mit `dns_name` nutzen den konfigurierten DNS-Server; ohne `dns_name` bleibt die Auflösung beim OpenSSH-Alias. CSV-Liste und manuelle Liste dürfen nicht gleichzeitig gesetzt sein.

## Enthaltenes Geräteinventar

`coordinates.csv` enthält bereits die Daten aus `6ae0d6ea53f2a32580d02f21.csv`: **3.663 eindeutige MACs**, davon **3.659 mit Koordinaten** und **vier ohne Koordinaten**. Vier Gerätenamen sind leer; dort wird die MAC angezeigt. Namen und Koordinaten werden über `deviceUniqueIdAnchor` der MAC im Monitoring zugeordnet. `deviceName` wird als `label` übernommen, `coordinates` als `[Breitengrad, Längengrad]` in WGS84 gelesen. Groß-/Kleinschreibung der MAC spielt keine Rolle. Die Originaldatei bleibt unverändert. `inventory_issues.csv` listet die MACs mit fehlenden Koordinaten oder fehlendem Namen auf.

Die mitgelieferte `coordinates.csv` beim Einrichten beibehalten. Die separate `coordinates.example.csv` enthält weiterhin nur drei fiktive Beispielpositionen. Gerätenamen erscheinen in Karten-Hover, Geräteauswahl, Statistiken, Status-Heatmap und CSV-Exporten. Die Karte und Inventarliste stehen auch vor der ersten Messung zur Verfügung. MACs ohne Position bleiben in Listen und Auswertungen erhalten. Mehrere Geräte können dieselbe Position besitzen und auf der Karte übereinander liegen.

Das Dashboard liest sowohl das normalisierte Format `mac,latitude,longitude,label` als auch den ursprünglichen Semikolon-Export mit `deviceUniqueIdAnchor;coordinates;deviceName`. Dazu in `config.json` unter `coordinates` die jeweilige Datei eintragen. Für spätere Exporte kann alternativ eine neue normalisierte Datei erzeugt werden:

```text
python import_inventory.py neuer-export.csv coordinates-neu.csv
```

Anschließend `coordinates` in der Konfiguration auf `coordinates-neu.csv` setzen und das Dashboard neu starten. Vorhandene Zieldateien werden vom Importwerkzeug nicht überschrieben. Unvollständige/ungültige Koordinaten oder doppelte MACs werden abgelehnt; ein vollständig leeres Koordinatenpaar ist erlaubt. Namen und Positionen entsprechen immer dem aktuellen Inventar, auch bei Ansicht historischer Messungen; Inventaränderungen werden nicht historisiert.

## Schnellstart

Im entpackten Verzeichnis:

```text
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item config.example.json config.json
```

Linux/macOS:

```sh
. .venv/bin/activate
python -m pip install -r requirements.txt
cp config.example.json config.json
```

1. Den bestehenden Jump-Alias `NMS3_Infra` in `~/.ssh/config` verwenden. Falls nötig, `ssh_config.example` mit echten Zugangsdaten ergänzen; die Beispieldatei ersetzt keine bestehende Konfiguration. Unter Windows liegt die Datei normalerweise in `%USERPROFILE%\.ssh\config`.
2. In `config.json` Jumphost und DNS prüfen. `headends_csv` verweist bereits auf die mitgelieferte CSV; die 126 HE werden automatisch ausgewählt. Auswahl ohne Netzverbindung prüfen: `python monitor.py --config config.json --list-headends`.
3. Schlüssel-/Agentzugriff auf den Jump-Host und geprüfte Hostschlüssel für Jump-Host und Headends unter demselben Benutzer einrichten, der den Collector ausführt. Das Headend-Passwort wie oben beschrieben einmalig mit `credentials.py set` hinterlegen. Bei `headend_auth=key` stattdessen auch den Schlüsselzugriff auf die Headends einrichten. Falls ein Agent benötigt wird, muss er auch im Dienstkontext verfügbar sein.
4. Die enthaltene `coordinates.csv` mit den echten Inventardaten verwenden. Für weitere Geräte kann sie ergänzt oder durch einen neuen Export ersetzt werden. Spalten im normalisierten Format: `mac,latitude,longitude,label`; `label` ist optional.
5. Den DNS-Server vom Collector-Rechner aus erreichbar machen und einen Durchlauf prüfen:

```text
python monitor.py --config config.json --once
```

Das entspricht dem gelieferten SSH-Aufruf mit `-J`, jedoch mit frisch ermittelter DNS-IP und stabiler `.eui`-Hostidentität. OpenSSH liest die SSH-Konfiguration selbst; das Skript implementiert keinen eigenen SSH-Konfigurationsparser.

Collector dauerhaft starten:

```text
python monitor.py --config config.json
```

In einem zweiten Terminal mit derselben virtuellen Umgebung:

```text
python -m streamlit run dashboard.py --server.address 127.0.0.1 -- --config config.json
```

Die lokale Oberfläche ist dann unter `http://127.0.0.1:8501` erreichbar. Sie liest nur die Datenbank und startet keine SSH-Abfragen. Standardmäßig bleibt sie auf dem neuesten Durchlauf und aktualisiert sich alle 60 Sekunden, solange die Seite geöffnet ist. „Follow latest round“ abwählen, um einen älteren Kartenzeitpunkt auszuwählen. Der Collector läuft unabhängig von geöffneten Browsern.

## Demo ohne Netz und Headends

```text
python demo.py --directory demo-data --days 3
python -m streamlit run dashboard.py --server.address 127.0.0.1 -- --config demo-data/config.json
```

Die Demo erzeugt 864 Durchläufe für drei Geräte mit Zell- und Headendwechseln, Duplikaten, Konflikten, alten Beobachtungen und Abfrageausfällen. Sie öffnet keine SSH-Verbindung und überschreibt keine vorhandene Demo-Datenbank/Konfiguration. Ein anderes Zielverzeichnis erlaubt eine neue Demo. Demo-Konfiguration niemals zum produktiven Collectorstart verwenden.

## Konfiguration

Alle relativen Datenpfade werden relativ zur JSON-Konfiguration aufgelöst, nicht zum aktuellen Arbeitsverzeichnis. Konfigurationsänderungen werden nach Collector-Neustart wirksam.

| Feld | Standard | Bedeutung |
|---|---:|---|
| `interval_seconds` | 300 | Startabstand der Durchläufe; erster Durchlauf sofort |
| `retention_days` | 56 | Acht Wochen Messhistorie, rollierende Bereinigung nach jedem Durchlauf |
| `database` | `data/bpl.sqlite3` | Lokale SQLite-Datei, kein Netzlaufwerk |
| `coordinates` | `coordinates.csv` | Inventarpositionen; Dashboard liest sie bei jeder Aktualisierung neu |
| `timeout_seconds` | 35 | Gesamtlaufzeitlimit je SSH-Versuch inklusive Jump-Host |
| `connect_timeout_seconds` | 10 | Zusätzliches Verbindungszeitlimit für das Ziel |
| `retries` | 1 | Weitere Versuche nach einem Fehler, maximal 5 |
| `retry_delay_seconds` | 2; Beispiel 5 | Basis für exponentielle Retrypause mit Zufallsversatz |
| `workers` | 4 | Parallel abgefragte Headends, maximal 64 |
| `ssh_start_interval_seconds` | 1 | Mindestabstand der SSH-Starts je Durchlauf, einschließlich Retries; 0 deaktiviert die Staffelung |
| `max_age` | `null` | Optionaler maximaler Rohwert der Spalte `age` |
| `headends` | alternativ | Manuelle Liste aus stabilem `name` und `ssh_host`; nicht zusammen mit `headends_csv` |
| `headends_csv` | im Beispiel `headends.csv` | Semikolon-Export; ausschließlich HE-Zeilen, alternativ zur manuellen Liste |
| `jumphost` | `NMS3_Infra` | SSH-Jump-Alias; `null` verwendet die SSH-Konfiguration |
| `ssh_user` | `root` | Zielbenutzer für die HE aus der CSV |
| `headend_auth` | `key`; Beispiel `keyring` | OpenSSH-Schlüsselanmeldung oder Passwort aus dem nativen Betriebssystemspeicher |
| `credential_service` | `bpl-monitor/headends` | Namensraum im Betriebssystemspeicher, kein Passwort |
| `dns_server` | `10.40.8.117` | Ausschließlich verwendeter DNS-Server bei DNS-Zielen |
| `dns_port` | 53 | DNS-Port |
| `dns_timeout_seconds` | 5 | DNS-Gesamtzeitlimit je Versuch, zusätzlich zum SSH-Zeitlimit |

Die Einheit und Bedeutung von `age` sind aus dem Beispiel nicht zuverlässig ableitbar. Deshalb wird der Wert unverändert gespeichert; standardmäßig werden keine Zeilen wegen `age` ausgeschlossen. Einen Grenzwert erst nach Prüfung der Firmware-Semantik setzen. Ein kleinerer Wert wird bei Duplikaten als frischere Beobachtung behandelt, sofern diese Filterung aktiviert ist.

## Datenmodell und Konfliktregeln

- **Geräteidentität ist die normalisierte MAC.** IDs werden niemals global als Geräteschlüssel verwendet. Adressbelegungen werden im Namensraum `(headend, cell, unit_id)` geprüft, weil Zellnummern verschiedener Headends ebenfalls wiederverwendet werden können. Die gleiche ID in verschiedenen Zellen ist erlaubt.
- `cycles` enthält Start/Ende in UTC als Unix-Sekunden und das zum Messzeitpunkt gültige Intervall. `polls` enthält pro Headend Erfolg/Fehler, Erfassungszeitpunkt, Versuchszahl, Fehlertext sowie Zeilen- und Duplikatzähler. Der Durchlaufzeitpunkt gruppiert die Abfragen; die einzelnen SSH-Abfragen sind nicht gleichzeitig.
- `observations` enthält alle unterschiedlichen `(Durchlauf, Headend, MAC, Zelle, ID)`-Kandidaten. Exakte Mehrfachzeilen werden zusammengefasst: `age` ist Minimum, `age_max` Maximum, `duplicates` die Anzahl zusätzlicher Zeilen. Rohtext und die einzelnen Zwischenwerte der Duplikate werden nicht archiviert.
- `states` enthält pro bereits bekanntem Gerät und Durchlauf einen abgeleiteten Status. Ein Gerät wird beim ersten erfolgreichen Auftreten bekannt. Inventar-MACs, die noch nie in einer Tabelle standen, werden auf der Karte als `never_observed` angezeigt; es wird keine Vorgeschichte erfunden.
- **observed:** Genau ein frischer Zuordnungskandidat. Bei Ausfall anderer Headends bleibt `coverage_complete=0`; ein dort möglicher weiterer Kandidat ist unbekannt.
- **conflict:** Mehrere frische Zuordnungen für dieselbe MAC oder verschiedene MACs mit derselben `(Headend, Zelle, ID)`-Belegung. Auch eine Kollision unter alten Einträgen wird vorsorglich als Konflikt markiert. Es wird kein Gewinner geraten; die Zuordnungsfelder im abgeleiteten Zustand bleiben leer. Alle Kandidaten bleiben abrufbar.
- **stale:** Es gibt Tabellenzeilen, aber alle überschreiten den konfigurierten Altersgrenzwert.
- **missing:** Das bekannte Gerät fehlt in sämtlichen Tabellen eines vollständig erfolgreichen Durchlaufs. Das beweist keine physische Nichterreichbarkeit des Geräts.
- **unknown:** Das Gerät fehlt in den verfügbaren Tabellen und mindestens ein Headend konnte nicht abgefragt werden. Die letzte bekannte Zuordnung wird nicht als aktuelle Zuordnung fortgeschrieben.

Ein SSH-Fehler, Timeout, ungültiges UTF-8 oder unerwartetes Tabellenformat gilt als Abfragefehler. Der Parser erwartet genau den Header `ethmac | id | cell | age`, eine Trennlinie und anschließend vollständige Datenzeilen. Eine leere Tabelle mit Header und Trennlinie ist gültig. Unbekannte Footer/Zusatzspalten werden bewusst abgelehnt. Bei anderer Firmware-Ausgabe zunächst den Parser anhand echter Ausgaben erweitern. Ein Transportabbruch mit nicht erfolgreichem SSH-Exitcode wird nicht als leere Tabelle behandelt. Eine Ausgabe, die trotz Exitcode 0 genau an einer Zeilengrenze abgeschnitten wurde, lässt sich ohne Geräte-seitigen Datensatz-Zähler nicht sicher erkennen.

Headendnamen, überwachter Headendumfang und Filter sollten für vergleichbare Zeiträume stabil bleiben. Wird ein Headend entfernt, bedeutet `missing` anschließend nur noch „in der jetzt konfigurierten Menge nicht gesehen“. Die Poll-Tabelle dokumentiert pro Durchlauf die abgefragten Headends. Umkonfiguration oder geänderte MAC-Adressen sind keine automatisch erkannten physischen Gerätewechsel.

## Auswertung

- **Karte:** Exakter Durchlauf statt zusammengewürfelter „letzter Sichtungen“. Farben nach Zuordnung oder Status; Angaben zu unvollständiger Abdeckung im Hover. Eine feste MAC-Position zeigt wechselnde logische Zuordnung, keine Bewegung des Geräts. OpenStreetMap-Kacheln benötigen Internet; Koordinaten und Messwerte werden aus lokalen Dateien gelesen. Der Browser kontaktiert den Kachelanbieter.
- **Zeitverlauf:** Ein Punkt pro Messung, einschließlich Konflikten und unbekannten Zuständen; keine Verbindung über Messlücken. Detailtabelle mit allen Kandidaten, `age` und Duplikaten.
- **Zell-/Headendwechsel:** Zählung nur zwischen aufeinanderfolgenden, vollständig abgedeckten, eindeutigen Messungen mit Abstand von höchstens 1,5 Abfrageintervallen. Ein Konflikt, Ausfall, fehlender Zustand oder längerer Abstand unterbricht die Vergleichskette. Änderungen über solche Lücken werden bewusst nicht gezählt. ID-Wechsel werden separat gezählt.
- **Heatmaps:** Gerätemessungen pro Headend/Zelle und UTC-Stunde, ausschließlich bei vollständiger Abdeckung; zusätzlich Status-Matrix für bis zu 100 ausgewählte MACs.
- **Statistik:** Erste/letzte Messung, erste/letzte eindeutige Sichtung, Statuszähler, Abdeckungszähler, verschiedene Headends/Zuordnungen und Wechselanzahlen. Prozentwerte beziehen sich auf Messpunkte im gewählten Fenster ab Erstbekanntwerden, nicht auf echte Uptime oder Verweildauer. Wechselzeitpunkte sind durch das Fünfminutenraster begrenzt.
- **Export:** Gerätestatistik und abgeleitete Zustände als CSV; Kandidaten und Poll-Fehler bleiben zusätzlich in SQLite abfragbar.

## Betrieb, Ausfälle und Aufbewahrung

Ein OS-Dateilock verhindert zwei Collectorprozesse für denselben aufgelösten Datenbankpfad. Die Lockdatei darf nach einem Absturz stehen bleiben; der Lock wird vom Betriebssystem freigegeben. Unterschiedliche Datenbankpfade oder Verzeichnis-Aliase dürfen nicht für dieselbe Datei verwendet werden.

Headends werden parallel abgefragt, SQLite-Schreibzugriffe erfolgen anschließend in einem Thread. Ein kompletter Durchlauf wird in einer Transaktion gespeichert. Bei einem Prozessabbruch vor dem Commit fehlt dieser Durchlauf vollständig; der nächste Start erfindet keine Nachmessungen. WAL erlaubt paralleles Lesen durch das Dashboard. Bei zu langsamen Durchläufen werden verpasste Startplätze übersprungen; Durchläufe überlappen sich nicht. Die Konsole protokolliert Fehler und übersprungene Slots. `--once` liefert Exitcode 0 bei vollständigem Erfolg und 2 bei mindestens einem Headendfehler; Konfigurations-/Datenbankfehler liefern einen Fehlerexit. Im Dauerbetrieb laufen einzelne Pollfehler nicht auf einen Prozessabbruch hinaus.

Für Linux ist `bpl-monitor.service.example` enthalten: Pfade und Benutzer anpassen, unter diesem Benutzer SSH einrichten, Datenverzeichnis beschreibbar machen und die angepasste Datei als systemd-Dienst installieren. `Restart=on-failure` startet bei unerwarteten Prozessfehlern neu. Logs sind über `journalctl -u bpl-monitor` sichtbar. Auf Windows kann die Aufgabenplanung den Dauerprozess bei Systemstart starten; vollständigen Pythonpfad, absoluten Konfigurationspfad, Arbeitsverzeichnis und dasselbe SSH-Benutzerkonto angeben. „Keine neue Instanz starten“ wählen. Alternativ darf die Aufgabenplanung `--once` alle fünf Minuten aufrufen; dann keinen zweiten Dauerprozess starten.

Messdaten einschließlich Zuständen, Kandidaten und Pollfehlern werden nach `retention_days` gelöscht. Die kleine MAC-Liste `devices` bleibt erhalten, damit später fehlende bekannte Geräte weiter als solche erfasst werden. SQLite gibt gelöschte Seiten intern zur Wiederverwendung frei; die Datei schrumpft dadurch nicht automatisch. Für eine zusätzliche physische Verkleinerung den Collector und das Dashboard stoppen und bei Bedarf `VACUUM` ausführen.

Für Backups während des Betriebs die SQLite-Backup-API verwenden, nicht ausschließlich die `.sqlite3`-Datei kopieren, während noch WAL-Schreibvorgänge laufen. Beispiel aus dem Projektverzeichnis (Backupziel muss neu oder zum Überschreiben bestimmt sein):

```text
python -c "import sqlite3; s=sqlite3.connect('data/bpl.sqlite3'); d=sqlite3.connect('data/bpl-backup.sqlite3'); s.backup(d); d.close(); s.close()"
```

Speicherbedarf grob vorab prüfen: pro Woche entstehen je Gerät 2.016 Zustände und typischerweise ebenso viele Kandidaten. Für 1.000 Geräte und acht Wochen sind das etwa 16,1 Millionen Zeilen je Tabelle, zusätzlich zu Indizes. Das Dashboard lädt das ausgewählte Zeitfenster in den Speicher. Bei vielen Geräten zunächst ein bis zwei Tage wählen; größere Bestände benötigen serverseitige Aggregation/Paginierung als spätere Erweiterung. SQLite-Daten auf einer lokalen Platte halten. Für längere Aufbewahrung `retention_days` erhöhen und Backups einrichten.

## Tests

Parser, Speicherung und SSH-Retry mit synthetischen Antworten testen:

```text
python -m unittest -v test_monitor.py
```

Nach Installation der Dashboardpakete:

```text
python -m unittest -v test_monitor.py test_analytics.py test_dashboard.py test_discovery.py test_password_auth.py
```

Validiert mit Python 3.11, Streamlit 1.66.1, Plotly 6.9.0 und Pandas 2.3.3 sowie Paramiko 4.0.0 und keyring 25.7.0: 53 Tests erfolgreich. `requirements-tested.txt` hält zusätzlich die vollständigen Paketversionen dieses Testlaufs fest; für die gleiche Umgebung kann es statt `requirements.txt` installiert werden. Die Streamlit-Tests prüfen die Ausführung und Widgets; ein browserseitiger Kachelabruf ist darin nicht enthalten.

Die Tests verwenden temporäre Datenbanken und synthetische SSH-Ergebnisse. DNS und entfernte Headends werden simuliert; die Passwortanmeldung wird zusätzlich mit einem echten lokalen SSH-Handshake über ein Socketpaar und einen generierten Hostschlüssel geprüft. Der echte Betriebssystem-Passwortspeicher wird in den Tests nicht beschrieben; ein echter Netztest ist mit `--once` in der Zielumgebung nötig. Es werden keine Passwörter in der JSON-Datei gespeichert.

## Referenzen

Die SSH-Weiterleitung folgt der [OpenSSH-Konfiguration einschließlich ProxyJump](https://man.openbsd.org/ssh_config). Das Dashboard verwendet [Streamlit-Fragmente mit periodischer Aktualisierung](https://docs.streamlit.io/develop/api-reference/execution-flow/st.fragment) und [Plotly-Karten mit `scatter_map`](https://plotly.com/python/tile-scatter-maps/).

Die explizite DNS-Auflösung nutzt den [dnspython-Resolver mit konfiguriertem Nameserver und Zeitlimit](https://dnspython.readthedocs.io/en/stable/resolver-class.html). SSH verwendet die [OpenSSH-Optionen für Jump-Host und Zielparameter](https://man.openbsd.org/ssh).
