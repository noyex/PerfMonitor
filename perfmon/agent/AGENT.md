# NX Frame: guide for AI agents

NX Frame is a Windows gaming-PC monitor: a background collector stores samples, game sessions, incidents and event-log data in a local SQLite database. This guide tells an AI agent with a terminal how to query it and analyze lags, stutters, freezes, resets and crashes.
The CLI path is given in the user's prompt. Wherever this guide says `pm <command>`, run that executable (PowerShell: `& "<path>\pm.exe" <command>`; in a source checkout it is `pm.cmd` in the repo root). It works from any cwd.

Rules: answer in the user's language (the app UI is Polish, so default to Polish); code and commands stay as-is. Base every claim on data: cite exact timestamps and numbers. NEVER change system settings (BIOS, drivers, registry, services, uninstalls) without asking first - propose, then wait.

## Data
- The collector writes 1 s samples, game sessions (PresentMon frames/stutters), network quality, top processes, Windows event log, detected incidents and config snapshots to a SQLite database (UTC epoch seconds; the CLI prints local time). The CLI finds the database itself; `pm status` shows its location. Installed builds keep data in `%LOCALAPPDATA%\NX Frame`.
- Add `--json` for JSON. Time args: `2026-01-15 23:30`, `23:30` (today), `2h` / `30m` / `7d` (that long ago - write WITHOUT a leading minus), `now`.
- The user can browse the same data in the NX Frame window: charts, sessions, incidents, "Co zrobić" (advice).
- `game_proc` (every ~5 s per game session): rss_mb, private_mb, thread_max_pct (busiest game thread, % of one core), threads, handles, io_read_mbs, gpu_pct, gpu_3d_pct, vram_dedicated_mb, vram_shared_mb, shader_bytes (shader cache size, where readable).
- `proc_events`: ts, pid, name, ppid, parent_name, action start|stop, session, path.
- `events.data`: raw event fields as JSON (crash 1000/1001/1002, Kernel-Power 41, disk/stornvme/Ntfs, WHEA, Defender).
- `frames` per-second columns: runtime, sync_interval, tearing, dropped, click_lat, input_lat, frame_gen_pct.
- `etw_captures`: id, ts, trigger (user_mark|dwm_stall|ui_freeze|dpc_pct|stutter_burst), session, path, size_mb, status captured|analyzed|failed, summary JSON (`dpc_top` / `isr_top` per driver: count, sum_us, max_us; `disk_top` per process).
- `sessions.summary` keys: `crash` {module, exception_code, interpretation{verdict, confidence, next_steps}}, `present` {runtime, modes, sync_interval, tearing_pct, click_lat_avg, input_lat_avg, frame_gen_pct}, `bottleneck` {kind vram|gpu_power|cpu_thread|cpu|limit|gpu|mixed, why, metrics}, `mem_growth_mb_h` + `mem_growth_r2`, `vram_shared_max_mb`.
- `frames.bound` can be NULL when PresentMon reports no GPUBusy; the bottleneck then falls back to GPU load >= 95% (`bound_source: gpu_load`).
- Stutter cause tags: `vram_shared` (VRAM spilling into RAM), `disk_reset` (NVMe reset / I/O retry +-15 s), `pagefile` (pagefile > 60% and > 200 hard faults/s), `background_start:<exe>` (culprit started just before), `background_gpu:<exe>` (> 15% GPU in background), `background_scan:defender`, `shader_compile` (shader cache grew > 256 KB around the stutter; used instead of shader_or_unknown), plus disk, vram, ram, cpu_thread, alt_tab, dpc, thermal.
- Newer incident kinds: `user_mark`, `nvme_reset`, `disk_retry`, `disk_removed`, `disk_health`, `defender_scan`, `malware`, `etw_capture`, `game_mem_leak`.

