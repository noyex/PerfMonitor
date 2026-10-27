# Plan: sygnały diagnostyczne (lagi, crashe, dysk, szukanie wydajności)

Status: **zaakceptowany, w realizacji** na gałęzi `feat/diag-signals` (baza: `main` @ e992fcd).

## 0. Kontekst i ograniczenia

**Co już jest na `main`** (nie dublujemy): próbki co 1 s (`samples`: CPU/GPU/RAM/commit/hard faults/dysk latencja+kolejka+busy+per-dysk w `extra.disks`/sieć kb/s/fg_exe/ui_ms/dwm_ms/user_idle), PresentMon per sekunda (`frames`: fps, ft_avg/p99/max, cpu_busy, gpu_busy, display_lat, present_mode, bound) + `stutters`, top procesy co 5 s (`procs` kind cpu/mem/io), dziennik zdarzeń (`events`) z mapowaniem na incydenty w `events.classify` (w tym dysk: `disk`/`Ntfs`/`stornvme`/`storahci`/`volmgr` level ≤ 3 → incydent `disk`), incydenty live (ui_freeze, dwm_stall, app_not_responding, hot_*, vram_full, commit_full), dzienny snapshot `system_info`, kontekst sesji, markery, luki w danych, `pm freeze-check`.

**Równoległe, niezmergowane gałęzie:**
- `feat/net-monitor`: monitor sieci (`perfmon/netmon.py`, `net_samples`, `pm net`, UI). **Sieć nie wchodzi w ten plan.** Wprowadza `events.LOGS` i retencję `net_samples`.
- `feat/uv-oc`: `hw_detail`, per-core, WHEA per rdzeń, `oc_*`, `pm oc`, `pm stress`, ekran „Podkręcanie”, `db.add_columns(con, table, {col: type})`, `detail_*`, `db_guard_gb`, strażnik rozmiaru bazy. **Nie dotykamy tych obszarów.**
- Obie modyfikują `collector.py`, `db.py`, `events.py`, `analyze.py`, `cli.py`, `config.py`, `ui/server.py`. Dlatego: **nowa logika w nowych modułach**, w istniejących plikach tylko małe, addytywne haki. `db.add_columns` identyczne jak na `feat/uv-oc`, `events.LOGS` jak na `feat/net-monitor`. Po zmergowaniu tamtych gałęzi ten branch trzeba zrebase'ować.

**Fakty o maszynie** (2026-10-27): kolektor = zadanie harmonogramu przy logowaniu, Interactive + RunLevel Highest (admin w sesji użytkownika, `RegisterHotKey` działa); PresentMon 2.6.0 (`--track_frame_type`, `--no_track_input`, `--track_app_timing`, `--qpc_time`); `wpr.exe` jest, brak `xperf`, WPA i `cdb`; `GPU Engine(*)` ~790 instancji; `Paging File` tylko przez `AddEnglishCounter` (lokalizacja); `C:\Windows\Minidump` pusty; `%LOCALAPPDATA%\CrashDumps` nie istnieje, `WER\LocalDumps` nieustawiony; `ReportArchive` wymaga admina, część katalogów bez `Report.wer`; `Get-StorageReliabilityCounter` wymaga admina; dziennik Defendera włączony, `TaskScheduler/Operational` wyłączony; shader cache: `%LOCALAPPDATA%\AMD\{DxCache,DxcCache,VkCache,OglCache,DX9Cache}` i `D:\SteamLibrary\steamapps\shadercache\<appid>`; sprzęt: 7700X, RX 9070 XT, 32 GB DDR5, Samsung 990 PRO (C:, D:), Goodram CX400 (E:), Goodram 240 GB (F:), monitory 144 Hz + 59 Hz.

**Testy równoległe:** sesja PresentMon `PerfMon_{session_id}` + `--stop_existing_session` w testowym kolektorze zabija capture produkcyjny → nazwa `PerfMon_{pid}_{sid}`. Testowy kolektor tylko gdy użytkownik nie gra, krótko (`PERFMON_RUN_SECONDS`).

## 1. Schemat (migracje addytywne)

- `samples`: `+ pagefile_pct REAL`.
- `events`: `+ data TEXT` (surowe `EventData` jako JSON lista) dla: Application Error 1000, Application Hang 1002, WER 1001, Kernel-Power 41, stornvme/disk/Ntfs, WHEA, Defender 1000/1001/1116/1117.
- `procs`: `+ gpu_pct REAL, vram_mb REAL`; `kind='gpu'` (top 5 wg obciążenia silników GPU).
- `game_proc` (co 5 s w sesji gry): `ts, session, pid, rss_mb, private_mb, thread_max_pct, threads, handles, io_read_mbs, gpu_pct, vram_dedicated_mb, vram_shared_mb, shader_bytes`, PK `(session, ts)`.
- `proc_events`: `ts, pid, name, ppid, parent_name, action ('start'|'stop'), session, path`; indeks po `ts`.
- `frames`: `+ runtime, sync_interval, tearing, dropped, click_lat, input_lat, frame_gen_pct`.
- `etw_captures`: `id, ts, trigger, session, path, size_mb, status ('captured'|'analyzed'|'failed'), summary`.
- `sessions.summary`: `crash {...}`, `present {...}`, `mem_growth_mb_h`, `vram_shared_max_mb`, `bottleneck {...}`.
- Incydenty: `user_mark`, `nvme_reset`, `disk_retry`, `disk_removed`, `disk_health`, `defender_scan`, `malware`, `etw_capture`, `game_mem_leak`.
- Retencja: `game_proc`, `proc_events` wg `retention_days`; `etw_captures` + pliki wg `etw.retention_days` (14) i `etw.max_gb` (2).
- Config: `hotkey_mark`, `hotkey_beep`, `gpu_proc_interval_s`, `shader_cache_dirs`, `shader_scan_interval_s`, `proc_events_ignore`, `background_culprits`, `wer_local_dumps`, `etw {mode, buffer_mb, dump_last_s, triggers, analyze, analyze_during_game, retention_days, max_gb}`.

