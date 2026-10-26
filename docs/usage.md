# DB2010 usage guide

Start with the [README](../README.md) for hardware status, Linux/macOS prerequisites,
build commands and basic runs. All commands here run from the emulator checkout.

## Build and engine pin

For a fresh or updated checkout, run `development.source init` first.
QEMU is pinned in `qemu/` and
[dependencies/qemu/source-lock.json](../dependencies/qemu/source-lock.json);
update both when intentionally changing engines. `development.source init` runs
standard submodule initialization, fetches the locked release tag from the
lock file's upstream when missing, and checks the pin. Existing tags are never
overwritten; a tag pointing to the wrong commit fails verification. Subsequent
`development.source check` and builds verify locally without fetching.

```sh
.venv/bin/python -m db2010.development.source init
.venv/bin/python -m db2010.development.source check
.venv/bin/python -m db2010.development.build --configure --jobs 4
```

QEMU builds under `qemu/build`; producer binaries, immutable generated input
caches, build logs and provenance live under `build/`. `--configure` reruns build
configuration. Both development tools accept `--repo /absolute/emulator/checkout`.

## Boot inputs and execution limits

Plain cold boot uses all five images under `fw/provisioned/`, preserving saved
language/settings from `w800-configured` (research0272). CPU and device state start
fresh. `--non-provisioned` selects the original 0268 inputs in
`fw/non-provisioned/`; `--unprovisioned` remains an alias. Image names follow
`w800i-r1l002-cid49-<area>.bin`. See [firmware provenance](../fw/README.md) for hashes
and derivation; neither group is a complete factory handset dump.

The default and `--non-provisioned` profiles model a constant full battery. Native
firmware reports4196mV and100%; PMIC inputs are synthetic, with no battery drain
or charging simulation. USB/charger status remains disconnected. Other ADC
channels retain their existing declared inputs.

`--non-provisioned` cannot combine with `--restore`, `--load-flash` or
`--experiment`; these select their own starting inputs. K750 preparation contracts
remain available, but K750 firmware is not bundled. Historical numbered profiles,
corpus inputs and investigations belong to the separate research repository.

The CLI and public runner API start with research guards off and no implicit
instruction or wall-clock cap, including when a profile declares research budgets.
Use `--seconds N` for a bounded default run, or Ctrl-C to stop. Add
`--research-guards` for experiments, tests and model development: it enables
original-code, execution coverage and RAM byte-validity checks. Only this guarded
mode supports `--limit N`. Peripheral protocols, storage coverage, supported CPU
contracts and host bounds remain checked in both modes. Fast execution uses the
existing RAM backing without asserting that its bytes were established by firmware.

The Python API uses `research_guards=False, config_limits=False`; research
callers opt in explicitly and may use `config_limits=True` to apply frozen
experiment budgets. Direct QEMU uses `-machine db2010,...,research-guards=on|off`,
with off as its default. Selection is fixed for that process.

Progress and final JSON expose `counter_kind`: `qemu-icount` samples native QEMU
instruction accounting in default mode; `research-observations` retains guarded
count semantics. Neither is a cycle/retirement measurement. Both modes retain
shift6 timing and native idle advancement. `--json-summary` prints final status
on stderr; `--no-progress` disables terminal progress.

Checkpoints require the same guard mode on save and restore. Historical
checkpoints have guards on, so restore them with `--research-guards`.
`--allow-engine-mismatch` does not bypass this requirement. Reset preserves the
startup selection; use a new process to change it.

## Browser and controls

`--ui` serves loopback port 8765; `--ui 0` picks a free port and prints its URL.
After a bounded stop, the final browser view stays open until Ctrl-C. The browser
needs WebGL2; bundled assets require no frontend build. For a remote host:

```sh
ssh -N -L 8080:127.0.0.1:8765 HOST
```

Open `http://localhost:8080` locally while the remote emulator runs with `--ui`.

Click/hold handset keys, drag to rotate, and scroll/pinch to zoom. Firmware
determines the action of each ideal contact. Audio starts once browser autoplay
permits it or a user gesture unlocks it.

