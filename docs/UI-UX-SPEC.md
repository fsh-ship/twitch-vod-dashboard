# Twitch VOD Dashboard – UI/UX Product Specification

**Stand:** 30.08.2026  
**Status:** Zielbild für die UI/UX-Überarbeitung – vor Implementierung  
**Source of Truth:** aktueller Repository-ZIP `twitch-vod-dashboard-main(2)(1).zip` + gemeinsam freigegebene UI-Mockups  
**Grundsatz:** Keine Funktionsregression. Bestehende Security-, Persistenz-, Ownership- und Recovery-Mechanismen bleiben erhalten.

---

## 1. Ziel

Das Twitch VOD Dashboard soll sich von einer historisch gewachsenen Admin-Oberfläche zu einer kohärenten, modernen Webanwendung entwickeln.

Das Ziel ist **nicht nur ein optisches Redesign**. Die Überarbeitung soll:

- den Systemzustand schneller erfassbar machen;
- automatische, manuelle und Recovery-Workflows klar voneinander trennen;
- die technische Entwicklungsgeschichte aus der normalen Benutzeroberfläche heraushalten;
- häufige Aufgaben mit weniger Reibung ermöglichen;
- parallele Download-/Upload-Aktivität korrekt darstellen;
- technische Kontrolle über Progressive Disclosure erhalten;
- Desktop und Mobile gleichwertig behandeln;
- für neue Nutzer des öffentlichen GitHub-Projekts ohne Vorwissen verständlich sein;
- ein hochwertigeres, reaktionsfreudigeres SaaS-App-Gefühl vermitteln.

---

## 2. Verbindliche Produktprinzipien

### 2.1 Benutzerabsicht vor Implementierungsnamen

Die UI soll nicht nach historischen internen Begriffen strukturiert werden. Begriffe wie `auto_vod`, `auto_youtube`, Ownership Ledger oder Coordinator dürfen intern bestehen bleiben, sind aber keine geeignete primäre Produktsprache.

### 2.2 Automatik ist sichtbar, aber nicht technisch überladen

Der Nutzer muss jederzeit erkennen können:

1. welche Automation global läuft oder pausiert ist;
2. welche Policy für einen Streamer gilt;
3. in welchem Schritt sich ein konkretes VOD befindet;
4. ob das System selbstständig weiterarbeiten kann;
5. ob eine Entscheidung erforderlich ist.

### 2.3 Normalzustand ruhig, Ausnahmen prominent

Ein gesundes System soll kompakt wirken. Fehler, Review-Bedarf, unterbrochene Recovery oder fälliges Cleanup erhalten dagegen klare Aufmerksamkeit.

### 2.4 Prozesse und Medien werden getrennt

- **Queue:** laufende/wartende Prozesse, Fehler, Recovery, History.
- **VODs:** Medien finden und lokale VODs verwalten.

`Ready for Upload` gehört daher langfristig nicht mehr als großer Medienbereich in die Queue.

### 2.5 Manuell und automatisch werden nicht vermischt

Eine manuell gestartete Suche bzw. ein manueller Download bleibt ein manueller Workflow, selbst wenn anschließend optional ein automatischer Upload ausgelöst wird.

### 2.6 Technische Details bleiben verfügbar

Logs, IDs, Pfade, Recovery-Gründe und technische Statusdaten werden nicht entfernt, sondern über `Technical details`, Disclosure Panels oder spezialisierte Detailansichten zugänglich gemacht.

### 2.7 Keine versteckte Automatik

Automatische Aktionen müssen vor ihrer Aktivierung nachvollziehbar konfiguriert werden. Laufende Automation und ihre Konsequenzen werden sichtbar kommuniziert.

---

## 3. Neues Produktmodell

### 3.1 VOD Automation

Ein abgeschlossener Twitch-VOD kann automatisch folgende Pipeline durchlaufen:

```text
Detect → Download → Prepare → YouTube Upload → Playlist → Local Retention
```

Pro Streamer wird künftig die gewünschte **VOD Handling Policy** gewählt:

| UI-Modus | Bedeutung | Bestehende technische Entsprechung |
|---|---|---|
| **Manual** | Keine automatische Behandlung abgeschlossener VODs | `auto_vod_download = false`, `auto_youtube_upload = false` |
| **Auto Download** | Neue VODs erkennen und herunterladen | `auto_vod_download = true`, `auto_youtube_upload = false` |
| **Download + YouTube** | Neue VODs erkennen, herunterladen und anschließend über den Auto-YouTube-Lifecycle verarbeiten | `auto_vod_download = true`, `auto_youtube_upload = true` |

Ein bestehender historischer Zustand `auto_vod_download = false` + `auto_youtube_upload = true` darf **nicht stillschweigend verändert** werden. Die neue UI markiert ihn als `Configuration needs review`, bis eine explizite Entscheidung getroffen wurde.

