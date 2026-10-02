from pathlib import Path


def test_worker_has_telethon_async_socks_dependency():
    requirements = Path("requirements.worker.txt").read_text().splitlines()
    assert "python-socks[asyncio]==3.1.1" in requirements