| Keyboard | Handset contact |
| --- | --- |
| Arrow keys / Enter | Joystick directions / select |
| Esc / Backspace | Back / C (clear) |
| Digits, `*`, `#` | Phone keys |
| Shift+Left / Shift+Enter / Shift+Right | Left soft key / Walkman / right soft key |
| Page Up / Page Down | Volume up / down |
| Space | Play/pause |
| C / Shift+C | Camera full / half press |
| P | Power |

## Serial and file transfer

The default serial profile supports the browser's native OBEX file browser and
transfers. `--uart` displays firmware bytes without changing that mode.
`--uart-debug` selects the existing debug-cable inputs and streams verbose output;
it is mutually exclusive with OBEX transfer and requires a cold boot. Restoring
preserves the checkpoint's serial mode; use `--uart` to display it. All four UART
streams remain available in browser observations and saved diagnostics.
The browser renders UART 2 (`FC030000`) as a hex dump with byte offsets and
printable ASCII; the other ports display text. Offsets count from the start of
the captured stream, including when only the latest 64 KiB is retained.

Known cold-start transfer/service limitations remain. Successful transfer depends
on firmware readiness and acceptance of its native dialogs; the modeled UART
transport alone does not establish that every transfer path works.

## Diagnostics and traces

Ordinary runs save no artifacts. Saving flags create a fresh labeled run under
`artifacts/` (or `--artifacts-dir`):

```sh
.venv/bin/python -m db2010 --diagnostics --snapshot --save-flash \
  --research-guards --limit 100000000 --seconds 120 --label boot
.venv/bin/python -m db2010 --trace --research-guards --limit 200000 --seconds 30 --label trace
.venv/bin/python -m db2010.diagnostics.verify artifacts/trace
```

`--diagnostics` retains state, logs, raw UART and source/build/input provenance.
`--trace` saves instruction and successful bus-access Parquet streams; rejected
accesses remain explicit model stops. `--disassemble` enables tracing and adds
decoded instructions. `--record-audio` saves PCM/WAV and packet metadata
independently of browser audio. `--label` requires an artifact-saving flag.

## Checkpoints and flash persistence

```sh
.venv/bin/python -m db2010 --restore boot --seconds 30
.venv/bin/python -m db2010 --load-flash boot --seconds 30
```

These examples use the `boot` run saved above. `RUN` can be a label or directory.
Checkpoints (`--snapshot`) preserve CPU/device execution state; flash exports
(`--save-flash`) preserve committed storage bytes for a new cold boot. Restore
checks engine/schema/model identity. `--allow-engine-mismatch` is an explicit
exploratory override retaining native schema/model checks. Restore cannot combine
with image/experiment overrides or `--uart-debug`; flash loading cannot combine
with explicit image inputs. Saving and restoring state does not patch firmware.

## GDB

```sh
.venv/bin/python -m db2010 --research-guards --debug --gdb-command 'break *0x44020f50' \
  --gdb-command continue --gdb-interactive --diagnostics --seconds 30
```

Debugger selection does not change research mode; the example explicitly enables
guards for a firmware probe. `--debug` starts ARM-capable GDB (`gdb-multiarch` by default; override with `--gdb`).
Repeat `--gdb-command COMMAND` or `--gdb-script FILE` in execution order;
`--gdb-interactive` retains a prompt after supplied actions. Debugger
interventions are labeled separately from normal boot evidence. A forced path
alone does not establish correct peripheral behavior.

## Checkout and installed-package paths

Editable installs default to their emulator checkout. Wheels default to the
working directory; pass `--repo /absolute/emulator/checkout` to select the engine,
caches and default artifact directory explicitly. Firmware remains in the
checkout rather than duplicated in the wheel. `--input-root DIR` separately
resolves relative image references for external research configurations; generated
caches always remain under the selected emulator's `build/`.

`--main`, `--fs`, `--gdfs`, `--nand-main` and `--nand-spare ` accept explicit image
paths. Default inputs are hash authenticated; explicit raw storage overrides are
caller-supplied data, and MAIN executable bytes must still authenticate.

## Testing

```sh
.venv/bin/python -m playwright install chromium firefox
.venv/bin/python -m pytest tests
.venv/bin/python tests/check_layout.py
.venv/bin/python -m pip wheel --no-deps --no-build-isolation .
```