### 3.2 Globale Automation Controls

Streamer-Policy und globale Betriebssteuerung sind unterschiedliche Ebenen.

Unter `Settings → Automation` stehen globale Controls wie:

- **VOD monitoring:** Running / Paused
- **Automatic YouTube processing:** Running / Paused
- **Automatic live recording:** Running / Paused
- **VOD monitoring interval:** z. B. jede Stunde
- **Default automatic retention:** falls weiterhin global benötigt

Globale Pause verändert die konfigurierte Streamer-Policy nicht.

### 3.3 Live Recording

Live-Aufnahme ist fachlich ein eigener Prozess:

```text
Live detected → Recording started → Recording saved
```

Pro Streamer:

- **Manual**
- **Automatic**

Globale Auto-Recording-Steuerung bleibt davon getrennt.

### 3.4 Manual VOD Workflow

Manuelle VOD-Beschaffung bleibt vollständig erhalten:

```text
Find VODs / Direct VOD → Select → Download → optional manual-post-download handling
```

Der bestehende Legacy-Pfad `Upload Automatically After Download` wird nicht mit VOD Automation gleichgesetzt. Falls er als eigenständiger Workflow bestehen bleibt, wird er kontextuell beim manuellen Download formuliert, z. B.:

- `Keep ready for review`
- `Upload to YouTube after download`

### 3.5 YouTube

In der normalen UI werden folgende Dinge getrennt:

- **YouTube connection:** Connected / Not connected / Attention
- **Automatic YouTube processing:** global Running / Paused
- **Manual upload:** bewusste Benutzeraktion
- **Upload defaults:** Visibility, Playlist, Metadata etc.

Die derzeitige Beschriftung `Enable YouTube Uploads` ist als allgemeiner Upload-Killswitch irreführend und soll in dieser Form nicht bestehen bleiben.

### 3.6 Local Retention

Für die neue automatische Pipeline lautet die Benutzerentscheidung:

- **Keep local copy**
- **Remove after 1 / 3 / 6 / 12 / 24 / 48 hours**

Cleanup wird erst nach den bestehenden Sicherheits-/Lifecycle-Bedingungen durchgeführt. Recovery- und Ownership-Mechanismen bleiben unverändert.

Der ältere Mechanismus `Archive Local VOD After Successful Upload` wird fachlich getrennt behandelt und nicht als Synonym für Auto-Retention dargestellt.

---

## 4. Hauptnavigation

Verbindliche Zielnavigation:

```text
Dashboard
VODs
Live
Queue
Settings
```

### Bedeutung

- **Dashboard:** Gesamtzustand, Attention, aktuelle Aktivität, schneller Überblick.
- **VODs:** VODs manuell finden und lokale Medien verwalten.
- **Live:** Live-Streamer und Recording.
- **Queue:** laufende und wartende Prozesse, Fehler, Recovery und History.
- **Settings:** Systemverhalten konfigurieren.

Desktop verwendet eine persistente Sidebar. Mobile verwendet einen zugänglichen Drawer.

Optional im unteren Sidebar-Bereich:

- `VOD Automation · Healthy/Paused/Attention`
- `YouTube · Connected/Attention`
- Storage-Kurzstatus

Diese Statuszeilen sind Navigation/Status, keine zusätzlichen globalen Schalter.

---

## 5. Dashboard – Control Center

### 5.1 Primäre Frage

Das Dashboard beantwortet in dieser Reihenfolge:

1. **Ist das System gesund?**
2. **Braucht etwas meine Aufmerksamkeit?**
3. **Was läuft gerade?**
4. **Was passiert live?**
5. **Welche häufigen manuellen Aktionen brauche ich?**

### 5.2 System Overview

Kompakte Karten für:

- **VOD Automation** – Healthy / Paused / Checking / Attention; Zahl überwachter Streamer; Aufteilung `Download + YouTube` / `Download only`.
- **Live Recording** – Healthy / Paused / Attention; Zahl aktueller Aufnahmen; Zahl automatisch überwachter Streamer.
- **Queue** – Anzahl Running und Waiting; bei Parallelität getrennt `1 download + 1 upload`.
- **YouTube** – Connection State, Default Visibility, optional Default Playlist.
- **Storage** – frei/gesamt; klare Warnung bei knappem Speicher.

### 5.3 Needs Attention

Nur sichtbar, wenn tatsächlich relevante Fälle existieren.

Beispiele:

- Upload failed / retries exhausted
- Playlist review required
- Cleanup needs attention
- persistence/recovery degraded
- invalid/migrated streamer configuration requires review
- YouTube connection requires action

Jeder Eintrag enthält:

- verständliche Ursache;
- betroffene Anzahl/VOD;
- genau eine primäre Aktion (`Review`, `Open`, `Reconnect`, ...).

