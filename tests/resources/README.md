# Maintained test inputs

Profiles are frozen model fixtures selected from the numbered research profiles;
GDFS references use the external firmware copy rather than derived run output.
The original identifiers describe provenance, not runtime file dependencies.
`baseline-models.json` preserves the pure device model declarations from the
retired direct-MAIN baseline. No retired CPU adapter is used.

`power-init-writes.json` preserves
the original MAIN startup-write sequence for controlled fixture setup.

The adjacent support modules are maintained native-QEMU fixture helpers copied
from the research tooling, with archived CPU/reference paths removed. The pure
Python oracles have exact original source hashes in ../oracles/provenance.json.

`audio/video-first-16-aac.bin` retains the first16 actual raw AAC-LC access units
from the original Demo Tour.mp4, each wrapped in the recovered native4488E864
eight-byte header and zero halfword alignment. Its adjacent JSON authenticates
source media, encoded bytes and continuous fixed-point FAAD2/independent3:1
resampling output. The QEMU fork pins unchanged upstream code and its license;
these compact test inputs do not require parent research artifacts at runtime.