## Commands
- `pm status` - is the collector alive, latest sample, open game session. Run first.
- `pm health [--days 14]` - overview: incidents by kind/day, unclean shutdowns with parsed Kernel-Power 41 fields, recurring app crashes, config changes, pattern findings. Run second.
- `pm advise` - ranked concrete changes from the user's own data, plus `not_worth` and `unknown`. Start here for "co wyłączyć / co poprawić".
- `pm sessions [--limit N]` - game sessions with fps_avg/1% low/stutters/driver/game version.
- `pm session ID` - full session report: bound share (GPU/CPU/limit), FPS stats, stutters with cause tags, temps/clocks/VRAM/RAM peaks, events, findings, network section.
- `pm regress [--exe X] [--limit N]` - per-game FPS / 1% low / stutters / latency vs earlier sessions + what changed.
- `pm incidents [--since 7d] [--kind K]` - kinds: ui_freeze, dwm_stall, app_not_responding, unclean_shutdown, app_crash, app_hang, gpu_driver, whea, whea_corrected, whea_fatal, whea_pcie, throttle, disk, hot_*, vram_full, commit_full, reboot, config_change, bsod, low_memory; network: net_down, net_link_down, packet_loss, latency_spike, game_lag, wifi_weak.
- `pm net [FROM [TO]] [--since 24h]` - network quality: per target (router, operator hop, internet, game server) avg/p50/p95/p99/max ms, jitter, loss %; DNS; seconds without link; Wi-Fi signal/BSSID/channel changes and disconnects (with reasons); NIC errors; network incidents with `where`. `pm session ID`, `pm range` and `pm at` include the same data.
- `pm events [--since 24h] [--level 2] [--grep TEXT] [--limit N]` - Windows event log (level 1 critical, 2 error, 3 warning).
- `pm range FROM [TO]` - aggregated stats (avg/p95/max), incidents, errors, top CPU/IO processes, sessions in a time range.
- `pm at TIME [--window 30]` - second-by-second samples, processes, events around one moment ("what happened right before the freeze").
- `pm mark ["text"]` - user mark "something was wrong now" (the app also has a global hotkey while playing); stored as incident `user_mark` with +-30 s context.
- `pm procs FROM [TO] [--kind cpu|mem|io|gpu]` - top processes in a range (`gpu` = background processes by GPU engine load / VRAM); `pm procs FROM --events` = process starts/stops (proc_events).
- `pm sysinfo [--diff]` - latest config snapshot (drivers, BIOS, RAM, disks, settings) or diff of the last two.
- `pm compare [--exe cod.exe] [--by week|day|month|driver|game_version|session|present_mode] [--since 90d]` - FPS, 1% low, stutters/min, bound shares per group with delta % vs the previous group. `present_mode` groups by IFlip / FSE / Composed / mixed.
- `pm compare --exe X --before <time|m<n>> [--after <time|m<n>>]` - before/after groups around a change marker (`m<n>` = number from `pm markers --exe X`); warns `small_sample` (< 30 min or < 3 sessions in a group).
- `pm markers [--exe X] [--since T]` - timeline of driver / game update / OS / BIOS / config changes, markers numbered `n`.
- `pm test start NAME [--exe X] [--key K]`, `pm test stop|cancel|list|show [ID]` - one changed setting, before/after verdict (pomogło, pogorszyło, mieszane, bez zmian, za mało danych). Needs >= 30 gameplay minutes and >= 2 sessions per side. Only one open test.
- `pm freeze-check` - LIVE diagnosis, run while the desktop is frozen (takes up to ~25 s): times DwmFlush (does DWM still compose frames?), pings explorer, foreground window hung?, GPU 3D load per adapter, display modes + which GPU drives each monitor, top CPU processes, dwm.exe/explorer.exe start times (restarts), collector's latest sample, incidents/events of the last 10 min, and a verdict.
- `pm sensors dump` - every hardware sensor right now (read by the elevated collector).
- `pm collector reload` - restart the running collector (no UAC needed).
- `pm dumps status|enable|disable` - WER LocalDumps minidumps for crashing apps (off by default; enable only if the user agrees).
- `pm etw list|show ID|analyze ID|start|stop` - ETW black-box kernel trace captures (needs the elevated collector; `start`/`stop` only when the user asks).
- `pm schema` - tables and columns; `pm sql "SELECT ..."` - read-only ad-hoc SQL (SELECT/WITH only, 500 rows max); `pm config` - effective collector config, game list.
- FPS, bottleneck and compare use gameplay seconds only (`summary.gameplay`). Stutter causes are judged against normal seconds (`stutters.cause_ranking`: confirmed, weak, ambient, noise). A tag marked `ambient` is a constant condition: never call it the cause of stutters, a before/after test decides. Tag `disk` means disk latency > 30 ms only; hard faults alone are normal streaming.