### 5.4 Current Activity

#### Kein laufender Prozess

Kompakter Empty State, kein großer leerer Block.

#### Genau ein laufender Prozess

Eine prominente Activity Card.

#### Download und Upload parallel

Desktop: zwei gleichwertige Lane Cards nebeneinander.

```text
Current Activity · 2 running

Download                         Upload
bearlychen                       celiciious
63% · 18.4 MB/s · 12 min        22% · 1.0 MB/s · 1h 42m
```

Mobile: Cards untereinander.

### 5.5 Lifecycle eines einzelnen Auto-VODs

Innerhalb einer VOD Activity Card kann die Pipeline dargestellt werden:

```text
Downloaded ✓ → Prepared ✓ → Uploading 22% → Playlist pending → Retention pending
```

Dies ist **VOD-Lifecycle**, nicht Queue-Lane-Status.

### 5.6 Live Now

Kompakte Liste statt großer Recorder-Karten.

Zeigt primär:

- Avatar
- Streamer
- Live-Indikator
- optional Titel/Game/Viewer, wenn zuverlässig vorhanden
- `View all`

Recording-Aktionen befinden sich im eigenen `Live`-Screen bzw. als kontextuelle Quick Action.

### 5.7 Quick Actions

Beispiele:

- Find VODs
- Direct VOD
- Start Live Recording
- Open Queue
- Manage Streamers
- View Local VODs

Keine redundanten Aktionen, die bereits dominant im selben Bereich stehen.

---

## 6. VODs

Zwei Haupttabs:

```text
Find VODs | Local VODs
```

### 6.1 Find VODs

Die heutige 4-Schritt-Wizard-Anmutung wird durch eine kompakte Such-/Filter-Arbeitsfläche ersetzt.

#### Date presets

Verbindlich:

- **Today**
- **Yesterday → Today**
- **Last 7 days**
- **Last 30 days**
- **Custom**

`Yesterday → Today` bedeutet: Startdatum = vorheriger lokaler Kalendertag, Enddatum = heutiger lokaler Kalendertag, jeweils inklusiv.

**Technischer Hinweis aus dem aktuellen Source of Truth:** Die bestehende Funktion `setDateRange()` verwendet `toISOString().slice(0,10)` und damit UTC-Datumswerte. Beim Redesign müssen Presets kalenderlokal berechnet werden, damit `Today` bzw. `Yesterday → Today` rund um Mitternacht in positiven UTC-Offets nicht um einen Tag verrutschen.

#### Streamer Picker

Statt permanenter Checkbox-Wand:

- Control `12 selected` / `All streamers`
- Suchfeld im Picker
- Checkboxen
- Select all / Clear
- außen optional die ersten Chips + `+N`

Der bisherige separate `Streamer Selection Mode` entfällt als doppelte Bedienlogik. Ein Streamer, mehrere Streamer oder alle Streamer werden im Picker selbst ausgedrückt.

#### Filters

Advanced-Optionen bleiben verfügbar, aber kompakt hinter `Filters`.

Beispiele:

- Search depth per streamer
- Include VODs with unknown dates
- Enforce exact date range
- Hide live/upcoming
- Twitch VOD links only

Technical Search Details bleiben separat einklappbar.

#### Ergebnisse

Ergebnisse sind der visuelle Mittelpunkt.

Desktop:

- kompakte Tabelle/Liste;
- nach Streamer gruppiert;
- Gruppen ein-/ausklappbar;
- Suchfeld innerhalb Resultate;
- Statusfilter;
- Streamerfilter;
- Sortierung.

Beispielstatus:

- New
- Downloaded
- In queue
- Already in archive

Status darf nicht nur durch Farbe vermittelt werden.

#### Selection Action Bar

Nur sichtbar, wenn mindestens ein VOD ausgewählt wurde.

Beispiel:

`3 VODs selected · 26.8 GB` → `Download 3 VODs` / `Clear`

Desktop sticky innerhalb des Arbeitsbereichs; Mobile als gut erreichbare Bottom Action Bar, ohne Content zu verdecken.

#### Direct VOD

Nicht mehr als angehängte Karte am Seitenende.

Desktop: kompakter Side Panel / Bereich neben den Suchfiltern oder über eine `Browse | Direct VOD`-Umschaltung.

Mobile: eigener Subtab `Direct VOD`.

Flow:

`URL or numeric ID → Check VOD → VOD preview → Download`

### 6.2 Local VODs

Der heutige `Ready for Upload`-Bereich wird zu einer richtigen Medienansicht.

Filter/Views:

- All
- Ready
- Uploading
- Uploaded
- Cleanup scheduled
- Needs attention

Ein Local-VOD-Eintrag kann zeigen:

- Avatar / Streamer
- Datum
- Titel
- Dateigröße
- Herkunft: Manual / VOD Automation / Live Recording
- aktueller Lifecycle-/Uploadzustand
- Retention-Zustand
- kontextuelle Actions

