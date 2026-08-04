# GOG Repo Downloader - Konzept & Architektur

Stand: 2026-08-04 · Status: Entwurf zur Abstimmung

---

## 0. Scope-Korrektur vorweg

GOG-Spiele sind **DRM-frei**, nicht copyright-frei. Das Tool lädt ausschließlich die
**eigene, gekaufte Bibliothek** des angemeldeten Kontos herunter. Daraus folgt direkt eine
Architekturentscheidung: **Es gibt keinen anonymen Modus und keinen Katalog-Scrape** -
Authentifizierung ist Pflichtvoraussetzung für jede Operation außer `--help`.

---

## 1. Zielbild

Ein CLI-Tool mit vier Verben:

| Verb | Aufgabe |
|------|---------|
| `login` | Einmalige Authentifizierung, persistiert Refresh-Token |
| `update` | Bibliothek + Datei-Metadaten von GOG holen → lokales Manifest aktualisieren |
| `download` | Fehlende/veraltete Dateien laden, resume-fähig, mit Live-Fortschritt |
| `verify` | Lokalen Bestand gegen Manifest prüfen (Größe, MD5, ZIP-Integrität) |

Ergänzend: `status` (was ist veraltet, ohne zu laden - Dry-Run) und `clean`
(verwaiste Altversionen nach Update entfernen, standardmäßig nur anzeigen).

---

## 2. Authentifizierung - die kritische Designentscheidung

### 2.1 Warum kein Username/Passwort-Login

`gogrepo`/`gogrepoc` implementieren einen Formular-POST gegen die Login-Seite. Dieser Weg ist
**unzuverlässig**, weil GOG reCAPTCHA vorschaltet, sobald das Login-Muster automatisiert
aussieht. Genau daran scheitern die offenen Issues dieser Projekte regelmäßig. Wir bauen das
nicht nach.

### 2.2 Gewählter Flow: OAuth2 Authorization Code mit Browser-Übergabe

```
1. Tool öffnet im System-Browser des Nutzers:
   https://auth.gog.com/auth
     ?client_id=46899977096215655
     &redirect_uri=https://embed.gog.com/on_login_success?origin=client
     &response_type=code
     &layout=client2

2. Nutzer meldet sich normal an (inkl. 2FA-Mailcode, reCAPTCHA - im Browser unproblematisch).

3. Redirect landet auf .../on_login_success?code=<CODE>.
   Nutzer kopiert die URL bzw. den Code und fügt ihn im Terminal ein.

4. Tool tauscht Code gegen Token:
   POST/GET https://auth.gog.com/token
     grant_type=authorization_code, client_id, client_secret, code, redirect_uri
   → { access_token, refresh_token, expires_in ≈ 3600, session_id, user_id }

5. Persistiert wird ausschließlich der refresh_token.
   Access-Token wird bei Ablauf still über grant_type=refresh_token erneuert.
```

`client_id`/`client_secret` sind die öffentlich dokumentierten Galaxy-Client-Credentials -
dieselben, die auch `lgogdownloader` verwendet.

**Cookie-Jar wird nicht benötigt.** `lgogdownloader` kommt für Bibliothek und Downloads mit
`Authorization: Bearer <token>` aus. Das erspart uns die fragile Cookie-Persistenz von
`gogrepo` (`gog-cookies.dat`).

### 2.3 Token-Ablage

- Datei `~/.config/gog-repo-downloader/auth.json`, Rechte `0600`.
- Optional (Phase 2): macOS Keychain via `security`-CLI bzw. plattformneutral `keyring`.
- Kein Klartext-Passwort wird je gespeichert oder überhaupt entgegengenommen.

### 2.4 Komfort-Variante (optional)

Statt Copy-Paste kann das Tool einen lokalen Einmal-Listener auf `127.0.0.1:<port>` starten
und den Code automatisch abgreifen. **Einschränkung:** Die `redirect_uri` muss zu einer bei GOG
registrierten passen - `localhost` ist es nicht. Realistisch bleibt daher Copy-Paste als
Primärweg; der Listener funktioniert nur mit Browser-Extension oder Clipboard-Watcher und wird
deshalb **nicht** für Phase 1 eingeplant.

---

## 3. Datenquellen (API-Landkarte)

### Pfad A - Offline-Installer (Phase 1, entspricht gogrepo)

