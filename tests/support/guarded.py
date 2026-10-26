"""Explicit guarded entry point for native regression/modeling tests."""
from db2010.execution import runner


def run(*args, **options):
    options.setdefault('research_guards', True)
    options.setdefault('config_limits', True)
    result, failure = runner.run(*args, **options)
    if result.get('capture_baseline') is not None:
        assert result['capture_baseline']['research_guards'] is options['research_guards']
    return result, failure
