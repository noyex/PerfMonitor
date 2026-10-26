# Plan: dane do undervoltingu i podkręcania (CPU + GPU)

Status: **do akceptacji** (nic z tego nie jest jeszcze zaimplementowane).

## 1. Co faktycznie da się odczytać (zrzut `pm sensors dump`, admin, 2026-10-27)

| Obszar | Dostępne | Brak / bezużyteczne |
|---|---|---|
| CPU 7700X (LHM) | zegar per rdzeń + **zegar efektywny per rdzeń**, mnożnik, moc per rdzeń (SMU), moc Package (≈ PPT), Tctl, CCD1 Tdie, obciążenie per wątek, bus clock | **VID per rdzeń = śmieci** (0,03–0,12 V, błąd LHM na Zen 4), brak limitów PPT/TDC/EDC, brak telemetrii SVI3 |
| Płyta (Super I/O ITE IT8689E) | **Vcore**, Vcore SoC, Vcore Misc, temp. VRM MOS i VSoC MOS, PCH, wentylator CPU, +12V/+5V/+3,3V | wentylatory obudowy = 0 (niepodłączone do płyty?) |
| RAM | temperatury obu DIMM, timingi SPD | rzeczywiste timingi (EXPO) |
| GPU 9070 XT (LHM) | zegar core, zegar pamięci, **napięcie core**, moc Package, temp. core / hotspot / VRAM, wentylator RPM, obciążenie | zegar efektywny, limit mocy, aktywny offset napięcia |
| Adrenalin | w rejestrze tylko presety auto-tuningu (`PP_AutoUVV2VoltageOffset`, `PP_AutoODActive`, `PP_AutoOCEngineClock`) | ręczny profil tuningu nie leży w rejestrze jawnie |

Wnioski:
- „Clock stretching” liczymy jako `zegar efektywny / zegar referencyjny` per rdzeń przy obciążeniu rdzenia > 90%.
- Napięcie CPU: Vcore z Super I/O (to napięcie z płyty, nie per rdzeń) — wystarczy do krzywej V/F i porównań profili.
- Moc CPU: Package z SMU ≈ PPT. TDC/EDC tylko z dodatkowym źródłem (patrz decyzja 1).
- GPU: aktywny profil Adrenalin spróbuję odczytać przez **ADL Overdrive8** (`atiadlxx.dll` przez ctypes: power limit, max zegar, offset napięcia, VRAM). Jeśli RDNA4 tego nie zwróci → profil wpisywany ręcznie + wykrywanie zmian po presetach z rejestru i skoku napięcia/zegara.

## 2. Próbkowanie i rozmiar bazy

- Nowa tabela `hw_detail(ts, session, cpu_eff[8], cpu_clk[8], cpu_core_w[8], thread_load[16], cpu_vcore, cpu_vsoc, cpu_pkg_w, tctl, tccd, vrm_temp, vsoc_temp, dimm_temp, gpu_clk, gpu_mem_clk, gpu_v, gpu_w, gpu_temp, gpu_hot, gpu_mem_temp, gpu_fan)` — tablice jako JSON.
- **Gęstość:** w sesji gry / testu obciążeniowego co **1 s**, poza nimi co **10 s**.
- Szacunek: ~600 B/wiersz → ~4 h gry dziennie ≈ 9 MB + pulpit ≈ 4 MB → **~13 MB/dzień**. Retencja szczegółów **30 dni (~400 MB)**, potem zostają tylko agregaty.
- Agregaty per sesja (`oc_session_stats`: średnie/p95 zegarów, napięć, mocy, temp., FPS/W, stretching per rdzeń) — trzymane bez limitu, to one służą do porównań profili.
- Do głównej tabeli `samples` dochodzą tylko 4 tanie kolumny: `cpu_vcore`, `gpu_voltage`, `vrm_temp`, `cpu_eff_max`.

## 3. Stabilność

- **WHEA-Logger 19** (corrected machine check): parsowanie APIC ID → rdzeń (`APIC ID / 2` na 7700X z SMT), bank MCA → typ (rdzeń / L3 / kontroler pamięci → raczej EXPO/SoC niż CO). Nowy incydent `whea_corrected` z numerem rdzenia.
- WHEA 18 (fatal), WHEA 17 (PCIe), resety sterownika (Display 4101 / amdkmdag), crashe gier, Kernel-Power 41 z oznaczeniem „przy niskim obciążeniu” (ostatnie próbki: CPU < 15%, typowy objaw za agresywnego CO na biegu jałowym).
- Metryka: **błędy na godzinę** (pracy / gry / testu) per profil, osobno per rdzeń dla WHEA 19.