Technische Details und seltene Aktionen liegen im `…`-Menü bzw. Disclosure.

---

## 7. Live

Eigener Screen für Live-Status und Recording.

### Standardansicht

- Live now
- Recording now
- automatisch überwachte Streamer
- Offline-Gruppe einklappbar

Live-Karten sind kompakter als die aktuellen Dashboard-Karten.

Pro Live-Streamer:

- Avatar
- Streamer
- Titel
- `Live since`
- Auto-Recording-Policy / Recording-Zustand
- primäre Aktion `Start Recording`, wenn sinnvoll

Abgeschlossene Aufnahme kann als temporärer Success-State erscheinen, ohne einen Live-Streamer fälschlich weiterhin als aktiv darzustellen.

---

## 8. Queue

### 8.1 Verantwortlichkeit

Queue zeigt **Prozesse**, nicht die gesamte lokale Medienbibliothek.

### 8.2 Lanes

Das aktuelle System besitzt getrennte Download- und Upload-Lanes. Die UI muss darstellen können, dass beide parallel aktiv sind.

#### Running

Desktop bei paralleler Aktivität:

- Download Card
- Upload Card

Je Card:

- Streamer / VOD
- Prozessart
- Fortschritt
- Geschwindigkeit
- ETA
- Bytes/Größe, soweit verfügbar
- lane-spezifische Pause
- sekundäre Aktionen / Technical details

#### Up Next

Nach Lane getrennt:

- Downloads · N waiting
- Uploads · N waiting

### 8.3 Needs Attention

Eigener sichtbarer Bereich nur bei relevanten Zuständen.

Nicht mit Completed vermischen.

### 8.4 History

- Completed
- Cancelled

Im Normalzustand kompakt.

Filter/Details erst bei Öffnung.

### 8.5 Recovery

Recovery-Ergebnisse werden verständlich formuliert. Technische Gründe bleiben unter Details verfügbar.

---

## 9. Settings

Zieltabs:

```text
General | Automation | Streamers | YouTube | Advanced
```

### 9.1 General

Nur echte allgemeine Defaults, z. B.:

- Quality
- File Format
- Twitch Download Rate Limit
- allgemeines Prepare-Verhalten, sofern weiterhin sinnvoll

Automatikschalter werden aus General herausgezogen.

### 9.2 Automation

Globale Betriebssteuerung:

- VOD monitoring
- Automatic YouTube processing
- Automatic live recording
- Monitoring interval
- Default Retention, sofern global erforderlich

Jede Automation zeigt Status + Control getrennt:

`Running` ist ein Zustand; `Pause` ist eine Aktion.

### 9.3 Streamers

Normalansicht = kompakte verwaltbare Liste, keine permanente Vollkonfiguration jedes Streamers.

Spalten/Informationen:

- Avatar + Streamer
- VOD workflow
- Playlist
- Live recording
- Retention
- `…` Actions

Werkzeuge:

- Search
- Workflow filter
- Add streamer
- Pagination oder sinnvolle virtuelle/segmentierte Darstellung bei langen Listen

Bearbeitung über Detailpanel / Inline Detail Area.

Pro Streamer:

- VOD Workflow: Manual / Auto Download / Download + YouTube
- Playlist
- Live Recording: Manual / Automatic
- Retention
- ggf. Recording Quality/Folder, falls tatsächlich streamer-spezifisch vorgesehen

Folgenschwere Änderungen müssen explizit gespeichert/bestätigt werden. Rein lokale ungefährliche Präferenzen können später autosave-fähig werden.

### 9.4 YouTube

Gliederung:

1. Connection
2. Upload defaults
3. Metadata
4. Manual upload behavior, falls Legacy-Workflow beibehalten wird
5. Advanced YouTube options

Nicht nebeneinander als scheinbar gleichwertige globale Schalter darstellen:

- Enable YouTube Uploads
- Upload Automatically After Download
- Auto YouTube
- Archive Local VOD After Successful Upload

Diese werden entsprechend ihrer tatsächlichen Semantik neu einsortiert.

### 9.5 Advanced

Technische Runtime-/yt-dlp-Einstellungen bleiben klar als Advanced markiert.

Bestehendes Progressive-Disclosure-Prinzip wird beibehalten bzw. verbessert.

---

## 10. Status- und Badge-System

Nicht jede Information wird in ein einzelnes Status-Badge gepresst.

### 10.1 Dimension A – Herkunft

Beispiele:

- Manual
- VOD Automation
- Live Recording

### 10.2 Dimension B – Prozessphase

Beispiele:

- Waiting
- Downloading
- Preparing
- Uploading
- Playlist
- Retention
- Completed

### 10.3 Dimension C – Gesundheits-/Ausnahmezustand

