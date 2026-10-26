# W800i cold-boot images

`db2010` cold boots the **provisioned** images by default. Use
`db2010 --non-provisioned` to select the original non-provisioned inputs. Both start
fresh CPU/device state; neither restores a runtime checkpoint. The previous
`--unprovisioned` spelling remains a CLI alias.

All regular images live in `provisioned/` or `non-provisioned/`, with the common
name `w800i-r1l002-cid49-<area>.bin`. Each group has an `images.json` manifest.

## Provisioned — default

| Image | Bytes | SHA-256 |
| --- | ---: | --- |
| w800i-r1l002-cid49-main.bin | 20840448 | 02b488e09aadebc67fa5346ff2d0a3b1379019b23b429027946c04f8eb9de69c |
| w800i-r1l002-cid49-fs.bin | 11534336 | 337433575500653d4af96223f065b8f56d15be3eb042d48de48a1ff1c0751a30 |
| w800i-r1l002-cid49-gdfs.bin | 1048576 | 0f3d9fa732615ca64b32308b1ac680468eaba243f3e54024e72b73f04e511b4f |
| w800i-r1l002-cid49-nand-main.bin | 33554432 | 8e66eaaa540172c62d61138dbf97a7ab1cc2eedd5e73375dd419a3470b11ed96 |
| w800i-r1l002-cid49-nand-spare.bin | 1048576 | 8f084213d7f61fde43afaa0e9f9b52e553debd05117b1e541dc4aabb37e7e694 |

## Non-provisioned — --non-provisioned

| Image | Bytes | SHA-256 |
| --- | ---: | --- |
| w800i-r1l002-cid49-main.bin | 18500536 | 46e09443e9d35fb3a4a3891e235af0b19180501ad9d57f959baf7869fd83fa8d |
| w800i-r1l002-cid49-fs.bin | 11534336 | f0e3aa2d9b9007a87fcb186b812eee43074fd8bb105ed52f238dce57c79150b3 |
| w800i-r1l002-cid49-gdfs.bin | 1048576 | 4118eb7369b5c2e8f8258591bd60c4a3e51eeeb9e6b6c917fb68e59b344a9e46 |

Provisioned images are the exact five NOR/NAND exports from the user's
`w800-configured` run. They retain the native saved language and other settings.
[Provenance](provisioned/provenance.json) pins the source run, original inputs,
engine and hashes. [Original export manifest](provisioned/source-flash.json)
records the source's original filenames and geometry. MAIN code is unchanged;
its export includes the original authenticated payload and erased storage tail.
Unknown cells were exported as FF under the existing storage assumption.
The source run ended at an unmodeled I2C write; this input set adds no response
to that dependency. The default profile is packaged `resources/boot.json`,
research0271, using the same0268 device contracts.

The non-provisioned group retains the original0268 inputs: unchanged extracted
MAIN payload, English-only customization XML in the derived raw FS, and the
native-generated GDFS bank. It has not been through the user's language/settings
configuration. NAND uses the original declared cold model inputs rather than
saved NAND data. It is not a factory handset dump. The FS `.bin.json` and
[GDFS provenance](non-provisioned/gdfs-provenance.json) preserve their derivation.
The packaged `resources/non-provisioned-boot.json` selects these paths while
keeping original code authentication and all guards.

The original source corpus and historical investigations remain in the separate
research repository. K750 images are not bundled.
