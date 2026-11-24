"""测试全局夹具。

最重要的一条：把 data_dir 指到 pytest 的临时目录。
否则跑一轮测试就会往仓库里写一堆项目目录，本地数据也被污染。
"""

from __future__ import annotations

import pytest

from app.core.config import settings


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    return tmp_path / "data"