Core tests use bundled inputs and compact controlled fixtures/oracles. Native
checks require built QEMU and the trace plugin; browser tests require the pinned
Playwright browsers. `check_layout.py` exercises a separate checkout and wheel
with explicit repository paths, trace integrity, checkpoint restore and flash
reload. Corpus-dependent integration tests live in the research repository.

## Troubleshooting

| Symptom | Check / action |
| --- | --- |
| Missing Python modules or unsupported syntax | Use Python 3.14+ and the checkout's venv; install `-e '.[dev]'` there. |
| Missing engine/release tag, source-pin mismatch or stale build | Run `development.source init`, then `development.build --configure`. |
| QEMU built, then trace plugin compilation failed | Check the reported `build/provenance/trace/<timestamp>/build.log`. On macOS the plugin uses bundle linking with QEMU API symbols resolved at load time. |
| Browser port already in use | Use `--ui 0` and open the printed URL. |
| Remote browser cannot connect | Forward the loopback port with SSH as above. |
| No browser graphics or audio | Check WebGL2 support; interact with the page to unlock audio. |
| Terminal run exits but browser remains | The final view stays available by design; Ctrl-C closes it. |
| No saved run directory | Enable an artifact-saving flag such as `--diagnostics`; `--label` alone is insufficient. |
| Checkpoint rejected | Use its matching engine/schema/model; the engine override is exploratory and cannot bypass schema/model checks. |
| OBEX transfer unavailable | Use default serial mode, wait for native readiness and accept handset dialogs; cold-start limitations remain. |
| Unsupported access, protocol or executable-byte stop | Inspect diagnostics and the declared profile; this is a model boundary requiring evidence, not a reason to bypass guards. |

## Hardware details

