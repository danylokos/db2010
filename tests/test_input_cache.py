"""Generated inputs are shared, immutable and authenticated by content."""
import stat

import pytest

from db2010.preparation.cache import cached_input


def test_cache_reuses_content_and_rejects_corruption(tmp_path):
    path = cached_input(tmp_path, b'generated input')
    assert path == cached_input(tmp_path, b'generated input')
    assert path.is_relative_to(tmp_path/'build/inputs')
    assert not path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    assert len(list(path.parent.iterdir())) == 1
    path.chmod(0o644);path.write_bytes(b'changed')
    with pytest.raises(ValueError, match='cached input identity differs'):
        cached_input(tmp_path, b'generated input')
