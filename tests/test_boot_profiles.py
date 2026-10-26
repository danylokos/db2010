"""Cold-boot selection keeps persisted storage distinct from the original inputs."""
import json

import pytest

from db2010 import cli
from db2010.paths import ROOT, DEFAULT_EXPERIMENT, NON_PROVISIONED_EXPERIMENT
from db2010.preparation.inputs import prepare


@pytest.mark.parametrize('flags,profile', [([], DEFAULT_EXPERIMENT),
    (['--non-provisioned'], NON_PROVISIONED_EXPERIMENT),
    (['--unprovisioned'], NON_PROVISIONED_EXPERIMENT)])
def test_cli_selects_cold_boot_profile(monkeypatch, flags, profile):
    def run(root, experiment, output, **options):
        assert experiment == profile.resolve()
        return {'success': True, 'phases': {}, 'uart': {}}, None
    monkeypatch.setattr(cli, 'run', run)
    monkeypatch.setattr('sys.argv', ['db2010', '--research-guards', *flags])
    cli.main()


@pytest.mark.parametrize('other', [['--restore', 'run'], ['--load-flash', 'run'],
    ['--experiment', 'other.json']])
def test_non_provisioned_rejects_ambiguous_start(monkeypatch, other):
    monkeypatch.setattr(cli, 'run', lambda *args, **options: pytest.fail('launched ambiguous boot'))
    monkeypatch.setattr('sys.argv', ['db2010', '--research-guards', '--non-provisioned', *other])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_both_profiles_prepare_original_code_with_distinct_storage(tmp_path):
    states = []
    for name, profile in [('provisioned', DEFAULT_EXPERIMENT),
                           ('non-provisioned', NON_PROVISIONED_EXPERIMENT)]:
        path, _ = prepare(ROOT, profile, tmp_path/name, recording=False)
        states.append(json.loads(path.read_text()))
    assert states[0]['executable_ranges'] == states[1]['executable_ranges']
    for field in ('nand_main_image', 'nand_spare_image'):
        assert field in states[0]['nand']
        assert field not in states[1]['nand']
    assert states[0]['entry'] == states[1]['entry'] == 0x44020114