## Playbook
1. `pm status` (is data being collected? admin=False means fewer sensors), then `pm health`.
2a. Freeze HAPPENING NOW (user reports a frozen screen; you can still run commands): run `pm freeze-check` immediately, then again after ~1 min to see if it is stuck or recovering. Read it as: DwmFlush blocked/>=2000 ms + explorer OK + GPU 3D ~0% = DWM / driver present path hung (graphics stack, not CPU/RAM/disk); explorer timeout too = shell/user-mode hang; foreground hung only = that app. Then `pm at now --window 60` and `pm events --since 15m --level 3`. Recovery steps to PROPOSE (ask first, one at a time, note the result of each): Win+Ctrl+Shift+B (graphics driver reset - must be pressed physically, injecting it programmatically may not work), close overlay/wallpaper/capture apps, unplug/replug or switch off a secondary monitor, restart the browser's GPU process, from an elevated shell `taskkill /f /im dwm.exe` (Windows normally relaunches DWM; apps may lose window state - ask first); last resort is a clean `shutdown /r /t 0` instead of a hard reset (a clean reboot keeps logs and the DB intact). Record the freeze start time for later analysis.
2. Freeze while the OS stayed alive: `pm incidents --kind ui_freeze` (also dwm_stall, app_not_responding), then `pm at <time> --window 30`. Look at ui_ms/dwm_ms (>=2000 ms = freeze), fg_exe/fg_hung, GPU load/clocks collapsing, disk_lat_ms, ram/commit, top procs, and events (Display 4101, GPU driver service, 1002 hangs) in the window.
3. Hard reset / unexpected shutdown (Kernel-Power 41, EventLog 6008): read fields in `pm health`. BugcheckCode=0 means no BSOD: power cut, hang followed by reset/power button, or instant reboot. SleepInProgress != 0 means it happened during a shutdown/sleep/hibernate transition. PowerButtonTimestamp != 0 means the power button was pressed (user forced it). Then check the last samples before the gap (`pm at <time of last_sample>`; `reboot` incident = where data stopped): temps, GPU/CPU load, ui_ms rising before the end? If samples were flat and healthy until the cut, suspect power/hardware (PSU, VRM, RAM XMP/EXPO profile, iGPU conflict); if ui/dwm stalled first, suspect the GPU driver. Also check events 1074, 10001 (apps blocking shutdown), WHEA, Kernel-Boot 29 (fast startup failures).
4. Low FPS: `pm sessions`, `pm session ID`. GPU-bound majority = normal; lower resolution/settings/upscaler. CPU-bound = background processes or CPU-heavy settings. High `limit` share = FPS cap / vsync / driver frame limiter (Radeon Chill/FRTC, NVIDIA Max Frame Rate).
5. Stutters: use cause tags in `pm session ID`. Dominant `shader_or_unknown` = shader compilation/streaming in the game itself.
6. Crashes: app_crash/app_hang incidents; events 1000 (Application Error), 1002 (Hang), 1001 (WER report with module). Group by app; check if it is a game, a launcher or a vendor/OEM service.
7. Thermal: `pm range` / `pm session`: CPU > 90 C or GPU hotspot > 100 C is a concern; >= 95 / 105 is throttling territory; `throttle` incidents (Kernel-Processor-Power 37).
8. Correlate onset of problems with `config_change` incidents and `pm sysinfo --diff` / `pm markers` (driver, BIOS, Windows update, game patch).
9. "Did FPS drop after the driver/game update?": `pm markers --exe <exe>` to get the change date, then `pm compare --exe <exe> --by driver` (or `--by game_version`) and `--by week`; compare fps_avg, fps_1low, stutters_per_min and delta %. Only compare similar content (same game/mode) and enough minutes per group (>30 min); say when the sample is too small. Confirm with `pm session ID` for the sessions around the change. The first session after a change stutters from shader compilation (finding "Pierwsza sesja po zmianie…") - compare the next session instead.
10. User mark: `pm at <time> --window 30` for context, then `pm etw list` / `pm etw show ID` for the capture taken right after it (DPC/ISR per driver, disk I/O per process).
11. The "Wąskie gardło" line in `pm session` ("kind (why) -> advice") says what to tune: gpu_power -> undervolt / power limit helps; cpu_thread -> CPU clock, RAM profile, CPU tuning (not more cores); limit -> limiter/vsync/network/disk, hardware tuning won't help.
12. `game_mem_leak` incident = game memory leak: propose restarting the game, updating it, disabling mods/overlays.
13. `Composed` presentation > 20% of frames = frames go through DWM (overlays, borderless window, MPO): suspect DWM stalls, especially with mixed monitor refresh rates.