Beispiele:

- Healthy
- Paused
- Checking
- Needs attention
- Failed
- Interrupted

### 10.4 Dimension D – lokale Datei

Beispiele:

- Local copy kept
- Cleanup scheduled
- Cleanup due
- Local copy removed

### Farbregeln

- Grün: bestätigter Erfolg / gesund
- Blau/Violett: aktive normale Verarbeitung / primäre Navigation
- Gelb/Orange: wartet auf Entscheidung, Warnung, paused
- Rot: Fehler oder destruktive Aktion
- Grau: neutral/inaktiv

Farbe nie als alleiniger Bedeutungsträger.

---

## 11. Interaktionssystem / SaaS-App-Feeling

### 11.1 Feedback

Browser-`alert()` und `confirm()` werden schrittweise aus dem normalen Produktflow entfernt.

Verwenden:

- Inline feedback für feldbezogene Fehler
- Toast für kurzlebige Erfolgsmeldungen
- Attention/Warning Region für systemweite Probleme
- Modal Dialog nur für echte Bestätigung/Entscheidung

### 11.2 Save-Verhalten

Nicht jeder Settings-Bereich benötigt einen großen globalen `Save`-Button.

Mögliche Regeln:

- ungefährliche Einzeländerung: direkt speichern + `Saving…` → `Saved`
- mehrere zusammenhängende Streamer-Policy-Änderungen: `Save changes`
- destruktiv / weitreichend: explizite Bestätigung

Keine stille Zustandsänderung historisch inkonsistenter Konfiguration.

### 11.3 Action Menus

Seltene sekundäre Aktionen werden in `…`-Menüs verschoben.

Primäraktionen bleiben sichtbar.

### 11.4 Progressive Disclosure

Verwenden für:

- Technical details
- Logs
- Recovery metadata
- Advanced search
- Advanced YouTube
- Runtime details

### 11.5 Loading

- Button-interne Loading States für kurze Requests
- Skeletons nur dort, wo echte Inhaltsflächen geladen werden
- vorhandenen Inhalt nicht unnötig komplett ausblenden

### 11.6 Animation

Dezent:

- Disclosure
- Toast Ein-/Ausblendung
- Progress-Updates
- kleine State Transitions

Keine Animation, die Bedienung verzögert. `prefers-reduced-motion` berücksichtigen.

---

## 12. Twitch Avatare

Streamer-Profilbilder werden als produktive UX-Komponente vorgesehen.

### Ziele

- schnellere visuelle Wiedererkennung;
- bessere Scanbarkeit bei vielen Streamern;
- konsistente Darstellung in Dashboard, VODs, Live, Queue und Streamer Settings.

### Technisches Zielbild

Profilbildquelle über Twitch beziehen und lokal cachen.

Cache-Metadaten mindestens konzeptionell:

- streamer login
- display name, sofern verfügbar
- source avatar URL / identifier
- cached file
- last refreshed

### Regeln

- kein Twitch-Request bei jedem Seitenrender;
- Aktualisierung nur periodisch/bei Bedarf;
- fehlendes Bild → Initialen-Avatar;
- fehlerhafter Cache → Initialen-Avatar, keine kaputte Bilddarstellung;
- Avatar-Ausfall darf Kernfunktionen nicht beeinträchtigen.

---

## 13. Mobile – 375 px als Pflichtviewport

Mobile UX ist keine bloße gestapelte Desktopansicht.

### Navigation

Drawer mit:

- Dashboard
- VODs
- Live
- Queue
- Settings
- Systemstatus unten

Anforderungen:

- Menu Button mit `aria-expanded`/`aria-controls`
- Escape schließt
- Backdrop schließt
- sinnvoller Fokus beim Öffnen
- Fokus kehrt beim Schließen zum Trigger zurück

### Dashboard

- System Overview stark verdichtet
- Current Activity vor langen Live-Listen
- parallele Download-/Upload-Cards untereinander
- `Live now` kompakt, `View all`

### VOD Search

- Presets horizontal scrollbar oder responsive segmented layout ohne Seiten-Horizontaloverflow
- Streamer Picker als mobile Auswahlfläche
- Direct VOD als Subtab/Panel
- Resultate als mobile Cards bzw. responsive Rows
- Selection Action Bar gut erreichbar

### Queue

- Tabs/Lane Filter dürfen verwendet werden, aber globale Parallelität muss weiterhin ersichtlich sein
- keine Desktop-Zweispalten erzwingen
- primäre Touch-Aktionen großzügig

### Streamer Settings

- Liste kompakt
- Bearbeitung eines Streamers als fokussierte Detailansicht
- nicht 26 vollständig aufgeklappte Konfigurationskarten untereinander

---

## 14. Accessibility-Baseline

Ziel: WCAG 2.2 AA als praktische Mindestbasis für die neue UI.

