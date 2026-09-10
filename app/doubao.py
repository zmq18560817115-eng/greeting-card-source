"""豆包 Doubao-Seedream 生图（火山方舟 Ark /images/generations）。

文档：https://www.volcengine.com/docs/82379/1541523
关键参数：model(接入点ID或模型名) / prompt / image(参考图，URL 或 data URI) /
         size / response_format / watermark / seed
"""
import base64
import logging
import mimetypes
import random
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from .settings import ARK_API_KEY, ARK_BASE, ARK_MODEL

log = logging.getLogger("doubao")


class ArkError(RuntimeError):
    pass


def _data_uri(path):
    path = Path(path)
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def generate_one(prompt, reference_image=None, size="2K", seed=None,
                 output_format="png", timeout=180, max_retry=2):
    """生成 1 张图，返回 {'url':..., 'seed':..., 'revised_prompt':...}。"""
    if not ARK_API_KEY:
        raise ArkError("未配置 ARK_API_KEY")
    payload = {
        "model": ARK_MODEL,
        "prompt": prompt,
        "size": size,
        "response_format": "url",
        "watermark": False,
        "output_format": output_format,
    }
    if seed is not None:
        payload["seed"] = int(seed)
    if reference_image:
        if str(reference_image).startswith("http"):
            payload["image"] = str(reference_image)
        else:
            payload["image"] = _data_uri(reference_image)

    last_err = None
    for attempt in range(max_retry + 1):
        try:
            r = requests.post(
                f"{ARK_BASE}/images/generations",
                headers={"Authorization": f"Bearer {ARK_API_KEY}",
                         "Content-Type": "application/json"},
                json=payload, timeout=timeout,
            )
            if r.status_code >= 400:
                raise ArkError(f"HTTP {r.status_code}: {r.text[:400]}")
            data = r.json()
            items = data.get("data") or []
            if not items:
                raise ArkError(f"返回为空: {str(data)[:300]}")
            item = items[0]
            return {
                "url": item.get("url"),
                "b64": item.get("b64_json"),
                "seed": payload.get("seed"),
                "revised_prompt": item.get("revised_prompt"),
            }
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("豆包生图失败(第%d次): %s", attempt + 1, e)
            if attempt < max_retry:
                time.sleep(2 + attempt * 3)
    raise ArkError(str(last_err))


def generate_batch(prompt, count=5, reference_image=None, size="2K", workers=5):
    """并发生成 count 张。返回长度为 count 的列表，失败项为 {'error': ...}。"""
    seeds = [random.randint(1, 2_000_000_000) for _ in range(count)]

    def _run(i):
        try:
            return generate_one(prompt, reference_image=reference_image,
                                size=size, seed=seeds[i])
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    with ThreadPoolExecutor(max_workers=min(workers, count)) as ex:
        return list(ex.map(_run, range(count)))


def download(url, dest):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    dest.write_bytes(r.content)
    return dest
