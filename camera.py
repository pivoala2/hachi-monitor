import os
import time
import requests
import io
from PIL import Image
from typing import List

CAMERA_URL = os.getenv("CAMERA_URL", "")
CAMERA_USER = os.getenv("CAMERA_USER", "")
CAMERA_PASSWORD = os.getenv("CAMERA_PASSWORD", "")
SNAPSHOT_INTERVAL = 2  # 秒（退室後すぐ掃除されるため短縮）
MAX_SNAPSHOTS = 30     # 最大60秒分

# トリミング設定（右下エリア）※要調整
CROP_LEFT = 1440
CROP_TOP = 808
CROP_RIGHT = 2880
CROP_BOTTOM = 1616

COOLDOWN_SNAPSHOTS = 4   # 退場後に追加で撮る枚数
COOLDOWN_INTERVAL = 3    # 秒


def crop_toilet_area(image_bytes: bytes) -> bytes:
    """トイレエリアをトリミング"""
    img = Image.open(io.BytesIO(image_bytes))
    cropped = img.crop((CROP_LEFT, CROP_TOP, CROP_RIGHT, CROP_BOTTOM))
    buf = io.BytesIO()
    cropped.save(buf, format="JPEG")
    return buf.getvalue()


def fetch_snapshot() -> bytes | None:
    """カメラから1枚スナップショットを取得してトリミング"""
    try:
        password = CAMERA_PASSWORD
        import urllib.parse
        encoded_password = urllib.parse.quote(password, safe='')
        url = f"{CAMERA_URL}?cmd=Snap&channel=0&rs={int(time.time())}&user={CAMERA_USER}&password={encoded_password}"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200 and resp.headers.get("Content-Type", "").startswith("image"):
            return crop_toilet_area(resp.content)
        else:
            print(f"[camera] 取得失敗: {resp.text[:100]}")
            return None
    except Exception as e:
        print(f"[camera] エラー: {e}")
        return None

def capture_session(stop_event) -> tuple[list[bytes], int]:
    """入場〜退場まで連続撮影、退場後もクールダウン撮影
    戻り値: (images, cooldown_start_index)
    """
    images = []
    print("[camera] 撮影開始")

    while not stop_event.is_set() and len(images) < MAX_SNAPSHOTS:
        img = fetch_snapshot()
        if img:
            images.append(img)
            print(f"[camera] 撮影中 {len(images)}枚")
        time.sleep(SNAPSHOT_INTERVAL)

    cooldown_start = len(images)  # 退室時点のインデックス

    print(f"[camera] 退場検知 → クールダウン撮影 {COOLDOWN_SNAPSHOTS}枚")
    for i in range(COOLDOWN_SNAPSHOTS):
        time.sleep(COOLDOWN_INTERVAL)
        img = fetch_snapshot()
        if img:
            images.append(img)
            print(f"[camera] クールダウン {i+1}/{COOLDOWN_SNAPSHOTS}枚")

    print(f"[camera] 撮影終了 合計{len(images)}枚 クールダウン開始={cooldown_start}枚目")
    return images, cooldown_start