### Verbindliche Anforderungen

- Reflow ohne zweidimensionales Scrollen bei schmalen Viewports, soweit vom Inhalt nicht technisch zwingend anders erfordert.
- Pointer Targets mindestens nach WCAG 2.2 AA; für primäre Mobile-Aktionen wird möglichst ~44×44 CSS px als komfortabler Zielwert genutzt.
- sichtbare Focus States.
- korrekte semantische Buttons/Links.
- Status zusätzlich zu Farbe als Text/Icon.
- dynamische Statusmeldungen über geeignete Live Regions, ohne unnötige Fokusverschiebung.
- Tabs nach WAI-ARIA Tabs Pattern inkl. `tablist`, `tab`, `tabpanel`, `aria-selected` und sinnvoller Keyboard-Navigation.
- Disclosure Controls mit `aria-expanded` und optional `aria-controls`.
- Action-Menüs mit zugänglicher Menu-Button-Semantik oder bewusst einfacher, nativer Button-/Popover-Struktur.
- Modal Dialoge mit Fokusmanagement, `role="dialog"`, `aria-modal="true"`, Label und sichtbarer Close/Cancel-Aktion.
- Mobile Drawer besitzt sauberes Fokus- und Escape-Verhalten.
- keine Information ausschließlich auf Hover.

### Referenzen

- WCAG 2.2 Understanding / Quick Reference: https://www.w3.org/WAI/WCAG22/
- WAI-ARIA APG: https://www.w3.org/WAI/ARIA/apg/
- GOV.UK Design System – Notifications/Buttons als zusätzliche Interaktionsreferenz: https://design-system.service.gov.uk/

---

## 15. Visuelles System

Die bestehende dunkle Grundidentität bleibt erhalten, wird aber zu einem systematischeren Design-System ausgebaut.

### Komponenten

- App Shell / Sidebar / Mobile Drawer
- Page Header
- System Health Card
- Content Card
- Activity Card
- Attention Row
- Badge / Status Dot
- Progress Bar
- Tabs / Segmented Controls
- Buttons: primary / secondary / quiet / danger
- Inputs / Selects / Search
- Action Menu
- Toast
- Confirmation Dialog
- Empty State
- Skeleton
- Avatar

### Design Tokens

Bei Umsetzung über semantische Variablen statt verstreuter Einzelwerte:

- surfaces
- borders
- text primary/secondary/muted
- accent
- success/warning/danger/info
- spacing scale
- radius scale
- shadows
- focus ring
- typography scale
- control heights

Die endgültigen Werte werden aus der bestehenden visuellen Identität abgeleitet; kein unnötiger Framework-/Dependency-Wechsel.

---

## 16. Priorisierte Roadmap

# P0 – semantisch irritierend / potenziell gefährlich

1. Produktmodell für `Auto VOD` + `Auto YouTube` verbindlich auf `VOD Automation` abbilden.
2. Historisch inkonsistente Streamer-Konfiguration (`Auto YouTube` ohne `Auto VOD`) in neuer UI erkennen und zur Review markieren; nicht still ändern.
3. `Enable YouTube Uploads` nicht mehr als globalen Upload-Killswitch darstellen, solange die tatsächliche Semantik das nicht garantiert.
4. Legacy `Upload Automatically After Download` klar vom Auto-VOD/Auto-YouTube-Lifecycle trennen.
5. Legacy Archive und Auto-Retention/Cleanup getrennt benennen.
6. Destruktive und zustandsverändernde Aktionen konsistent kennzeichnen und bestätigen.
7. Date-Preset-Berechnung kalenderlokal statt UTC-abhängig gestalten, damit Today/Yesterday→Today rund um Mitternacht korrekt sind.

# P1 – hoher täglicher Nutzen

1. Neue Hauptnavigation: Dashboard / VODs / Live / Queue / Settings.
2. Dashboard als Control Center.
3. Eigener Live-Screen; große Live-Recorder-Liste vom Dashboard entfernen.
4. VODs-Bereich mit Find VODs / Local VODs.
5. Search Redesign inkl. Yesterday → Today, Streamer Picker, Filters und Selection Bar.
6. Queue auf Prozesse fokussieren; Download + Upload parallel sichtbar.
7. Local VODs aus Queue herauslösen.
8. Streamer Management auf kompakte Liste + Detailbearbeitung umstellen.
9. Neue VOD-Workflow-Policy: Manual / Auto Download / Download + YouTube.
10. Settings um Automation-Tab erweitern und historische Schalter korrekt einsortieren.
11. Needs Attention als app-weites, konsistentes Muster.
12. Twitch Avatare in zentralen Streamer-Darstellungen.

# P2 – Konsistenz / Interaction Quality / Polish