| Endpunkt | Liefert |
|----------|---------|
| `GET embed.gog.com/userData.json` | Auth-Probe, Username, Anzahl Spiele |
| `GET embed.gog.com/account/getFilteredProducts?mediaType=1&page=N` | Paginierte Bibliothek: `id`, `title`, `slug`, `updates`, `isNew` |
| `GET api.gog.com/products/{id}?expand=downloads,expanded_dlcs,description` | Installer, Patches, Extras, DLC-Baum |
| `GET api.gog.com{downlink}` | JSON mit signierter CDN-URL + URL des Checksum-XML |
| `GET <checksum-xml>` | `<file name= md5= total_size= chunks=>` |

Die `expand=downloads`-Payload liefert pro Installer:
`os`, `language`, `version`, `total_size` und je Datei `id`, `size`, `downlink`.
DLCs erscheinen als eigene Produkte unter `expanded_dlcs` mit identischer Struktur -
werden also **rekursiv** mit derselben Logik behandelt, nicht als Sonderfall.

### Pfad B - Galaxy Content-System (bewusst zurückgestellt)

`content-system.gog.com/products/{id}/secure_link` + `cdn.gog.com/content-system/v2/meta/…`
liefert chunk-basierte Manifeste mit `md5_compressed`/`md5_uncompressed` pro Chunk. Das ist der
Weg des Galaxy-Clients und ermöglicht **echte Delta-Updates** (nur geänderte Chunks laden).

Bewertung: deutlich höherer Aufwand (Chunk-Reassembly, Depot-Auflösung, V1/V2-Unterscheidung),
und das Ergebnis ist ein *entpacktes Spielverzeichnis*, kein Installer-Archiv. Für das gestellte
Ziel - Offline-Archiv der Installer - ist Pfad A richtig. Pfad B bleibt als Phase-3-Option
architektonisch offen (siehe §9).

---

## 4. Aktualitätsprüfung - der Kern des Tools

### 4.1 Warum Dateinamen nicht ausreichen

GOG ändert Installer auf **zwei** Arten:
1. Neuer Dateiname (`setup_spiel_2.1.0.exe` → `setup_spiel_2.1.1.exe`)
2. **Stille Neu-Uploads unter identischem Namen** (Repack, Hotfix, geänderte Sprachdatei)

Ein Namensvergleich sieht im Selbsttest korrekt aus und übersieht Fall 2 dauerhaft. Der
Dateiname ist deshalb **kein** Aktualitätssignal, sondern nur ein Ablage-Detail.

### 4.2 Autoritative Signale, in dieser Präzedenz

| # | Signal | Quelle | Kosten |
|---|--------|--------|--------|
| 1 | `version` | `downloads.installers[].version` | frei (in Bibliotheks-Update enthalten) |
| 2 | `size` | `downloads.installers[].files[].size` | frei |
| 3 | `md5` | Checksum-XML pro Datei | 1 Request/Datei |

**Regel: Abweichung in irgendeinem Signal ⇒ Datei gilt als veraltet.**
Fehlt `version` (kommt bei Extras/Bonus vor), rückt `size` auf Rang 1 und `md5` wird zum
Tiebreaker - bei Extras ist md5 daher standardmäßig aktiv, bei Installern nur bei `verify`
oder `--strict`.

### 4.3 Manifest-Eintrag

```
{ product_id, dlc_of, file_id, kind: installer|patch|extra,
  os, language, version, size, md5, filename,
  local_path, local_state: missing|partial|complete|stale,
  bytes_done, last_seen_utc, last_verified_utc }
```

`last_seen_utc` erlaubt das Erkennen von Dateien, die GOG **entfernt** hat - die bleiben lokal
erhalten, werden aber als `orphaned` markiert statt still zu verschwinden.

### 4.4 Ablage: SQLite statt einer großen Datei

`gogrepo` schreibt ein einzelnes Manifest-File; ein Abbruch mitten im Schreiben kostet den
kompletten Zustand. SQLite (WAL) gibt uns atomare Updates, inkrementelles Schreiben pro Produkt
und Abfragen wie „alle veralteten Windows-Dateien" ohne Vollparse. Ein `export`-Kommando kann
weiterhin JSON ausgeben.

---

## 5. Download-Engine

### 5.1 Signierte URLs laufen ab - Resume darf sie nicht wiederverwenden

Der `downlink`-Aufruf liefert eine **zeitlich signierte** CDN-URL. Wird sie im Manifest
persistiert und Stunden später zum Fortsetzen benutzt, antwortet der CDN mit 403.

**Konsequenz für den Resume-Pfad:**
```
resume(file):
  1. downlink NEU auflösen (Bearer-Token, ggf. vorher refreshen)
  2. Range: bytes=<bytes_done>- gegen die frische URL
  3. Antwort MUSS 206 Partial Content sein
     → 200 bedeutet: CDN ignoriert Range. Dann NICHT anhängen,
       sondern Datei verwerfen und sauber neu laden (sonst stille Korruption).
  4. Content-Range-Startoffset gegen bytes_done prüfen
```
Punkt 3 ist der Fehler, den man erst in Produktion bemerkt. Er gehört als expliziter Test in
die Implementierung.

