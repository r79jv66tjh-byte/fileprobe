# FileProbe

Windows-Werkzeug zum **Untersuchen von Daten und Dateien**, mit deutscher Oberfläche (Python + tkinter).
Es vereint vier Tools in einem Fenster:

| Tab | Wozu |
|---|---|
| 🔍 **Format-Erkenner** | Herausfinden, was in einer unbekannten Datei steckt (auch ohne Endung) |
| 📦 **Archiv-Entpacker** | Spiel-Archive ansehen, Bilder/Sounds anzeigen und entpacken |
| 🗂 **Registry-Vergleich** | Registry vor/nach einer Installation sichern und Unterschiede zeigen |
| 👁 **Datei-Wächter** | Live sehen, welche Dateien geschrieben werden: so findest du Spielstände und Configs |

**Grundregel:** FileProbe liest nur. Untersuchte Dateien, Archive und die Registry werden nie verändert,
und unbekannte Dateien werden nie ausgeführt. Geschrieben wird nur dorthin, wo du es ausdrücklich wählst
(Entpack-Zielordner, Exporte, Snapshot-Ordner).

## Starten

Voraussetzungen: Python 3.11+ (getestet mit 3.13), `numpy`, `pillow`.

```bash
pip install numpy pillow
```

```bash
python FileProbe.py
```

Oder `start.bat` doppelklicken. Eine Datei als Parameter (`python FileProbe.py datei.pak`, oder Datei auf `start.bat`
ziehen) öffnet sie direkt: Archive im Archiv-Tab, alles andere im Format-Erkenner.

Eine eigenständige `FileProbe.exe` baut `build.ps1` (braucht `pyinstaller`).

### Beispieldateien

```bash
python tools/make_samples.py
```

legt im Ordner `beispiele/` 25 selbst erzeugte Testdateien an: Bilder ohne bzw. mit falscher Endung,
XOR-verschlüsselte, zufällige und komprimierte Daten, ein unbekannter Container mit eingebetteten Bildern und
Sounds sowie je ein Doom-WAD, Half-Life-WAD3, Quake-PAK, Unreal-PAK (v3 und v8), Unity-Bundle (LZ4, LZMA,
unkomprimiert), Valve-VPK (mit Teilarchiv) und Godot-PCK (Godot 3 und 4).

## 🔍 Format-Erkenner

- **Signaturen** von rund 70 Formaten: Bilder (PNG, JPEG, GIF, BMP, WebP, DDS, KTX, TIFF, PSD …), Audio/Video
  (WAV, Ogg, FLAC, MP3, MIDI, FMOD-FSB5, Wwise, Bink, MKV, MP4), Archive und Kompression (ZIP, RAR, 7z, CAB,
  gzip, bzip2, xz, zstd, LZ4), Spiel-Archive (UnityFS, Unity-SerializedFile, WADs, Quake-PAK, Unreal-PAK, VPK,
  Godot-PCK), Programme (PE mit EXE/DLL, 32/64 Bit, .NET; ELF, Java, Lua-Bytecode, WebAssembly), Dokumente,
  SQLite, Registry-Hives und Schriften.
- **Text-Erkennung** samt Untertyp: JSON (wird auf Gültigkeit geprüft), XML, HTML, INI, CSV, Schlüssel/Wert-Configs,
  UTF-8/UTF-16/Windows-1252.
- **Hinweis, wenn die Endung nicht passt** (z. B. ein JPEG namens `.txt`).
- **Entropie** gesamt und als Verlauf über die Datei (farbiges Diagramm, Maus zeigt Offset/Wert).
  Ohne erkennbare Signatur unterscheidet ein Chi²-Test:
  - Bytes gleichverteilt → **wahrscheinlich verschlüsselt**
  - hohe Entropie, aber nicht gleichverteilt → **wahrscheinlich komprimiert** (zusätzlich wird versucht,
    rohe Deflate- und zlib-Ströme zu erkennen)
- **Einfache XOR-Verschlüsselung** wird erkannt: Alle 255 Ein-Byte-Schlüssel werden probiert, ob daraus eine
  bekannte Signatur oder lesbarer Text entsteht. Das ist bei Spielständen häufig.
- **Eingebettete Dateien** an beliebiger Stelle finden (PNG, JPEG, GIF, BMP, WebP, WAV, Ogg, FSB5, ZIP, 7z,
  UnityFS, DDS) samt exakter Länge, mit Vorschau und Speichern.
- **Hex-Ansicht** ab beliebigem Offset und **Zeichenketten** (ASCII und UTF-16) mit Filter.

## 📦 Archiv-Entpacker