1. Toast-System statt normaler Browser-Alerts.
2. Konsistente Confirmation Dialogs statt uneinheitlicher `confirm()`-Flows.
3. Saved/Saving/Error-Feedback und selektives Autosave.
4. konsistente Action-Menüs.
5. Badge-/Statussystem vereinheitlichen.
6. Loading/Skeleton-Zustände.
7. vollständige Keyboard-/Focus-/ARIA-Überarbeitung.
8. Mobile-Polish bei 375 px und Reflow-Prüfung.
9. konsistente Abstände, Typografie, Card-Dichte und Button-Hierarchie.
10. dezente Transitions + Reduced Motion.

# P3 – Nice-to-have

1. periodische Avatar-Aktualisierung/Cache-Management verbessern.
2. zusätzliche Live-Metadaten (Game/Viewer), falls zuverlässig und API-seitig sinnvoll.
3. gespeicherte Suchpresets/Filter.
4. erweiterte Activity Timeline.
5. zusätzliche Storage-/Lifecycle-Statistiken.
6. optionale Personalisierung von Dashboard-Kurzinfos, nur falls später echter Bedarf besteht.

---

## 17. Umsetzung in Codex-Slices

Kein Big-Bang-Redesign.

### Slice 1 – Design Foundation

**Risiko:** niedrig bis mittel  
**Empfohlen:** GPT-5.6 Terra · medium

- vorhandene CSS-Struktur inventarisieren;
- semantische Design Tokens einführen;
- Basiskomponenten für Cards, Buttons, Badges, Inputs, Focus States und Spacing konsolidieren;
- vorhandenes Verhalten und Seitenstruktur noch nicht grundsätzlich ändern;
- keine neuen Dependencies;
- bestehende UI-Tests anpassen/ergänzen, nur soweit durch Klassenstruktur erforderlich.

### Slice 2 – Navigation Shell

**Empfohlen:** GPT-5.6 Terra · medium

- neue Hauptnavigation strukturell vorbereiten;
- Desktop Sidebar;
- Mobile Drawer;
- zugängliches Focus-/Escape-Verhalten;
- noch keine große fachliche Verlagerung von Funktionen.

### Slice 3 – VOD Find Experience

**Empfohlen:** GPT-5.6 Terra · high

- Search VODs → VODs / Find VODs;
- kompakte Filterleiste;
- Yesterday → Today;
- lokale Kalenderdatumsberechnung;
- Streamer Picker;
- Filters;
- Selection Bar;
- Direct VOD neu integrieren;
- Search-Backend-Semantik nicht unbeabsichtigt ändern.

### Slice 4 – Live Screen

**Empfohlen:** GPT-5.6 Terra · medium

- Live-Bereich aus Dashboard in eigenen Screen verschieben;
- bestehende manuelle Recording-Funktion erhalten;
- Auto-Recorder-Zustände weiterhin korrekt darstellen.

### Slice 5 – Dashboard Control Center

**Empfohlen:** GPT-5.6 Terra · high

- System Overview;
- Needs Attention;
- Current Activity;
- paralleler Download + Upload;
- kompakter Live-Überblick;
- Quick Actions;
- keine neue Backend-Semantik erfinden.

### Slice 6 – Queue Redesign

**Empfohlen:** GPT-5.6 Terra · high

- Download-/Upload-Lanes;
- Running/Up Next/Needs Attention/History;
- parallele Aktivität;
- bestehende Queue-/Recovery-Fähigkeiten vollständig erhalten.

### Slice 7 – Local VODs

**Empfohlen:** GPT-5.6 Terra · high

- `Ready for Upload` aus Queue in VODs → Local VODs verschieben;
- bestehende Actions/Status semantisch neu ordnen;
- keine Datei-/Cleanup-Semantik ändern.

### Slice 8 – Automation Product Model / Migration

**Risiko:** hoch  
**Empfohlen:** GPT-5.6 Sol · high

- UI-Policy Manual / Auto Download / Download + YouTube auf bestehende Settings abbilden;
- bestehende inkonsistente Profile erkennen;
- keine stille Migration mit Informationsverlust;
- Persistenz-/Recovery-/Ownership-Semantik bewahren;
- umfassende Regressionstests.

### Slice 9 – Streamer Management

**Empfohlen:** GPT-5.6 Terra · high

- kompakte Liste;
- Suche/Filter;
- Detailbearbeitung;
- Workflow/Playlist/Recording/Retention;
- bestehende Reihenfolge-/Dateipersistenz sicher erhalten oder bewusst migrieren.

### Slice 10 – Settings Cleanup

**Empfohlen:** GPT-5.6 Terra · high

- General / Automation / Streamers / YouTube / Advanced;
- Legacy-Begriffe und Schalter neu einordnen;
- tatsächliche Semantik nicht durch reine UI-Vereinfachung verändern.

### Slice 11 – Interaction & Accessibility Polish

