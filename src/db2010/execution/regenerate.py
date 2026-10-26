"""Regenerate a continued run from its referenced starting checkpoint."""
import argparse
from pathlib import Path

from ..diagnostics.artifacts import load_run
from ..paths import ROOT
from .runner import run


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run',type=Path)
    parser.add_argument('--repo',type=Path,default=ROOT)
    args=parser.parse_args()
    source=args.run.resolve()
    manifest=load_run(source)
    families=manifest['families']
    result,error=run(args.repo.resolve(),Path('unused'),replay=source,
        artifacts_dir=source.parent,label=source.name+'-regenerated',
        save_flash='flash' in families,record_audio='audio' in families,diagnostics='diagnostics' in families,trace='trace' in families,snapshot='snapshot' in families,
        disassemble=manifest.get('trace',{}).get('collector',{}).get('disassembly',False),
        instruction_limit=manifest['limits']['instruction_limit'],seconds=manifest['limits']['seconds'],
        allow_engine_mismatch=manifest.get('checkpoint_compatibility', {}).get('allow_engine_mismatch', False),
        config_limits=False,bounded_history=True,
        research_guards=manifest.get('research_guards', True))
    if error: raise SystemExit(f'regeneration failed: {error}')
    print(result['run_directory'])


if __name__ == '__main__':
    main()
