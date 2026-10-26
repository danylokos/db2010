"""Render known LCD panel snapshots and preserve their actual DMA update payloads."""
from db2010.diagnostics.artifacts import artifact as artifact_path, read_json, stored_artifact
import argparse
import hashlib
import json
from pathlib import Path
import zlib

from db2010.ui.pixels import png_rgb565


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--byteorder',choices=('big','little'),default='big',
                        help='native format0x110 uses big-endian RGB565; little reproduces old previews')
    parser.add_argument('--from-frame',type=int,default=0,help='first captured transfer index to preserve')
    args = parser.parse_args()
    if args.from_frame < 0:parser.error('--from-frame must be nonnegative')
    result_path = stored_artifact(args.run, 'result.json')
    result = read_json(args.run)
    pdi = result['snapshot']['pdi']
    frames = pdi.get('frames', [pdi.get('frame', {})])
    args.output.mkdir(parents=True, exist_ok=False)
    rendered = []
    for number, frame in enumerate(frames):
        if number < args.from_frame:continue
        raw = bytes.fromhex(frame.get('pixels_hex', ''))
        name = f'frame-{number:04d}'
        update_metadata = {}
        if 'update_hex' in frame:
            update = bytes.fromhex(frame['update_hex'])
            assert len(update) == frame['bytes']
            update_path = name + '.update.rgb565'
            (args.output / update_path).write_bytes(update)
            update_metadata = {'update': update_path, 'update_bytes': len(update),
                'update_sha256': hashlib.sha256(update).hexdigest(),
                'cursor_start': frame['cursor_start'], 'cursor_end': frame['cursor_end'],
                'panel_known_pixels': frame['panel_known_pixels']}

        if len(raw) != 77440 or frame.get('completed', 0) <= frame.get('started', 0):
            if update_metadata:
                rendered.append({**update_metadata, 'frame': number, 'source': frame['source'],
                    'started': frame['started'], 'completed': frame['completed'],
                    'rendered': False, 'reason': 'complete panel contents unknown'})
            continue
        png = png_rgb565(raw, 176, 220,args.byteorder)
        (args.output / (name + '.rgb565')).write_bytes(raw)
        (args.output / (name + '.png')).write_bytes(png)
        rendered.append({**update_metadata, 'frame': number, 'source': frame['source'],
            'started': frame['started'], 'completed': frame['completed'],
            'bytes': len(raw), 'width': 176, 'height': 220,
            'interpretation': f'{args.byteorder}-endian RGB565 panel preview; native format0x110 swaps RGB565 bytes; physical wiring unmeasured',
            'all_zero': not any(raw), 'raw': name + '.rgb565', 'png': name + '.png',
            'raw_sha256': hashlib.sha256(raw).hexdigest(),
            'png_sha256': hashlib.sha256(png).hexdigest()})
    manifest = {'source_run': str(args.run), 'source_result_artifact': str(result_path.relative_to(args.run.resolve())),
        'source_result_sha256': hashlib.sha256(result_path.read_bytes()).hexdigest(),
        'zlib_runtime': zlib.ZLIB_RUNTIME_VERSION, 'frames': rendered}
    (args.output / 'render.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