**Empfohlen:** GPT-5.6 Terra · high

- Toasts;
- Dialogs;
- Auto/Inline save;
- Loading/Skeleton;
- Actions Menu;
- Tabs/ARIA;
- Drawer Focus;
- Keyboard;
- Reduced Motion;
- 375-px Browserprüfung.

### Slice 12 – Twitch Avatar Cache

**Empfohlen:** GPT-5.6 Terra · high

- Twitch-Profilbilder kontrolliert beziehen;
- lokalen Cache + Fallback;
- Refresh-Strategie;
- Fehler darf Kernworkflow nicht beeinträchtigen;
- externe Requests in Tests mocken.

Die Avatar-Arbeit kann technisch früher eingeordnet werden, wenn sie für neue Live-/Streamer-Komponenten benötigt wird; sie darf aber nicht unnötig den ersten Layout-Slice vergrößern.

---

## 18. Nicht-Ziele

- kein vollständiger Framework-Rewrite;
- kein React/Vue/etc. nur für das Redesign;
- keine Änderung der bestehenden Security-Grenzen;
- keine Vereinfachung von Ownership/Recovery auf Kosten der Robustheit;
- keine Entfernung manueller Workflows;
- keine Entfernung technischer Diagnosemöglichkeiten;
- keine Zusammenlegung fachlich verschiedener Legacy-/Auto-Cleanup-Mechanismen ohne separate technische Analyse;
- keine automatischen Persistenzmigrationen allein aus UX-Gründen;
- keine realen Twitch-/YouTube-Seiteneffekte in Routine-Tests.

---

## 19. Definition of Done – gesamte UI/UX-Überarbeitung

Die Überarbeitung gilt erst als abgeschlossen, wenn:

- [ ] Hauptnavigation dem neuen Produktmodell entspricht.
- [ ] Dashboard innerhalb weniger Sekunden System Health, Attention und Current Activity vermittelt.
- [ ] Auto-VOD/Auto-YouTube-Historie nicht mehr als verwirrende Produktbegriffe dominiert.
- [ ] Manual / Auto Download / Download + YouTube verständlich und technisch korrekt abgebildet sind.
- [ ] Download und Upload parallel korrekt dargestellt werden.
- [ ] Queue Prozesse und VOD-Bibliothek getrennt sind.
- [ ] Find VODs inklusive Yesterday → Today schnell bedienbar ist.
- [ ] Today/Yesterday-Presets lokale Kalendertage korrekt behandeln.
- [ ] Streamer-Verwaltung bei 25+ Streamern gut scanbar ist.
- [ ] wichtige technische Details weiterhin erreichbar sind.
- [ ] Needs-Attention-Zustände klar und handlungsorientiert sind.
- [ ] destruktive Aktionen eindeutig und konsistent abgesichert sind.
- [ ] Browser-Alerts/-Confirms aus regulären Kernflows verschwunden oder begründet verbleiben.
- [ ] 375-px-Mobile-Ansichten ohne unnötigen horizontalen Overflow funktionieren.
- [ ] Keyboard, Fokus und ARIA für neue Komponenten geprüft sind.
- [ ] `prefers-reduced-motion` berücksichtigt ist.
- [ ] relevante automatisierte Tests grün sind.
- [ ] reale Browser-/Workflow-Prüfung für Desktop und Mobile erfolgt ist.
- [ ] Security, Persistenz, Recovery und Dateisicherheit unverändert geschützt sind.
- [ ] README/Screenshots/öffentliche Doku anschließend die neue Produktsprache verwenden.

---

## 20. Aktuell freigegebene Designrichtung

Die gemeinsam betrachteten Mockups gelten als **visuelle Richtung**, nicht als pixelgenaue Implementierungsvorgabe.

Freigegeben sind insbesondere:

- dunkle, hochwertige SaaS-Optik;
- Sidebar mit Dashboard / VODs / Live / Queue / Settings;
- System-Health-Cards;
- Attention Center;
- Current Activity mit separaten parallelen Download-/Upload-Cards;
- kompakter Live-Now-Bereich;
- VOD-Suche mit Toolbar, Presets und Result-Fokus;
- Streamer-Verwaltung als kompakte Liste + Detailbearbeitung;
- mobile, informationsreduzierte Varianten statt bloßem Desktop-Stacking.

Die Mockups enthalten illustrative Daten. Die Implementierung muss ausschließlich reale Daten nutzen, die das aktuelle Backend zuverlässig liefert.

---

## 21. Nächster Schritt

**Slice 1 – Design Foundation** als erster Codex-Auftrag.

Dieser Slice soll bewusst noch keine fachliche Automation-/Persistenzlogik verändern. Er schafft die gestalterische und komponentenseitige Basis, damit Navigation und Screens danach schrittweise ohne erneuten CSS-Wildwuchs umgesetzt werden können.
