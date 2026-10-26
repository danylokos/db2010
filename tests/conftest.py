"""Keep the pinned Firefox inspector usable for worker WebSocket tests."""
import json
import os
from pathlib import Path

import pytest


@pytest.fixture(scope='session',autouse=True)
def firefox_worker_inspector():
    old=os.environ.get('NODE_OPTIONS')
    hook=Path(__file__).parent/'support/playwright_worker_ws.cjs'
    option='--require='+json.dumps(str(hook.resolve()))
    os.environ['NODE_OPTIONS']=((old+' ')if old else '')+option
    try:
        yield
    finally:
        if old is None:os.environ.pop('NODE_OPTIONS',None)
        else:os.environ['NODE_OPTIONS']=old
