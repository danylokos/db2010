# Maintained runtime inputs

These resources are versioned emulator dependencies. They must remain usable when
research runs and derived analysis are removed. Historical source identifiers in
provenance describe evidence; the runner never follows them as file dependencies.

uart-debug-registers.json copies exactly the two UART0 register contracts from
0255 (FB06000A guest-established readback and FB060012 initializer capture) that
the0260 data transport replaced. `--uart-debug` reinstates them alongside the
verified RX0-high/RX1-low inputs, with no new response or reset-state assumption.
The effective prepared state and run manifest retain this cold-boot override;
the original experiment stays frozen and regeneration repeats the option.

The manifest preserves the MAIN/FS container identities and all 344 record
mappings for each image. The baseline preserves the direct-MAIN initial register,
memory and executable-range contract. The five configured cold-boot images are bundled under checkout fw/provisioned/.

The direct-MAIN entry is read from authenticated firmware, not the baseline:
the observed ARM reset vector `ldr pc,[pc,#0x18]` at MAIN offset0 selects the
little-endian handler literal at offset0x20. Preparation requires a word-aligned
handler inside supplied MAIN code; sparse K750 gaps and expanded W800 storage
tails are excluded. Unsupported vector layouts remain guarded. Container inputs
are unpacked into their mapped records before reading the vector.

execution-contract.json promotes the already authenticated original-code copy
mappings retained in the sealed 0234-mma-fresh-normal-v2 run. It preserves both
approved source/destination ranges and their exact byte hashes, plus chronological
copy provenance. Expected bytes are regenerated from authenticated MAIN; they do
not seed RAM. QEMU still requires known guest-written instruction bytes and stops
on changed, unknown or unapproved execution. This records an emulator execution
contract, not a new measurement of handset behavior.

boot.json is the standalone default, copied from verified research0271: exact
w800-configured NOR/NAND exports with the same0268 device contracts. Native
saved language/settings persist in storage; no CPU/device checkpoint is restored.
Research owns numbered configurations and its relative current.json pointer. The
authored customize-minimal-english.xml supplies the one-file English-only,
provider-free package. The A0 vibration latch removes the PMIC guard exposed
by this input; customize-minimal-english-vibrator.xml retains0267's historical
profile workaround.
The XML resources are self-contained; their deterministic host ZIP/FS generation
and native evidence are documented in the root minimal-customization investigation.
0234 and0235 remain explicitly selected research profiles. Other historical experiments
remain with the research repository; test-only profiles belong in tests/resources.

MAIN storage preparation authenticates the original payload, then pads short
inputs with known erased FF to 0x45400000 (20,840,448 bytes). Expanded raw MAIN
inputs may supply tail data, while original code bounds and copy proofs remain
unchanged. Raw NAND page/spare overrides establish all supplied bytes and keep
cold controller/bookkeeping defaults; flash/ images contain no runtime state.

The separate k750-r1db001-cid49 baseline/manifest and experiment0269 authenticate
cataloged K750 MAIN/FS records without flattening sparse gaps. The prefix profile
excludes RAM execution;0270 adds only the authenticated36008-byte original-copy
contract, without seeding RAM. Both leave GDFS/tails unknown. These profiles are
exploratory; their numbered configurations and firmware inputs stay in research.

`non-provisioned-boot.json` selects original0268 storage under `fw/non-provisioned/`
for the explicit `--non-provisioned` CLI option. Default boot uses provisioned images.