| Format | Typische Spiele/Engines | Was geht |
|---|---|---|
| ZIP / PK3 / PK4 | Quake 3, Doom 3, viele Mods | auflisten, entpacken |
| Doom-WAD (IWAD/PWAD) | Doom, Heretic, Hexen, Mods | Grafiken, Sprites, Flats → **PNG** (mit Palette PLAYPAL), Sounds → **WAV** |
| WAD3 / WAD2 | Half-Life, Counter-Strike 1.6 / Quake | Texturen (auch `{`-Transparenz) und Bilder → **PNG** (WAD3) |
| Quake-PAK | Quake 1/2, Half-Life | auflisten, entpacken |
| Unreal-PAK v1–9 | UE4 bis 4.25 | unverschlüsselt, unkomprimiert oder zlib/gzip |
| Unity-Bundle (UnityFS) | Unity-Spiele (`.unity3d`, `.bundle`) | LZ4/LZ4HC/LZMA-Blöcke entpacken, enthaltene Dateien auflisten |
| Valve-VPK v1/v2 | Source-Spiele (CS:GO, TF2, Portal 2 …) | inkl. Teilarchive `_000.vpk …` (die `_dir.vpk` öffnen) |
| Godot-PCK | Godot 3 und 4 | unverschlüsselt |
| alles andere | – | **Signatur-Suche**: eingebettete Bilder/Sounds/Archive finden |

- Vorschau für Bilder (auch umgewandelte Doom-/Half-Life-Grafiken), Text und Hex; WAV abspielbar.
- **„Bilder & Sounds entpacken“** holt nur Medien heraus und wandelt dabei Doom-/Half-Life-Grafiken um.
- **„Eintrag durchsuchen“** sucht in einem Eintrag nach eingebetteten Dateien, z. B. das PNG in einer
  Unity-`CAB-…`-Datei oder die FSB5-Soundbank in einer `.resource`. **„Eintrag als Archiv öffnen“** öffnet
  verschachtelte Archive (z. B. ein ZIP in einem PAK); „◀ Zurück“ führt wieder hinaus.
- Sicheres Entpacken: Pfade wie `../`, Laufwerksbuchstaben, verbotene Zeichen und reservierte Namen (`CON` …)
  werden entschärft, nichts landet außerhalb des Zielordners, und vorhandene Dateien werden nicht überschrieben.

**Grenzen (bewusst ehrlich):** Unity-Assets (Texture2D, AudioClip) werden nicht aus dem Unity-Objektformat
dekodiert. Das Tool zeigt die Dateien im Bundle, und die Signatur-Suche findet darin eingebettete PNG/OGG/WAV/FSB5.
Für vollständige Unity-Extraktion gibt es z. B. AssetStudio/UnityPy. Unreal-PAKs ab Version 10 (UE 4.26+/UE5),
verschlüsselte PAKs und Oodle-Kompression werden erkannt, aber nicht entpackt; dafür gibt es FModel/repak.
FSB5 wird als `.fsb` gespeichert, aber nicht in OGG umgewandelt.

## 🗂 Registry-Vergleich

1. Zweige wählen (Standard: `HKCU\Software` und `HKLM\SOFTWARE`; zusätzlich Dienste, Umgebungsvariablen, HKCR
   oder eigene Pfade wie `HKCU\Software\Valve`).
2. **Snapshot „vorher“** → Programm installieren/starten/Einstellung ändern → **Snapshot „nachher“**.
3. **Vergleichen**: Neue, gelöschte und geänderte Schlüssel und Werte werden farbig gelistet, mit altem und
   neuem Wert, Filter und Export als Textdatei.

Die Registry wird nur mit `KEY_READ` geöffnet (64-Bit-Ansicht; `WOW6432Node` ist enthalten). Snapshots landen als
komprimierte Textdateien (`*.pireg`) im Ordner `snapshots/` neben dem Programm. Der Vergleich liest sie
schrittweise und braucht deshalb auch bei großen Zweigen wenig Speicher. Zum Vergleich: Die
Standardauswahl (rund 550.000 Schlüssel und 930.000 Werte) dauerte auf dem Testrechner 15 s für einen
Snapshot (etwa 15 MB) und 9 s für den Vergleich. Groß- und Kleinschreibung von
Schlüsseln wird wie in Windows ignoriert. Exportiert wird absichtlich kein `.reg`, damit nichts versehentlich
importiert wird.

## 👁 Datei-Wächter

- Überwacht Ordner samt Unterordnern live (Windows `ReadDirectoryChangesW`). Vorbelegt sind Dokumente,
  „Saved Games“ und `AppData\Roaming`, `AppData\Local` sowie `AppData\LocalLow`; dort speichern die meisten Spiele.
- **„Ordner eines Programms…“** listet laufende Programme und fügt den Ordner der `.exe` hinzu.
- **Dateien (Zusammenfassung)**: pro Datei Anzahl der Änderungen, Zeit, Größe und eine Vermutung
  („möglicher Spielstand“, „Einstellungen/Config“, „Logdatei“ …), farbig markiert und filterbar.
