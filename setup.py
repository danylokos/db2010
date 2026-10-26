"""Include native producer source and QEMU lock in installed provenance resources."""
from pathlib import Path
import shutil
from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self):
        shutil.rmtree(Path(self.build_lib)/'db2010', ignore_errors=True)
        super().run()
        project = Path(__file__).resolve().parent
        target = Path(self.build_lib)/'db2010/_build_resources'
        shutil.copytree(project/'dependencies/qemu', target/'qemu', dirs_exist_ok=True)
        shutil.copytree(project/'native/trace', target/'trace', dirs_exist_ok=True)
        (target/'project').mkdir(exist_ok=True)
        for name in ('pyproject.toml', 'setup.py', 'MANIFEST.in', 'README.md', '.gitmodules'):
            shutil.copyfile(project/name, target/'project'/name)


setup(cmdclass={'build_py': BuildPy})
