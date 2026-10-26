"""Offline evidence verification and optional independent snapshot comparison."""
import argparse
import hashlib
import json
from pathlib import Path

import pyarrow.parquet as pq

from .artifacts import artifact, files, relative_artifact, read_json, load_run, verify_seal, verify_external_inputs

from .trace import SCHEMA
from .memory import SCHEMA as MEMORY_SCHEMA
from .integrity import ordinals, memory_identity, reference_instructions


def verify(root, reference=None):
    if (artifact(root, 'retention.json')).exists():
        raise ValueError('bulk-pruned run: use db2010.diagnostics.retention verify for retained integrity; regenerate for full trace replay')
    manifest = load_run(root)
    seals = verify_seal(root)
    if "external_inputs" in manifest:
        verify_external_inputs(manifest["external_inputs"])
    if 'flash' in manifest['families'] and manifest.get('flash'):
        from ..execution.flash import load_flash_images
        load_flash_images(root)
    if 'diagnostics' in manifest['families']:
        result = read_json(root)
    else:
        result = {**manifest['outcome'], **manifest.get('trace', {}),
                  'trace_enabled': 'trace' in manifest['families']}
    if reference:
        load_run(root, family='diagnostics')
        load_run(reference, family='diagnostics')
    if result.get('debugger') and result['debugger'] != json.loads((artifact(root, 'debug-session.json')).read_text()):
        raise ValueError('debug session/result provenance differs')
    rows = None
    if result['trace_enabled']:
        parquet = pq.ParquetFile(artifact(root, 'trace.parquet'))
        if not parquet.schema_arrow.equals(SCHEMA, check_metadata=True):
            raise ValueError('trace schema differs')
        rows = parquet.metadata.num_rows
        if rows != result['collector']['observations']:
            raise ValueError('collector/Parquet count differs')
        if result['success'] and rows != result['producer']['observations']:
            raise ValueError('producer/Parquet count differs')
        ordinals(parquet, 'index', 'trace ordinal integrity differs')
        if reference:
            old = pq.ParquetFile(artifact(reference, 'trace.parquet'))
            if old.metadata.num_rows != rows:
                raise ValueError('reference observation count differs')
            reference_instructions(parquet, old)
        if 'memory_collector' in result:
            memory = pq.ParquetFile(artifact(root, 'memory.parquet'))
            count = memory.metadata.num_rows
            if not memory.schema_arrow.equals(MEMORY_SCHEMA, check_metadata=True):
                raise ValueError('memory schema differs')
            if count != result['memory_collector']['events']:
                raise ValueError('memory collector/Parquet count differs')
            if result['success'] and count != result['memory_producer']['events']:
                raise ValueError('memory producer/Parquet count differs')
            ordinals(memory, 'event', 'memory ordinal integrity differs')
            if result['success']:
                memory_identity(artifact(root, 'trace.parquet'), artifact(root, 'memory.parquet'))
    if reference:
        expected = read_json(reference)['snapshot']
        if expected != result['snapshot']:
            raise ValueError('reference CPU/device stop state differs')
    return {'sealed_artifacts': len(seals), 'parquet_rows': rows, 'integrity': 'passed',
            'run_success': result['success'], 'reference_snapshots_and_state': 'exact' if reference else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--reference', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.run.resolve(), args.reference.resolve() if args.reference else None), indent=2))


if __name__ == '__main__':
    main()
