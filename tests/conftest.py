import os
import tempfile

# 必须在导入 app.* 之前设置，测试用独立的临时数据库
os.environ["GST_DB"] = os.path.join(tempfile.mkdtemp(prefix="gst-test-"), "test.db")
os.environ["GST_DEV"] = "1"

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """限流计数是进程内全局的，每个测试前清空，避免测试之间互相影响。"""
    from app.web import limiter

    limiter.reset()
    yield