- **Ereignisse (live)**: jede einzelne Änderung (erstellt, geändert, umbenannt, gelöscht).
- Störgeräusche (Browser-Caches, Temp, Microsoft-Ordner …) werden über editierbare Muster ausgeblendet. Ein
  Muster gilt nicht für einen Ordner, den du selbst ausdrücklich überwachst.
- Aus der Liste: im Explorer zeigen, Pfad kopieren, im Format-Erkenner analysieren (z. B. ob ein Spielstand
  verschlüsselt ist).

Windows verrät bei dieser Schnittstelle nicht, *welches* Programm eine Datei schreibt. Am besten startest du die
Überwachung, spielst kurz (speichern, Optionen ändern) und schaust dann in die Zusammenfassung.

## Claude-Schnittstelle

Damit Claude FileProbe über **ToolHub** (MCP-Server) bedienen kann, gibt es die Kommandozeile `cli.py` und die
Beschreibung `toolhub.json`. Jeder Befehl gibt genau ein JSON-Objekt auf stdout aus. Bei Fehlern steht die Meldung
als JSON (`{"error": "…"}`) auf stderr, der Exit-Code ist 1 (falsche Aufrufparameter: 2).

| Befehl | Zweck | readonly |
|---|---|---|
| `identify --path P` | Format, Entropie, Hinweise, eingebettete Dateien | ja |
| `strings --path P [--filter T]` | lesbare Zeichenketten mit Offset | ja |
| `hexdump --path P [--offset 0x10] [--length 256]` | Rohbytes | ja |
| `archive-list --path P [--filter *.png] [--kind Bild] [--carve]` | Archivinhalt | ja |
| `archive-extract --path P --out Z [--entries …] [--media-only] [--dry-run]` | entpacken | **nein** |
| `registry-snapshot [--roots "HKCU\Software;…"] [--label vorher] [--out D] [--dry-run]` | Snapshot-Datei schreiben (Registry nur lesen) | **nein** |
| `registry-snapshots [--folder D]` | vorhandene Snapshots | ja |
| `registry-compare --before A --after B [--filter T]` | Unterschiede | ja |
| `processes [--filter T]` | laufende Programme mit Ordner | ja |
| `watch [--process spiel.exe] [--folders "A;B"] [--seconds 30]` | geschriebene Dateien beobachten | ja |

- Befehle mit `"readonly": false` schreiben Dateien und unterstützen `--dry-run`: Dann wird nichts geschrieben,
  das JSON zeigt nur die geplanten Zieldateien bzw. die Snapshot-Datei. ToolHub hängt `--dry-run` automatisch an,
  solange nicht ausdrücklich bestätigt wurde.
- Lange Listen werden mit `--limit` gekürzt; das JSON meldet das mit `…_truncated: true`.
- `watch --process` überwacht den Ordner des laufenden Programms (Name oder PID). Ohne Angaben werden
  Dokumente, Saved Games und AppData beobachtet. Windows meldet auch hier nicht, welcher Prozess schreibt.
- `toolhub.json` setzt längere Zeitlimits für `watch` (bis 600 s Beobachtung) und große Registry-Zweige.

Beispiel:

```bash
python cli.py archive-extract --path spiel.wad --out bilder --media-only --dry-run
```

## Tests

```bash
python -m unittest discover -s tests -v
```

42 Tests erzeugen alle Beispieldateien neu und prüfen unter anderem:

- Erkennung, Entropie, Verschlüsselt/komprimiert/XOR und exakte Offsets und Längen eingebetteter Dateien
- Byte-genaues Entpacken jedes Archivformats und Pixelwerte der umgewandelten Doom-/Half-Life-Grafiken
- WAV-Parameter, LZ4 (auch überlappende Kopien) und die Pfad-Absicherung beim Entpacken
- dass die untersuchten Dateien danach unverändert sind (SHA-256)
- den Registry-Vergleich (synthetisch und mit einem echten, nur gelesenen Zweig)
- Live-Ereignisse des Datei-Wächters und die Prozessliste
- jeden Befehl aus `toolhub.json` so, wie ToolHub ihn aufruft (gültiges JSON, Dry-Run schreibt nichts, Fehler als JSON auf stderr)

## Aufbau

```
FileProbe.py               Start
cli.py                     JSON-Kommandozeile für Claude/ToolHub (Beschreibung: toolhub.json)
inspector/formats.py       Signaturen, Entropie, eingebettete Dateien, Hinweise
inspector/archives.py      Archiv-Leser + sicheres Entpacken
inspector/converters.py    Doom-/Half-Life-Grafiken und DMX-Sounds → PNG/WAV
inspector/lz4.py           LZ4-Blockdekompression (für Unity)
inspector/registry.py      Snapshots (nur lesen) und Vergleich
inspector/watcher.py       Ordnerüberwachung und Prozessliste (ctypes)
inspector/gui/             Oberfläche (ein Modul pro Tab)
tools/make_samples.py      Beispieldateien erzeugen
tests/                     Tests
```

## Lizenz

MIT – siehe [LICENSE](LICENSE).