### 5.2 Schreibstrategie

- Download nach `<ziel>.part`, Umbenennung erst nach erfolgreicher Größen-/MD5-Prüfung.
- `bytes_done` wird aus der tatsächlichen `.part`-Dateigröße abgeleitet, nicht aus der DB -
  die Datei ist die Wahrheit, die DB nur der Cache.
- Neue Version derselben Datei → Ablage in `<spiel>/` neben der alten; die Altversion wird
  nach erfolgreicher Verifikation der neuen **automatisch entfernt** (§5.5).

### 5.3 Parallelität

Standard: **2 gleichzeitige Dateien**, konfigurierbar via `--jobs`. Kein Multi-Connection-
Splitting einer einzelnen Datei - das reizt Rate-Limits, bringt bei GOGs CDN wenig und
verkompliziert Resume erheblich. Bei HTTP 429 exponentielles Backoff mit Respektierung von
`Retry-After`.

### 5.4 Fortschrittsanzeige

Zwei Ebenen gleichzeitig:
```
Gesamt   [████████░░░░░░░░]  12/47 Dateien · 8.2/31.5 GB · ETA 42m
Aktuell  Baldur's Gate 3 - setup_bg3_de_4.1.2_(1).bin
         [██████████████░░]  3.1/4.0 GB · 11.4 MB/s · ETA 1m20s
```
- TTY: Live-Refresh, gedrosselt auf ~4 Hz.
- Kein TTY (Cron, Pipe): zeilenweise Statusmeldungen ohne ANSI-Steuerzeichen, `--quiet` für
  reine Fehlerausgabe. Automatische Erkennung, kein manueller Schalter nötig.

### 5.5 Automatisches Aufräumen alter Versionen (Default: **an**)

Ohne Aufräumen wächst das Archiv mit jedem Update um eine volle Installer-Generation. Prune
läuft deshalb standardmäßig als Teil von `download`/`sync`, nicht als separates Opt-in-Verb.
Weil Löschen irreversibel ist, hängt alles an der Reihenfolge und an der Frage, was überhaupt
als „alte Version" gilt.

#### Ablauf - strikt in dieser Reihenfolge

```
prune(product, slot):
  1. ALLE Dateien der neuen Version dieses Slots sind local_state = complete
  2. Größe stimmt; md5 stimmt, sofern ein Checksum-XML vorlag
  3. erst dann: alte Version löschen
```

Schritt 1 ist der Punkt, an dem eine naive Implementierung Daten verliert: Große Installer sind
**mehrteilig** (`…_(1).bin`, `…_(2).bin`). Wird nach jeder fertigen Einzeldatei aufgeräumt,
verschwindet Teil 1 der alten Version, während Teil 2 der neuen noch fehlt - der Lauf bricht ab
und man hat *keine* vollständige Version mehr. Deshalb ist die Prune-Einheit der **Slot**
(product_id + kind + os + language), nicht die Einzeldatei.

#### Was gelöscht werden darf - und was nie

| Kategorie | Verhalten |
|-----------|-----------|
| Frühere Version desselben Slots, im Manifest erfasst | wird gelöscht |
| `.part`-Reste zu einer nicht mehr angebotenen Version | wird gelöscht |
| Datei, die GOG aus dem Angebot genommen hat (`orphaned`) | **bleibt**, wird nur gemeldet |
| Datei im Zielverzeichnis, die das Tool nie erfasst hat | **bleibt**, wird nur gemeldet |

Regel dahinter: Gelöscht wird ausschließlich, was das Tool selbst angelegt hat und wofür
nachweislich ein aktuellerer Ersatz vollständig auf der Platte liegt. Alles andere ist
Fremdbestand - dafür gibt es Meldungen, keine Löschung. Zusätzlich als hartes Sicherheitsnetz:
kein Pfad außerhalb von `<dest>/<slug>/`, keine Symlink-Verfolgung, absoluter Pfadvergleich
gegen `dest` vor jedem `unlink`.

#### Parameter

