from pathlib import Path
from db2010.paths import ROOT, RESOURCES as PACKAGE_RESOURCES
RESOURCES = Path(__file__).resolve().parents[1]/"resources"
FIRMWARE_MANIFEST = PACKAGE_RESOURCES/"firmware-manifest.json"
BASELINE_MODELS = RESOURCES/"baseline-models.json"
BASELINE = PACKAGE_RESOURCES/"baseline.json"