## 4. Rejestr profili OC/UV

- `oc_profiles(id, name, created_ts, status: testing|stable|rejected, cpu JSON, gpu JSON, notes)`
  - cpu: `co_per_core[8]`, `pbo: {ppt, tdc, edc, scalar, boost_override}`, `expo`, `soc_v`
  - gpu: `voltage_offset_mv`, `power_limit_pct`, `max_clock_offset`, `vram_mhz`, `vram_fast_timing`, `fan_curve`
- `oc_profile_log(ts, profile_id, source: manual|detected)` — okresy aktywności; sesje dostają `profile_id` przy starcie.
- CLI: `pm oc list | show ID | add ... | activate ID | edit ID | compare [--exe X] | stability [--profile ID]`.
- UI: formularz profilu + przycisk „Aktywuj od teraz”. (Serwer UI jest dziś tylko do odczytu — dojdzie jeden endpoint POST do profili.)
- Wykrywanie zmian: GPU (ADL / rejestr / skok napięcia przy tym samym zegarze) → incydent „wykryto zmianę ustawień GPU — oznacz profil”. CPU (BIOS) nie da się wiarygodnie wykryć → ręcznie.

## 5. Testy obciążeniowe jako sesje

Lista `stress_exes` w konfiguracji: OCCT, y-cruncher, Prime95, Cinebench, CoreCycler (Prime95 z katalogu CoreCycler), 3DMark, FurMark, Kombustor. Sesje z `kind='stress'`; PresentMon tylko dla testów 3D. Wynik testu: czas, błędy WHEA w trakcie, maks. temp., moc, stretching.

## 6. Analizy

- **Porównanie profili** (tylko ta sama gra): FPS, 1% low, temp. CPU/GPU/hotspot, moc CPU+GPU, **FPS/W**, błędy/h, liczba sesji i minut (z ostrzeżeniem przy zbyt małej próbie).
- **Krzywa V/F**: GPU — mediana napięcia w koszykach zegara co 50 MHz pod obciążeniem; CPU — Vcore vs maks. zegar efektywny (1-wątek i all-core osobno). Porównanie krzywych między profilami.
- **Clock stretching per rdzeń** i WHEA per rdzeń → który rdzeń ma za mocny CO.

## 7. Ekran „Podkręcanie”

Minimalnie: aktywny profil + status stabilności (błędy/h od aktywacji) → tabela porównania profili → wykres V/F → per-rdzeń (WHEA, stretching). Edytor profilu w panelu bocznym.

## 8. Skill perf-analyst — playbook UV/OC

Zen 4 (CO per rdzeń, PBO limity, EXPO/SoC), RDNA4 (undervolt w Adrenalin, power limit, VRAM), procedura krok po kroku z testami (CoreCycler, y-cruncher, OCCT, gry), interpretacja WHEA 19 / resetów przy niskim obciążeniu. **Zasada: agent nigdy sam nie zmienia ustawień OC — tylko doradza i ocenia dane.**

## 9. Kolejność wdrożenia (atomowe commity)

1. czujniki + `hw_detail` + próbkowanie zależne od trybu + retencja
2. WHEA 19/18 + parsowanie APIC → rdzeń, „reset przy niskim obciążeniu”
3. profile + `pm oc` + tagowanie sesji
4. sesje testów obciążeniowych
5. analizy (porównanie profili, V/F, FPS/W, stretching) + API
6. ekran „Podkręcanie”
7. playbook w skillu

## Decyzje do podjęcia

1. **TDC/EDC i telemetria SVI3 CPU**: LHM ich nie daje. Opcje: (a) bez nich — Package ≈ PPT + Vcore z płyty (domyślnie, zero instalacji); (b) doinstalować AMD Ryzen Master i czytać przez jego Monitoring SDK (PPT/TDC/EDC z limitami, napięcia rdzeni).
2. **Retencja szczegółowych danych**: 30 dni (~400 MB) czy inaczej?
3. Profil GPU: wystarczy, że spróbuję ADL i w razie porażki zostanie wpis ręczny?