## Network
- Table `net_samples` (1 row/s, see `pm schema`): `gw_*` = router, `isp_*` = first operator hop, `inet_*` = 1.1.1.1 + 8.8.8.8, `game_*` = game server (when detected and it answers ICMP); `*_ms` NULL = lost or not probed, `*_loss` 0..1 (NULL = not probed); `dns_ms` every 30 s; `link` wifi | ethernet | NULL (no route = disconnected); `wifi_signal` %, `wifi_rssi` dBm; `session` = game session id; `extra` JSON with only changed fields (gateway, isp_hop, adapter, link_mbps, wifi_ssid/bssid/channel/phy/rx_mbps/tx_mbps, game_ip, nic_errors, dns_failed). Wi-Fi events (WLAN-AutoConfig 8000 start, 8001 connected, 8002 failed, 8003 disconnected with ReasonCode/Reason; ReasonCode 3 = user switched network) are in `events`.
- Incident `where` (in details JSON): `home` = Wi-Fi/router/local network (the gateway already loses or delays packets), `isp` = operator (router fine, first operator hop bad), `internet` = beyond the operator, `game` = only the game server path is bad while general internet is fine. net_down = no internet (3 s of total loss), net_link_down = adapter disconnected, packet_loss >= 5% over 20 s, latency_spike = ping well above its baseline, game_lag = game server ping/loss up, wifi_weak = signal < 40%.
- Network lag vs local stutter: `pm session ID` -> `net.correlation` counts stutters within +-2 s of a lossy/spiking network second and the share expected by chance (`chance_pct`). Network lag: ping/loss spikes (especially `game_*`), game_lag/packet_loss incidents, rubber-banding, while FPS and frametime stay smooth (no frames/ft_max spikes, CPU/GPU/disk normal). Local stutter: frametime spikes with cause tags while ping stays flat. Many stutters near net problems (well above chance) = network. A latency spike during high `net_rx_kbs` (a download) = saturated line, not an operator fault. Locate with `where`: gw loss/jitter and low Wi-Fi signal or BSSID roaming -> Wi-Fi (suggest a cable); gw fine but isp bad -> operator; only game bad -> server/route. Loss to a target further out than the first lossy hop is usually the same fault.
- The network monitor has no data before it was deployed; an empty `net` section means that, not a healthy network.

## UV/OC (undervolting / overclocking) - advise only
RULE: never change BIOS / GPU-driver tuning / vendor tuning tools / registry OC settings yourself, and never tell the user a profile is stable without `pm oc stability` saying so. You only advise and judge the data.

