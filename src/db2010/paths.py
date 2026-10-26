"""Paths inside a standalone emulator checkout (or explicit --repo for wheels)."""
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
PROJECT = PACKAGE.parents[1]
ROOT = PROJECT if (PROJECT/'pyproject.toml').is_file() else Path.cwd()
RESOURCES = PACKAGE/'resources'
DEFAULT_EXPERIMENT = RESOURCES/'boot.json'
NON_PROVISIONED_EXPERIMENT = RESOURCES/'non-provisioned-boot.json'
DEPENDENCIES = (PROJECT/'dependencies/qemu' if (PROJECT/'dependencies/qemu/source-lock.json').is_file()
                else PACKAGE/'_build_resources/qemu')
TRACE_SOURCE = (PROJECT/'native/trace/trace.c' if (PROJECT/'native/trace/trace.c').is_file()
                else PACKAGE/'_build_resources/trace/trace.c')


def qemu_source(root=ROOT):
    return (Path(root)/'qemu').resolve()


def emulator_build(root=ROOT):
    return Path(root)/'build'
