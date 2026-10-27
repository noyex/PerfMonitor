# Plan: dane do undervoltingu i podkręcania (CPU + GPU)

Status: **do akceptacji** (nic z tego nie jest jeszcze zaimplementowane). Moje propozycje do decyzji: sekcja „Rekomendacje” na końcu.

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

## 10. Dodatkowe propozycje

### 10.1 Najpierw baza, potem UV/OC
- Zanim cokolwiek zmienisz: profil **„Stock”** (ustawienia BIOS jak teraz + EXPO) jako punkt odniesienia — min. ~10 h gry i kilka dni pracy. Bez tego nie da się powiedzieć, czy UV/OC coś poprawiło lub zepsuło.
- **Nie zaczynać UV/OC, dopóki trwają twarde resety i zawieszenia DWM** (historia 20–27.10). Niestabilność OC wyglądałaby identycznie i zamazałaby diagnozę. Kolejność: najpierw ustalić przyczynę zawieszeń (eksperymenty z monitorami / MPO / sterownikiem), potem tuning.
- Pierwszy eksperyment po stronie sprzętu, jeśli resety wrócą: profil **„EXPO off”** (JEDEC 4800) na kilka dni — najtańszy test, czy resety to pamięć/SoC.

### 10.2 Kolejność tuningu (do playbooka)
1. **GPU undervolt w Adrenalin** (offset napięcia, potem power limit, na końcu VRAM) — najbezpieczniejsze, cofa się jednym kliknięciem.
2. **CPU Curve Optimizer** per rdzeń (start −10 all-core, potem per rdzeń wg stretchingu i WHEA 19).
3. **PBO limity** (PPT/TDC/EDC, scalar, boost override) — tylko jeśli temperatury i moc na to pozwalają.
4. **RAM / SoC** na końcu i osobno (najtrudniej wykryć błędy, najgroźniejsze dla danych).
- Jedna zmiana naraz, każda jako nowy profil w `pm oc`.

### 10.3 Kryteria „stabilny”
Profil dostaje status `stable` dopiero gdy łącznie:
- 0 × WHEA 19/18, 0 × reset sterownika GPU, 0 × Kernel-Power 41, 0 × crash gry niewyjaśniony innym powodem,
- ≥ 10 h gry + ≥ 3 dni zwykłej pracy (w tym bezczynność — za mocny CO często wysypuje się na biegu jałowym),
- zaliczone: CoreCycler (min. 1 h/rdzeń dla CO), y-cruncher, OCCT (CPU + VRAM dla GPU), 3DMark Steel Nomad stress (GPU).
`pm oc stability` liczy te warunki i mówi, czego jeszcze brakuje.

### 10.4 Test bezczynności i ręczne sesje testowe
- `pm stress start "nazwa"` / `pm stress stop` — sesja testu bez znanego exe (np. benchmark wbudowany w grę, test ręczny).
- Rodzaj sesji `idle_soak`: oznacza okres celowej bezczynności (1–2 h) — do wyłapywania resetów przy niskim obciążeniu.

### 10.5 Progi i alerty (incydenty)
- CPU: Tctl ≥ 95 °C, VRM MOS ≥ 100 °C, **Vcore SoC > 1,30 V** (górna granica bezpieczeństwa Zen 4), stretching > 3% przy pełnym obciążeniu rdzenia.
- GPU: hotspot ≥ 105 °C, VRAM ≥ 100 °C, nagły spadek zegara przy tym samym obciążeniu.
- Alert tylko gdy przekroczenie trwa ≥ 5 s (bez szumu).

### 10.6 Rozmiar bazy
- Retencja szczegółów + **cotygodniowy `incremental_vacuum`** (bez blokowania kolektora na długo).
- Strażnik: jeśli `perf.db` > 2 GB → incydent ostrzegawczy i automatyczne skrócenie retencji szczegółów do 14 dni.
- `pm status` pokaże rozmiar bazy.

### 10.7 FPS/W
Liczone z mocy **CPU Package + GPU Package** (czujniki, bez reszty systemu i strat zasilacza) — dobre do porównań profili, nie do rachunku za prąd. Pokazywane tylko dla tej samej gry i porównywalnej długości sesji.

## Rekomendacje do decyzji (moje propozycje)

1. **TDC/EDC i telemetria SVI3 — bez Ryzen Master (na razie).** Ryzen Master instaluje własny sterownik jądra; w trakcie szukania przyczyny zawieszeń/resetów nie chcę dokładać zmiennej. Package ≈ PPT + Vcore z płyty + stretching + WHEA 19 per rdzeń wystarczą do Curve Optimizera. Wrócimy do tego, jeśli CO per rdzeń okaże się trudne do dostrojenia.
2. **Retencja szczegółów 30 dni (~400 MB)**, agregaty sesji bez limitu, strażnik 2 GB (10.6).
3. **Profil GPU: próba ADL Overdrive8, w razie porażki wpis ręczny** + wykrywanie zmian po presetach z rejestru i skoku napięcia przy tym samym zegarze → incydent „oznacz profil”.
4. Wdrożenie zacznę od kroków 1–2 z sekcji 9 (czujniki + stabilność) — dają wartość od razu, także dla diagnozy obecnych resetów — a profile/ekran dopiero potem.