```
--prune            Default. Alte Versionen nach verifiziertem Ersatz löschen.
--no-prune         Alles behalten (Vollarchiv/Versionshistorie).
--keep-versions N  Default 1 = nur die aktuelle. N=2 hält eine Generation als Rückfallebene.
--prune-mode delete|trash    Default: delete.
                   trash = Verschieben nach <dest>/.trash/<datum>/, manuell zu leeren.
                   Löst das Platzproblem nur mit anschließendem Aufräumen - bewusst nicht Default.
--dry-run          Zeigt Downloads *und* geplante Löschungen mit Freigabe-Volumen, ohne beides.
```

Der erste Lauf nach einem Update meldet die Löschungen explizit im Protokoll
(`entfernt: setup_x_2.1.0.exe (4.2 GB) - ersetzt durch 2.1.1`), damit im Cron-Log
nachvollziehbar bleibt, wohin der Platz gegangen ist. `gogdl clean` bleibt zusätzlich als
manuelles Verb erhalten - für den Fall, dass mit `--no-prune` gearbeitet wurde oder ein
früherer Lauf abgebrochen ist.

#### Wechselwirkung mit `verify`

Nach `--prune` existiert die alte Version nicht mehr - ein später fehlschlagender
`verify --deep` auf der neuen Datei hat dann keine lokale Rückfallebene und erzwingt einen
Neu-Download. Wer das nicht will, nimmt `--keep-versions 2`. Bei `--prune-mode trash` liegt
die Vorgängerversion noch im Papierkorbordner und `verify` weist im Fehlerfall darauf hin.

---

## 6. CLI-Parameter

```
gogdl login
gogdl update   [--only <slug|id> …] [--skip <slug|id> …] [--full]
gogdl status   [Filter …]
gogdl download [Filter …] [--jobs N] [--dry-run] [--limit-rate 5M]
               [--prune|--no-prune] [--keep-versions N] [--prune-mode delete|trash]
gogdl verify   [Filter …] [--deep]     # --deep = MD5 + ZIP-Test
gogdl clean    [--apply]               # manuelles Nachholen; ohne --apply nur Anzeige

Filter (gelten für status/download/verify):
  --os windows,linux,mac | all        Default: nur die laufende Plattform
  --lang de,en                        Sprachcodes nach GOG-Schema
  --dlc / --no-dlc                    Default: an
  --extras / --no-extras              Default: aus (Extras sind groß und selten nötig)
  --patches / --no-patches            Default: aus
  --only / --skip                     Produktauswahl

Global: --config PATH  --dest PATH  --json (maschinenlesbare Ausgabe)  -v/-q
```

Konfigurationsdatei (TOML/YAML) mit denselben Schlüsseln; CLI schlägt Datei.
Wichtig für den Cron-Anwendungsfall: `update && download` als ein Aufruf
(`gogdl sync`) mit sauberen Exit-Codes (0 = nichts zu tun, 10 = etwas geladen,
>0 sonst = Fehler).

---

## 7. Modulschnitt

```
auth/        Token-Flow, Refresh, sichere Ablage
api/         Typisierte Clients für embed/api/content-system + Rate-Limit + Retry
model/       Produkt, Datei, Manifest-Eintrag, Filterlogik
store/       SQLite-Schema, Migrationen, Queries
sync/        Vergleich Remote↔Lokal → Arbeitsliste + Prune-Plan (das Herzstück, §4/§5.5)
download/    Resume, Range-Handling, .part-Verwaltung, Verifikation
prune/       Ausführung des Prune-Plans: Pfad-Whitelist, Slot-Vollständigkeit, unlink/trash
ui/          Fortschritt TTY vs. non-TTY, JSON-Ausgabe
cli/         Argument-Parsing, Kommandos, Exit-Codes
```

`sync/` ist bewusst **frei von I/O**: Eingabe sind zwei Listen (Remote-Metadaten,
lokaler Zustand), Ausgabe sind eine Download-Arbeitsliste und ein Prune-Plan. Dadurch sind
die beiden riskantesten Entscheidungen - „ist das veraltet?" und „darf das gelöscht werden?" -
ohne Netzwerk und ohne GOG-Konto testbar. `prune/` führt nur aus, was `sync/` beschlossen hat,
und lehnt jeden Plan-Eintrag ab, dessen Ersatz nicht als verifiziert markiert ist - die
Sicherheitsprüfung findet also zweimal statt, in Planung und Ausführung.

---

## 8. Risiken