Data: `hw_detail` (per-core effective vs reference clock, per-core power, per-thread load, Vcore/VSoC/VMisc from the Super I/O, package W, Tctl, CCD, VRM/VSoC MOS, DIMM temp, GPU clock/V/W/temps; availability depends on CPU/board; every 1 s in game/stress sessions, every 10 s otherwise, kept 30 days); `oc_session_stats` per-session aggregates kept forever; `gpu_od_snapshots` = GPU tuning read via the driver (on AMD: ADL Overdrive8, named fields vram_mhz, power_limit_pct, zero_rpm, fan_curve; unknown raw `od8[i]` changes are shown as such). Per-core VID from LibreHardwareMonitor is unreliable on Zen 4 - never use it. There is no TDC/EDC/SVI3 telemetry.
Commands:
- `pm oc list|show ID` - profiles (CO per core, PBO, EXPO, VSoC, GPU offset/PL/clock/VRAM) and when each was active.
- `pm oc add NAME [--co -10,-15,... | --co-all -10] [--ppt/--tdc/--edc/--scalar/--boost] [--expo on|off] [--soc-v 1.2] [--gpu-uv -60] [--gpu-pl 10] [--gpu-clk N] [--vram 2700] [--vram-fast on] [--activate [--since 30d]]`, `pm oc edit ID ...`, `pm oc activate ID [--since TIME]` (back-dating re-tags sessions; only run write commands when the user asked to).
- `pm oc next` - one tuning step with value, stability gate and rollback. Read-only.
- `pm oc compare [--exe X]` - per profile for ONE game: FPS, 1% low, FPS/W (CPU+GPU package power only), power, temps, errors/h; delta % vs the oldest profile; `small_sample` < 30 min = say the result is indicative only.
- `pm oc stability [--profile ID]` - criteria: 0 WHEA 19/18, 0 GPU driver resets, 0 Kernel-Power 41/BSOD, 0 game crashes (check if explained), >= 10 h gaming, >= 3 days of work, >= 2 h idle, CoreCycler >= 8 h, y-cruncher >= 1 h, OCCT >= 1 h, 3DMark (Steel Nomad stress) >= 20 min, all without errors. Lists what is missing and resets at low load.
- `pm oc vf [--profile ID]` - GPU V per 50 MHz under load, CPU Vcore vs effective clock (single-thread / all-core). Compare curves between profiles: same clock at lower V = the undervolt is active.
- `pm oc cores [--profile ID]` - per core: WHEA 19/18 count, clock stretching % under full load, effective clocks; `suspect` = needs a weaker (less negative) CO.
- `pm stress start NAME` / `pm stress idle` / `pm stress stop` / `pm stress list` - manual test sessions (built-in game benchmarks, idle soak 1-2 h to catch low-load crashes). Known stress exes (OCCT, y-cruncher, Prime95, CoreCycler, Cinebench, 3DMark, FurMark, Kombustor) become `kind='stress'` sessions automatically.
- Incidents: `whea_corrected` (WHEA 19, title has the core), `whea_fatal` (18), `whea_pcie` (17), `clock_stretching`, `vsoc_high` (> 1.30 V), `hot_vrm_temp` (>= 100 C), `gpu_clock_drop`, `gpu_settings_change` (GPU driver settings changed - ask the user to tag a profile), `db_size`.

Interpretation (CPU items are for AMD Ryzen; on Intel use the equivalent core-voltage/offset signs):
- WHEA 19 with unit=core on core N: Curve Optimizer (CO) on core N too aggressive -> raise it by 3-5 (toward 0). unit=l3: also CO / Vcore. unit=soc (UMC, Infinity Fabric): EXPO / VSoC / FCLK, not CO.
- Kernel-Power 41 with `low_load` (CPU < 15 % before the reset): typical of a too negative CO at idle - but NOT if resets already happened before any UV/OC began (that is a separate problem).
- Stretching > 3 % at full core load = the core lacks voltage (CO too negative) even without errors.
- GPU: driver reset / game crash after a GPU undervolt -> reduce the offset by 10-20 mV. Lower V at the same clock + same FPS + lower W = good undervolt.

