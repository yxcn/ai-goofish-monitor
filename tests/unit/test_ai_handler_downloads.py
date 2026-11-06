import asyncio
from pathlib import Path

import src.ai_handler as ai_handler


def test_download_all_images_runs_with_concurrency(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_handler, "IMAGE_SAVE_DIR", str(tmp_path / "images"))

    active_downloads = 0
    max_active_downloads = 0

    async def fake_download(url, save_path):
        nonlocal active_downloads, max_active_downloads
        active_downloads += 1
        max_active_downloads = max(max_active_downloads, active_downloads)
        await asyncio.sleep(0.02)
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        Path(save_path).write_text("ok", encoding="utf-8")
        active_downloads -= 1
        return save_path

    monkeypatch.setattr(ai_handler, "_download_single_image", fake_download)

    async def run():
        return await ai_handler.download_all_images(
            "product-1",
            [
                "https://example.com/1.jpg",
                "https://example.com/2.jpg",
                "https://example.com/3.jpg",
            ],
            task_name="demo",
            concurrency=3,
        )

    paths = asyncio.run(run())
    assert len(paths) == 3
    assert max_active_downloads == 3


# -- 单张下载：整段在线程里跑、大小上限、MIME --

import threading

import requests


class _FakeStreamResponse:
    def __init__(self, chunks, *, headers=None, status=200, seen_threads=None):
        self._chunks = list(chunks)
        self.headers = headers or {}
        self.status_code = status
        self._seen_threads = seen_threads

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Server Error for url: https://img.example/x.jpg")

    def iter_content(self, chunk_size=8192):
        for chunk in self._chunks:
            if self._seen_threads is not None:
                self._seen_threads.append(threading.current_thread())
            yield chunk


def _install_get(monkeypatch, response_factory, calls):
    def fake_get(url, headers=None, timeout=None, stream=False):
        calls.append(url)
        return response_factory()

    monkeypatch.setattr(ai_handler.requests, "get", fake_get)


def test_download_single_image_runs_whole_download_off_the_event_loop(tmp_path, monkeypatch):
    seen_threads = []
    calls = []
    monkeypatch.setattr(ai_handler, "AI_MAX_IMAGE_BYTES", 1024)
    _install_get(
        monkeypatch,
        lambda: _FakeStreamResponse([b"\x89PNG\r\n\x1a\n", b"rest"], seen_threads=seen_threads),
        calls,
    )
    save_path = tmp_path / "a.png"

    result = asyncio.run(ai_handler._download_single_image("https://img.example/a.png", str(save_path)))

    assert result == str(save_path)
    assert save_path.read_bytes() == b"\x89PNG\r\n\x1a\nrest"
    assert not (tmp_path / "a.png.part").exists()
    # 读 body 的循环也在工作线程里，不在跑事件循环的主线程
    assert seen_threads and all(t is not threading.main_thread() for t in seen_threads)
    assert calls == ["https://img.example/a.png"]


def test_download_single_image_skips_oversized_by_content_length(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ai_handler, "AI_MAX_IMAGE_BYTES", 1024)
    _install_get(
        monkeypatch,
        lambda: _FakeStreamResponse([b"x" * 10], headers={"Content-Length": "5000"}),
        calls,
    )
    save_path = tmp_path / "big.jpg"

    result = asyncio.run(ai_handler._download_single_image("https://img.example/big.jpg", str(save_path)))

    assert result is None
    assert not save_path.exists() and not (tmp_path / "big.jpg.part").exists()
    assert len(calls) == 1  # 超大图不重试


def test_download_single_image_skips_oversized_while_streaming(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ai_handler, "AI_MAX_IMAGE_BYTES", 1024)
    _install_get(monkeypatch, lambda: _FakeStreamResponse([b"x" * 600] * 3), calls)
    save_path = tmp_path / "big.jpg"

    result = asyncio.run(ai_handler._download_single_image("https://img.example/big.jpg", str(save_path)))

    assert result is None
    assert not save_path.exists() and not (tmp_path / "big.jpg.part").exists()
    assert len(calls) == 1


def test_download_single_image_retries_http_errors_then_gives_up(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ai_handler, "AI_MAX_IMAGE_BYTES", 1024)
    monkeypatch.setattr(ai_handler, "IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS", 0)
    _install_get(monkeypatch, lambda: _FakeStreamResponse([], status=503), calls)
    save_path = tmp_path / "x.jpg"

    result = asyncio.run(ai_handler._download_single_image("https://img.example/x.jpg", str(save_path)))

    assert result is None
    assert len(calls) == ai_handler.IMAGE_DOWNLOAD_RETRIES == 2
    assert not save_path.exists()


def test_download_all_images_only_downloads_first_n_urls(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_handler, "IMAGE_SAVE_DIR", str(tmp_path / "images"))
    monkeypatch.setattr(ai_handler, "AI_MAX_IMAGES_PER_ITEM", 2)
    downloaded = []

    async def fake_download(url, save_path):
        downloaded.append(url)
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        Path(save_path).write_text("ok", encoding="utf-8")
        return save_path

    monkeypatch.setattr(ai_handler, "_download_single_image", fake_download)
    urls = [f"https://example.com/{i}.jpg" for i in range(5)]

    paths = asyncio.run(ai_handler.download_all_images("p", urls, task_name="demo"))

    assert len(paths) == 2
    assert downloaded == urls[:2]

    # 显式传 max_images 可覆盖
    downloaded.clear()
    paths = asyncio.run(ai_handler.download_all_images("q", urls, task_name="demo", max_images=3))
    assert len(paths) == 3 and downloaded == urls[:3]


def test_build_image_data_urls_detects_mime_and_skips_large_files(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_handler, "AI_MAX_IMAGE_BYTES", 64)
    monkeypatch.setattr(ai_handler, "AI_MAX_IMAGES_PER_ITEM", 10)
    files = {
        "png.jpg": b"\x89PNG\r\n\x1a\n" + b"\x00" * 8,
        "webp.jpg": b"RIFF\x00\x00\x00\x00WEBPVP8 ",
        "gif.jpg": b"GIF89a" + b"\x00" * 6,
        "jpeg.jpg": b"\xff\xd8\xff\xe0" + b"\x00" * 8,
        "heic.jpg": b"\x00\x00\x00\x18ftypheic" + b"\x00" * 4,
        "big.jpg": b"\xff\xd8\xff" + b"\x00" * 100,
    }
    paths = []
    for name, data in files.items():
        path = tmp_path / name
        path.write_bytes(data)
        paths.append(str(path))
    paths.append(str(tmp_path / "missing.jpg"))

    data_urls = ai_handler.build_image_data_urls(paths)

    prefixes = [url.split(";", 1)[0] for url in data_urls]
    assert prefixes == [
        "data:image/png",
        "data:image/webp",
        "data:image/gif",
        "data:image/jpeg",
        "data:image/jpeg",  # heic 认不出来回退 jpeg
    ]