The [README table](../README.md#hardware-support) describes supported paths.
Unknown inputs, unsupported protocols and changed original executable bytes stop
execution. Mock responses are not measured handset hardware. The complete
accumulated contracts and assumptions are in
[boot.json](../src/db2010/resources/boot.json), with initial memory/CPU setup in
[baseline.json](../src/db2010/resources/baseline.json). Earlier assumption notes
record historical stages; the active profile fields and pinned source determine
current behavior.

### Profiles, parts and service limits

Intel L18 NOR (`89/880D`) and Samsung NAND (`EC/35`) are configured compatible
profiles, not verified fitted packages. NOR completion is synchronous, with
synthetic identity/protection fields and guarded command/query behavior. NAND has
65,536 pages of 512 main + 16 spare bytes (32 MiB +1 MiB), known-cell read/program,
erase and copy-back paths, immediate-ready assumptions and no wear model. The
firmware's logical NAND storage range `0x50000000–0x51FFFFFF` is not a linear CPU
array mapping: byte accesses use the controller ports below.

W800 service documents identify LCD assembly **RNH 942 29** and PMIC **Vincenne2
A07, ROP 101 3066/2**. Firmware identifies the **R61500_B** display profile; this
is not a physical controller-ID measurement. PDI/DMA feeds retained 176×220 GRAM
rendered as RGB565. PMIC slave `0x4A` uses scoped register transactions/latches and
synthetic battery, cable and ADC inputs; the sibling-part ID hypothesis and
analog/timing assumptions are not W800 measurements. FM slave `0x10` accepts only
the declared write frame; there is no tuning or radio audio. Wire write addresses
are `0x94` and`0x20` respectively; the README uses 7-bit bus addresses.

Timers/RTC use declared virtual-clock/reset assumptions (32768 Hz and calendar
epoch 2000-01-01). Native firmware masks, acknowledgements and ARM exception entry
drive connected IRQ paths. Contacts are ideal matrix/GPIO/power inputs; physical
power removal is absent. Memory Stick is an empty-slot hypothesis via GPIO42.

The SIM peer supplies dummy GSM files through native byte/IRQ transport, without
subscriber authentication. Bluetooth supplies minimal startup acknowledgements,
a synthetic address and zero ACL/SCO capacities, then scoped failures; no
functional Bluetooth service is available. The DSP peer supplies scoped FA
mailbox/DMA/audio and modular-exponentiation services without executing DSP code.
C55x is an ISA candidate supported by firmware analysis, not a confirmed installed
part number. Host MP3/AAC-LC output works on supported paths; equalizer controls
lack EQ arithmetic, and full audio/video synchronization is not established.
The MIDI service consumes the firmware's timed ring1 events and renders40 global
triangle voices at48kHz through the same browser PCM path. All instruments and
percussion use the same pitched triangle; note velocity, channel volume (CC7)
and a default two-semitone pitch bend are supported. Note-off uses a64ms release.
Other controllers, SysEx/long events and instrument-bank uploads remain guarded.
Pause, stop, native looping and active voice/clock checkpoint state are supported.
QuadraPop gameplay music loops; its menu currently issues rapid stop/restart
requests, which remain a separate Java/player-state investigation. Its WAV
effects are a separate unsupported source contract.
Camera capture, USB enumeration/transport and cellular registration/calls remain
unmodeled. Camera focus/shutter contacts do not imply
image capture support; USB startup latches do not imply a USB controller model.

### Sparse register inventory

All addresses below are CPU physical. Offsets are hexadecimal and relative to
the stated base; ranges include endpoints. Register starts are listed separately
from byte coverage. Widths, allowed values, access directions and lifecycle rules
remain guarded; an inventory entry does not authorize arbitrary reads/writes.

| Block / base | Selected register starts and widths | Source of contract |
| --- | --- | --- |
| NAND `0x50000000` | Byte ports: `00` data, `08` address, `10` command, `20/24` gates; remaining aperture holes guarded | [NAND map](../qemu/hw/arm/db2010-nand.c) |
| DMA `0xF2000000` | Words `00–34` stride 4; eight channels at `100 + 20*n`, each `00/04/08/0C/10` | [DMA map](../qemu/hw/arm/db2010-dma.c) |
| Timer `0xF9010000` | Words `00/04/08/1C` | [Timer/IRQ map](../qemu/hw/arm/db2010-timer.c) |
| IRQ `0xFB020000` | Banks `100/200/300`: words at `00/04/10/18`, level words `1C–98` stride 4 | [Timer/IRQ](../qemu/hw/arm/db2010-timer.c), [profile latches](../src/db2010/resources/boot.json) |
| RTC `0xF9050000` | Halfwords `00/02/04/06/08/0C/0E/10/12/14/16/18/1C/1E/20`; low-byte retained fields accessed as halfwords at every start `2A–42`; halfwords `4A/4C/4E/50/52/56/58/5E`; marker `54` supports byte/halfword | [RTC](../qemu/hw/arm/db2010-rtc.c), [retained bank](../qemu/hw/arm/db2010-rtc-retained.c), [power contact](../qemu/hw/arm/db2010-simple.c), [profile latches](../src/db2010/resources/boot.json) |
| PDI `0xF7000100` | Words `00–64` stride 4 (program), `68–7C` stride 4 (data), `8C/90/DC/E0/F4/F8/FC`; stream words at `0xF7000300/0304/0308` | [PDI](../qemu/hw/arm/db2010-pdi.c), [display](../qemu/hw/arm/db2010-display.c) |
| Matrix `0xFB010000` | Halfwords `00/02` | [Keypad](../qemu/hw/arm/db2010-keypad.c) |
| GPIO `0xF9000000` | Halfwords `00/02/04/06/0A/0C/0E/10/14/16/18/1A/1E/20/22/24/28/2A/2C/2E/32/34/3C/40/44/48/6C`; includes PWM latches | [Profile](../src/db2010/resources/boot.json), [input modifiers](../qemu/hw/arm/db2010-register-bank.c) |
| I²C `0xF9040000` | Halfwords `00/02/04/06`; all other offsets in 4 KiB aperture guarded | [I²C map](../qemu/hw/arm/db2010-i2c.c) |
| UART0 `0xFB060000` | Halfwords `00/02/04/06/08/0A/0C/0E/10/12/14/16/18/1A/1C/1E/20/22/24/26/2C/2E` | [UART/data maps](../qemu/hw/arm/db2010-simple.c), [profile](../src/db2010/resources/boot.json) |
| UART1 `0xFC040000`, UART4 `0xFB040000` | Halfwords `02/0A/12/2C` | [UART map](../qemu/hw/arm/db2010-simple.c), [profile](../src/db2010/resources/boot.json) |
| UART3 `0xFC030000` | Halfwords `00/02/04/06/08/0A/0C/0E/12/14/16/18/1A/1C/1E/20/22/24/26/2C/2E` | [Bluetooth/UART maps](../qemu/hw/arm/db2010-simple.c), [profile](../src/db2010/resources/boot.json) |
| SIM `0xF9060000` | Bytes `00/02/04/06/08/0C`; `0A` and remaining aperture holes guarded | [SIM interface](../qemu/hw/arm/db2010-simif.c) |
| Memory Stick `0xFC010000` | Halfwords `08/0C/10/2C`; GPIO42 input at `0xF9000028`, direction at `0xF900002A` | [Profile](../src/db2010/resources/boot.json), [slot input](../qemu/hw/arm/db2010-register-bank.c) |
| FA `0xFA040000` | Words `00/04/08/0C/10/14/18/1C/24/28/2C`; DMA data port `20`; TX halfwords `40–7E` stride 2, RX halfwords `80–BE` stride 2 | [Mailbox](../qemu/hw/arm/db2010-fa-mailbox.c), [transfer](../qemu/hw/arm/db2010-fa-transfer.c), [DSP peer](../qemu/hw/arm/db2010-dsp-peer.c), [DMA links](../qemu/hw/arm/db2010-transfer-links.c), [profile](../src/db2010/resources/boot.json) |
| USB `0xFD000000` | Halfwords `04/0C/18/20/28/30`; `80–FC` stride 4, all startup captures/latches | [Profile](../src/db2010/resources/boot.json) |

This table covers the README's peripheral surfaces, not every board setup latch.
Additional clock, memory-controller, F500 and system-control contracts remain in
the profile and [board wiring](../qemu/hw/arm/db2010-bus-wiring.c).

### Synthetic ROM services and GDFS overlays

These are guarded host substitutions at selected ROM call entries, **not MMIO**
or recovered ROM instructions. The original firmware call executes; the host
validates its caller/context before supplying the declared service. ROM timing
cost is unspecified (zero modeled).

| Service | Entry PCs | Declared behavior |
| --- | --- | --- |
| SHA | Init `0xFFFF0F48`, update `0xFFFF112C`, final `0xFFFF129E` | SHA-1 with the selected little-endian 92-byte context contract |
| KGEN | Init `0xFFFF12A2`, update `0xFFFF12BE`, final `0xFFFF12C2` | Synthetic SHA-1-compatible service; not recovered hardware key generation |
| ETX | `0xFFFF0DDC` (Thumb call pointer `0xFFFF0DDD`) | Exact static GDFS request/status-success substitution; not cryptographic verification |

See [entry guards](../qemu/hw/arm/db2010-research.c) and
[service implementation](../qemu/hw/arm/db2010-rom.c). Two separate 20-byte NOR
read overlays at `0x45F00A2E–0x45F00A41` and `0x45F00BFA–0x45F00C0D` supply declared
GDFS digest replacements without changing backing storage or executable code.
Expected/replacement bytes and scope are explicit in `nor_model.diagnostic_read_overlay`
in [boot.json](../src/db2010/resources/boot.json).

### Supporting research

Detailed evidence and retained manual identities live in the separate
[research repository](https://github.com/danylokos/db2010-research), rather than
being runtime dependencies. Relevant reports distinguish firmware declarations,
static analysis, observations and model assumptions:

- [NOR identity/profile](https://github.com/danylokos/db2010-research/blob/main/docs/investigations/nor-id-query.md)
  and [NAND ports/profile](https://github.com/danylokos/db2010-research/blob/main/docs/investigations/nand-access.md).
- [LCD module and R61500 firmware profile](https://github.com/danylokos/db2010-research/blob/main/docs/investigations/lcd-controller-reference.md)
  and [PMIC service-part identity](https://github.com/danylokos/db2010-research/blob/main/docs/investigations/pmic-hardware-identity.md).
- [DSP modeled surface](https://github.com/danylokos/db2010-research/blob/main/docs/investigations/dsp-modeled-surface.md)
  and [C55x candidate analysis](https://github.com/danylokos/db2010-research/blob/main/docs/investigations/dsp-c55x-decoder.md).
- [Bluetooth startup acknowledgements and limits](https://github.com/danylokos/db2010-research/blob/main/docs/investigations/bluetooth-startup-fix.md)
  and [synthetic SIM contract](https://github.com/danylokos/db2010-research/blob/main/docs/investigations/dummy-sim.md).