Procedure (propose one step at a time, each change = new profile, one change at a time):
0. Baseline first: profile "Stock" (current BIOS + memory profile, e.g. `pm oc add Stock --expo on --activate --since <data start>`), >= 10 h gaming + a few days. Do NOT start UV/OC while unexplained hard resets / DWM freezes still happen - OC instability would look the same. If resets return, first test the memory profile off (JEDEC speed) for a few days.
1. GPU undervolt in the GPU vendor's tuning tool (on AMD Radeon: Adrenalin voltage offset, e.g. -30 mV then steps of -10..-20; on NVIDIA: a voltage/frequency curve or power limit in a tool like MSI Afterburner), then power limit, VRAM last. Test: games + 3DMark Steel Nomad stress + OCCT VRAM.
2. On AMD Ryzen, CPU Curve Optimizer: start -10 all-core, then per core using `pm oc cores` (stretching, WHEA 19). Test: CoreCycler >= 1 h per core, y-cruncher, idle soak, normal use.
3. On AMD Ryzen, PBO limits (PPT/TDC/EDC, scalar, boost override) only if temps (Tctl < 90 C in games) and VRM allow.
4. RAM / SoC last and separately (hardest to detect, riskiest for data). Never VSoC > 1.30 V.
Mark a profile `stable` only when `pm oc stability` says the criteria are met.

## Thresholds
- 1% low < 60% of average FPS = uneven frametimes. Stutters > 2/min noticeable, > 6/min bad.
- disk_lat_ms > 30 or hard_faults_s > 200 = disk/pagefile pressure. VRAM > 95%, RAM > 90%, commit > 92% of limit = exhaustion.
- cpu_core_max > 95 with gpu_load < 85 = single-thread CPU limit. dpc/interrupt > 5% = driver problem.
- ui_ms / dwm_ms >= 2000 = desktop freeze.
- Network: loss >= 1% is noticeable in online games, >= 5% bad; jitter > 10 ms or p99 > 3x median = unstable; router p95 > 15 ms over Wi-Fi = interference/range; Wi-Fi signal < 40% weak.
- Game memory leak: `mem_growth_mb_h` > 500 with `mem_growth_r2` > 0.8.

## Exception codes
- 0xC0000005 ACCESS_VIOLATION: bad pointer - game/driver/overlay bug, or unstable RAM/CPU/GPU OC/UV if the faulting module moves between crashes.
- 0xC0000409 STACK_BUFFER_OVERRUN / fail-fast: often a deliberate abort by the game or anticheat.
- 0xC0000374 heap corruption.
- 0xC000001D illegal instruction: unstable CPU OC/CO or wrong CPU features.
- 0x80000003 breakpoint: assert in game code.
- 0xE06D7363 C++ exception: unhandled game exception.
- Faulting module amdxc64.dll / atidxx64.dll / amdvlk64.dll = AMD driver; nvwgf2umx.dll / nvlddmkm.sys / nvoglv64.dll = NVIDIA driver; igdumdim64.dll / igd10iumd64.dll = Intel graphics driver. Same module repeatedly = software; different modules = suspect hardware instability.

## Optimization checklist (propose, do not apply without asking)
Memory XMP/EXPO profile on and stable (test a lower speed if WHEA/resets); Resizable BAR (AMD: Smart Access Memory) enabled in BIOS and the GPU driver; latest chipset and GPU drivers (clean install via DDU if gpu_driver incidents); newer BIOS/AGESA/microcode; with a CPU iGPU plus a discrete GPU, consider disabling the iGPU in BIOS (only if all monitors are on the discrete GPU); remove board-vendor bloat (tuning/RGB/LCD services, control centers) that crashes or runs constantly; GPU-driver background recording/replay (e.g. Adrenalin Instant Replay, ShadowPlay) off if it eats GPU video-engine time; HAGS on/off test; Windows Game Mode on; power plan Balanced/High performance (no USB/PCIe power saving); Fast Startup off (resets during shutdown); pagefile system-managed on the fastest SSD; games on the fastest SSD; limit background apps (launchers, overlays, OneDrive, browsers); check PSU and cooling if resets continue without BSOD.

## Report format
Short summary in the user's language: what is wrong (with timestamps/numbers), likely cause with confidence level, ranked next steps. State what the data cannot show (e.g. no samples before collector start, no PresentMon data without admin).
