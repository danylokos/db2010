"""Explicit unpaced host lifecycle fixtures; normal pacing remains unchanged."""
import pytest


@pytest.fixture
def controlled_clock(monkeypatch):
    # Task133 tracks the normal aligned initial epoch. Keep these host lifecycle
    # controls exact with the same unpaced clock as native migration fixtures,
    # including the independent CLI regeneration process.
    import subprocess
    original = subprocess.Popen
    bootstrap = """
import runpy, subprocess, sys
original = subprocess.Popen
def launch(command, *args, **options):
    if isinstance(command, list):
        command = [arg.replace('shift=6,sleep=on,align=on','shift=6,sleep=off,align=off') for arg in command]
    return original(command, *args, **options)
subprocess.Popen = launch
module = sys.argv[1]
sys.argv = [module, *sys.argv[2:]]
runpy.run_module(module, run_name='__main__')
"""
    def launch(command, *args, **options):
        if isinstance(command, list):
            command = [arg.replace('shift=6,sleep=on,align=on','shift=6,sleep=off,align=off') for arg in command]
            if '-m' in command and command[command.index('-m')+1].startswith('db2010'):
                index = command.index('-m')
                command = [*command[:index], '-c', bootstrap, *command[index+1:]]
        return original(command, *args, **options)
    monkeypatch.setattr(subprocess, 'Popen', launch)
