"""Build a complete W800i FS seed with the minimal customization file set.

Usage: python -m db2010.provision_fs --stock-fs STOCK.bin
       --customization CUSTOM.zip --output READY.bin
"""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import zipfile
import xml.etree.ElementTree as ET

from .paths import ROOT, RESOURCES
from .preparation.inputs import payload
from .preparation.filesystem import Fat16, padded_fs, FS_BASE, FS_SIZE


def identity(raw):
    return {'size_bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def customization_members(archive, xs_pack=None, skip_wap_links=False):
    """Select minimal files or reproduce XS31's basic/hook/region selection."""
    if skip_wap_links and xs_pack is None:
        raise ValueError('--skip-wap-links requires --xs-pack')
    groups = None
    if xs_pack is not None:
        parts = xs_pack.split('/')
        if (len(parts) != 2 or parts[0] != 'W800' or not parts[1].startswith('CDA')
                or not all(c.isascii() and (c.isalnum() or c == '_') for c in parts[1])):
            raise ValueError('XS pack must be W800/CDA... for the supported stock image')
        groups = ['W800/basic'] + ([] if skip_wap_links else ['W800/hook']) + [xs_pack]
    members, names = [], set()
    for member in archive.infolist():
        if member.is_dir():
            continue
        path = PurePosixPath(member.filename)
        if (path.is_absolute() or '..' in path.parts or '\\' in member.filename
                or (member.external_attr >> 16) & 0o170000 == 0o120000):
            raise ValueError('unsafe customization archive path: '+member.filename)
        name = path.as_posix()
        if name.casefold() in names:
            raise ValueError('duplicate customization archive path: '+name)
        names.add(name.casefold())
        members.append((member, path))
    if groups is None:
        selected = [(m, p.as_posix()) for m, p in sorted(members, key=lambda item: item[0].filename.casefold())
                    if len(p.parts) == 4 and tuple(x.casefold() for x in p.parts[:3]) == ('tpa', 'preset', 'custom')]
    else:
        # XS appends group lists in this order, retaining ZIP order within each.
        if not any('/'.join(p.parts[:2]) == xs_pack for _, p in members):
            raise ValueError('XS region is absent from customization archive: '+xs_pack)
        if not any('/'.join(p.parts[:2]) == 'W800/basic' for _, p in members):
            raise ValueError('XS archive lacks W800/basic files')
        selected = [(m, 'tpa/preset/custom/'+p.name) for group in groups for m, p in members
                    if len(p.parts) >= 3 and '/'.join(p.parts[:2]) == group]
    used = {m.filename for m, _ in selected}
    omitted = [m.filename for m, _ in sorted(members, key=lambda item: item[0].filename.casefold()) if m.filename not in used]
    return selected, omitted, groups


def provision(stock_fs, customization, *, xs_pack=None, skip_wap_links=False):
    manifest = json.loads((RESOURCES/'firmware-manifest.json').read_text())
    entry = next(e for e in manifest['entries'] if e['id'] == 'fs')
    original = payload(ROOT, entry, 'fs', None, stock_fs)
    disk = Fat16(padded_fs(original))
    selected = []
    archive_bytes = customization.read_bytes()
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        members, omitted, groups = customization_members(archive, xs_pack, skip_wap_links)
        final, superseded = {}, []
        for member, name in members:
            key = name.casefold()
            if key in final:
                # XS deletes and rewrites each upload. On a fresh image, the last
                # member gives the resulting file bytes without intermediate churn.
                superseded.append(final.pop(key)[0].filename)
            final[key] = member, name
        for member, name in final.values():
            if member.file_size > FS_SIZE:
                raise ValueError('customization member exceeds image capacity')
            raw = archive.read(member)
            if PurePosixPath(name).name.casefold() == 'customize.xml':
                if ET.fromstring(raw).tag != 'customization':
                    raise ValueError('expected customization XML root')
            disk.install(name, raw)
            selected.append({'path': name, **identity(raw), **({'source_member': member.filename} if groups else {})})
    if not any(e['path'].casefold() == 'tpa/preset/custom/customize.xml' for e in selected):
        raise ValueError('customization archive lacks tpa/preset/custom/customize.xml')
    result = disk.render()
    # Reopen the serialized FTL/FAT rather than trusting the writer's cache.
    verify = Fat16(result)
    for member in selected:
        item = verify.find(member['path'])
        if item is None or identity(verify.chain(item['cluster'])[:item['size_bytes']]) != {k: member[k] for k in ('size_bytes', 'sha256')}:
            raise ValueError('serialized customization verification failed')
    report = {'format_version': 1, 'profile': 'w800i-r1l002-minimal-customization-raw-fs',
        'stock_container': {'input_path': str(stock_fs), **identity(stock_fs.read_bytes())},
        'stock_payload': identity(original),
        'customization_archive': {'input_path': str(customization), **identity(archive_bytes)},
        'installed_files': selected, 'omitted_files': omitted,
        'selection': 'Only tpa/preset/custom files; preserve stock drivers and other mounts.',
        'output': {'address': FS_BASE, 'end': FS_BASE+FS_SIZE, **identity(result)},
        'tail': {'fill': 255, 'span_size': 0x40000, 'footer_value': 0x1254,
                 'footer_count': 44, 'physical_contents_established': False},
        'storage_input_derivation': True, 'firmware_code_changes': False,
        'debugger_interventions': []}
    if groups is not None:
        report['profile'] = 'w800i-r1l002-xs31-builtin-customization-raw-fs'
        report['selection'] = 'XS31 basic, optional hook, then region; flatten basenames into tpa/preset/custom.'
        report['xs_selection'] = {'pack': xs_pack, 'skip_wap_links': skip_wap_links,
                                  'groups': groups, 'superseded_members': superseded,
                                  'existing_stock_file_replacement': 'unsupported'}
    return result, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stock-fs', type=Path, required=True, help='authenticated W800i R1L002 FS container')
    parser.add_argument('--customization', type=Path, required=True, help='customization ZIP, or extracted XS31 resource111 ZIP with --xs-pack')
    parser.add_argument('--xs-pack', help='select XS31 built-in pack, e.g. W800/CDA102425_38_EMEA_1')
    parser.add_argument('--skip-wap-links', action='store_true', help="XS31 Don't upload SE WAP links checkbox; requires --xs-pack")
    parser.add_argument('--output', type=Path, required=True, help='fresh output path for the 11 MiB raw FS')
    args = parser.parse_args()
    output = args.output.resolve()
    sidecar = output.with_suffix(output.suffix+'.json')
    if output.exists() or sidecar.exists():
        parser.error('output image or provenance file already exists')
    try:
        raw, report = provision(args.stock_fs.resolve(), args.customization.resolve(),
                                xs_pack=args.xs_pack, skip_wap_links=args.skip_wap_links)
    except (ValueError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        parser.error(str(exc))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(raw)
    report['output']['file'] = output.name
    sidecar.write_text(json.dumps(report, indent=2)+'\n')
    print(f'{output}: {len(raw)} bytes; {len(report["installed_files"])} customization files; {len(report["omitted_files"])} omitted')


if __name__ == '__main__':
    main()