| Risiko | Auswirkung | Gegenmaßnahme |
|--------|-----------|---------------|
| GOG ändert Auth-Flow oder Client-Credentials | Tool unbrauchbar | Auth-Schicht isoliert; Fehler mit klarer Meldung statt Stacktrace |
| CDN ignoriert `Range` | **stille Korruption** | 206 erzwingen, sonst Neuladen (§5.1) |
| Rate-Limiting / temporärer Bann | Abbruch mitten im Lauf | `--jobs` konservativ, Backoff, Wiederaufnahme jederzeit möglich |
| Checksum-XML fehlt für manche Dateien | md5-Prüfung nicht möglich | Fallback auf `size`, im Manifest als `md5: null` markiert, `verify` meldet es ehrlich |
| Sprach-/OS-Codes uneinheitlich (v. a. Altbestand) | Filter greift zu eng | Normalisierungstabelle + `--os`/`--lang` ohne Treffer → Warnung, nicht stilles Nichts |
| 2FA-Mailcode bei jedem neuen Gerät | Login bricht ab | Interaktiver Browser-Flow deckt das ab (§2.2) |
| Prune löscht Teil 1 der Altversion, bevor Teil 2 der neuen da ist | **Datenverlust: keine vollständige Version mehr** | Prune-Einheit ist der komplette Slot, nicht die Einzeldatei (§5.5) |
| Prune greift Fremddateien im Zielverzeichnis an | Verlust nicht ersetzbarer Daten | Nur manifest-erfasste Dateien mit verifiziertem Ersatz; Pfad-Whitelist unter `<dest>/<slug>/`, keine Symlinks |

---

## 9. Phasen

**Phase 1 (Kern)** - `login`, `update`, `status`, `download` mit Resume, Fortschritt,
Filter für OS/Sprache/DLC/Extras, SQLite-Manifest **und automatischem Prune (§5.5)**.
Prune ist Teil des Kerns, nicht des Komforts: ohne ihn ist das Tool nach wenigen
Update-Zyklen unbenutzbar. Die Größen-/MD5-Verifikation der Ersatzdatei ist damit
ebenfalls Phase 1 - sie ist die Vorbedingung jeder Löschung.
**Phase 2** - `verify --deep` (ZIP-Test), `clean` als manuelles Verb, `--prune-mode trash`,
Konfigurationsdatei, Keychain, `sync` + Exit-Codes für Cron, `--json`.
**Phase 3 (optional)** - Galaxy-Content-System (Pfad B) für echte Delta-Updates. Die
`api/`-Schicht wird in Phase 1 bereits so geschnitten, dass ein zweiter Download-Backend
danebentreten kann, ohne `sync/` anzufassen.

---

## 10. Getroffene Entscheidungen

1. **Implementierungssprache: Python 3** (`httpx` + `rich`, Begründung siehe Vergleich unten).
2. **Default für `--os`: nur die laufende Plattform.** Auf dem Mac also `mac`. Andere
   Plattformen nur bei expliziter Angabe - verhindert unbeabsichtigte 3-fache Downloadmenge.
   `--os all` als Kurzform für den Vollarchiv-Fall.
3. **Verzeichnislayout: flach je Spiel** - `<dest>/<slug>/<datei>`. OS und Sprache stecken
   bereits im GOG-Dateinamen; ein bestehender gogrepo-Bestand bleibt dadurch importierbar.
   Für die Ablage von Altversionen gilt: die neue Datei kommt daneben, die alte wird erst
   nach erfolgreicher Verifikation durch `clean` zur Löschung vorgeschlagen (§5.2).

### Sprachvergleich (Entscheidungsgrundlage)

Entscheidend ist eine einzige Frage: **Braucht der Login eine eingebettete Browser-Engine?**
Antwort: nein (§2.2) - `lgogdownloader` löst das in reinem C++. Damit ist die Sprachwahl frei
und reduziert sich auf zwei sinnvolle Kandidaten:

| | Python 3 | Go |
|---|---|---|
| Parität zum Vorbild | hoch (gogrepoc ist Python) | keine |
| Fortschritts-UI | `rich` - nahezu geschenkt | manuell, aber überschaubar |
| HTTP/Resume | `httpx` | stdlib reicht |
| Auslieferung | braucht Runtime + venv | **eine statische Binary** |
| Cron-Tauglichkeit | gut | sehr gut (keine Umgebungsabhängigkeit) |
| Nebenläufigkeit | `asyncio`, ausreichend | strukturierter, sauber begrenzbar |
| Hackbarkeit für dich | sehr hoch | mittel |

**Empfehlung: Python 3.** Der Engpass des Tools ist Netzwerk-I/O, nicht CPU; die Fortschritts-
und Retry-Logik ist mit `rich` + `httpx` erheblich schneller solide als selbstgebaut; und wenn
GOG etwas ändert, kannst du im Feld direkt nachbessern, ohne einen Build-Schritt.
Go gewinnt nur, wenn „eine Datei auf jeden Rechner kopieren" ein echtes Kriterium ist.