## 2. Punkty (każdy = osobny commit, kolejność = priorytet)

- **P1. Znacznik użytkownika** (`perfmon/marks.py`): `pm mark ["opis"]` → `data/mark.request`; kolektor tworzy incydent `user_mark` z kontekstem; hotkey `ctrl+alt+l` (`RegisterHotKey`), beep; sekcja `marks` w raportach; marker w UI; wyzwala ETW (P11).
- **P2. GPU per proces + `game_proc`** (`perfmon/gpuproc.py`): PDH `GPU Engine` / `GPU Process Memory` co 5 s; `procs kind='gpu'`; `game_proc`; przyczyny `vram_shared`, `background_gpu:<name>`; `gpu_background` w raporcie; `pm procs --kind gpu`; seria VRAM gry w UI.
- **P3. Crashe** (`perfmon/crashinfo.py`): `events.data`, `summary.crash` (1000 + 1001 + `Report.wer` + dumpy), `interpret(code, module, exe)`, `pm health` z modułem i kodem, `pm dumps enable|disable|status` (tylko na żądanie), opcjonalne `cdb !analyze -v` dla BSOD.
- **P4. Dysk** (`perfmon/diskinfo.py`): `nvme_reset`, `disk_retry`, `disk_removed`, NTFS z woluminem; przyczyna `disk_reset`; `pagefile_pct` + etykieta `pagefile`; `disk_health` z `Get-StorageReliabilityCounter`; `session_report.disk` per dysk.
- **P5. Procesy + Defender** (`perfmon/procwatch.py`): `proc_events` (diff co 5 s), dziennik Defendera, `defender_scan`, przyczyny `background_start:<name>` / `background_scan:defender`, `session_report.proc_events`, `pm procs --events`.
- **P6. PresentMon 2.6** (`perfmon/presentmon.py`): log nagłówka CSV, `--track_frame_type` z fallbackiem, nowe kolumny `frames`, `summary.present`, findings Composed Flip, `pm compare --by present_mode`. Śledzenie `dwm.exe` poza grami: follow-up.
- **P7. Shadery** (`perfmon/shadercache.py`): rozmiar cache AMD + Steam `shadercache/<appid>` → `game_proc.shader_bytes`; przyczyna `shader_compile`; `first_session_after_change`.
- **P8. Wyciek pamięci gry**: regresja `private_mb`, `mem_growth_mb_h`, incydent `game_mem_leak`.
- **P9. Wąskie gardło + przed/po** (`perfmon/bottleneck.py`): `summary.bottleneck`, `pm compare --exe X --before ... --after ...`, numeracja markerów.
- **P10. UI** (minimalnie): markery, seria VRAM, box Crash, linia wąskiego gardła, `present`, `mem_growth`, chip bottleneck. Zero nowych ekranów.
- **P11. Czarna skrzynka ETW** (`perfmon/etwbox.py`): `wpr` w trybie Memory, zrzuty na wyzwalacze, analiza `tracerpt` (DPC/ISR per sterownik, DiskIo), `pm etw`. Bez instalowania ADK/WPT.

## 3. Kolejność commitów

0. `docs: add diagnostics signals plan`. 1. `fix(presentmon): unique ETW session name per collector process`. 2. `feat(db): add_columns helper and diagnostics schema`. 3–12. P1–P10. 13. `docs(skill): ...` (`perf-analyst/SKILL.md`). 14. P11.

## 4. Testy i odbiór

- Jednostkowo: parsery (`crashinfo`, instancje PDH GPU, `Report.wer`, nagłówki PresentMon, regresja, bottleneck, `tracerpt` XML).
- Integracyjnie: testowy kolektor `PERFMON_DATA_DIR=<tmp> PERFMON_MUTEX=Local\PerfMonTest PERFMON_RUN_SECONDS=600` (tylko gdy użytkownik nie gra), potem `pm status/session/at/health`. Narzut kolektora < 2% CPU bez ETW.
- Przed PR: `/code-review`, poprawki, PR na `main`, sekcja „Decyzje użytkownika”: LocalDumps, ADK/WPT, hotkey.

## Follow-up (poza zakresem)

- Śledzenie `dwm.exe` przez PresentMon poza sesjami gier.
