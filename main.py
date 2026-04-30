from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
from database import init_db, get_conn
import time
import json
import os
import threading
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()
init_db()

CAMERA_EVENTS_FILE = "/app/shared_summary/camera_events.json"
CAT_WEIGHT_MIN = 3500
CAT_WEIGHT_MAX = 5000
EXIT_THRESHOLD = 2000  # 退場検知閾値（砂箱1300g + 余裕700g）

# ファイル先頭のグローバル変数に追加
_last_gemini_time = 0
GEMINI_MIN_INTERVAL = 120  # 秒（2分以内の連続呼び出しはスキップ）

# 撮影中フラグ（多重起動防止）
_shooting = False
_shooting_lock = threading.Lock()
_stop_event: threading.Event | None = None  # ★追加：退場検知用


def get_gemini_client():
    """呼び出し時にclientを初期化（起動時エラー防止）"""
    from google import genai
    return genai.Client(api_key=os.getenv("GEMINI_API_KEY"))


class WeightData(BaseModel):
    timestamp: int
    weight: float


def get_recent_average():
    """過去10回のラベル済み体重平均を取得"""
    try:
        conn = get_conn()
        rows = conn.execute(
            "SELECT cat_w FROM labels WHERE cat_w > 100 ORDER BY start_ts DESC LIMIT 10"
        ).fetchall()
        conn.close()
        if len(rows) < 3:
            return 4300.0
        weights = [float(r[0]) for r in rows]
        return sum(weights) / len(weights)
    except Exception as e:
        print(f"[avg] エラー: {e}")
        return 4300.0


def get_baseline():
    try:
        conn = get_conn()
        rows = conn.execute(
            """SELECT weight FROM raw_data
               WHERE weight < 3500
               ORDER BY timestamp DESC LIMIT 10"""
        ).fetchall()
        conn.close()
        if len(rows) < 3:
            return None
        weights = [float(r[0]) for r in rows]
        return sorted(weights)[len(weights) // 2]
    except Exception as e:
        print(f"[baseline] エラー: {e}")
        return None


def shoot_and_analyze(timestamp: int):
    # shoot_and_analyze の最初のtryブロック内に追加
    global _last_gemini_time

    now = time.time()
    if now - _last_gemini_time < GEMINI_MIN_INTERVAL:
        print(f"[Gemini] スキップ（前回から{now - _last_gemini_time:.0f}秒）")
        return

    _last_gemini_time = now
    """カメラ撮影してGeminiで解析、結果をJSONに保存"""
    global _shooting, _stop_event

    with _shooting_lock:
        if _shooting:
            print("[camera] 既に撮影中のためスキップ")
            return
        _shooting = True

    # ★ stop_eventをグローバルに保持（/weightから参照できるように）
    stop_event = threading.Event()
    _stop_event = stop_event

    try:
        from camera import capture_session

        # ★ タイムアウトを180秒に延長（保険）
        def stop_after_timeout():
            time.sleep(180)
            if not stop_event.is_set():
                print("[camera] タイムアウト180秒 → 強制停止")
                stop_event.set()

        threading.Thread(target=stop_after_timeout, daemon=True).start()

        images, cooldown_start = capture_session(stop_event)
        if not images:
            print("[camera] 画像取得なし")
            return
        print(f"[camera] {len(images)}枚取得 → Gemini解析開始")

        # ===== 画像保存（全枚数）=====
        shot_dir = "/app/shared_summary/camera_shots"
        os.makedirs(shot_dir, exist_ok=True)

        for img_bytes in images:  # ← 全枚数
            ts_str = datetime.now().strftime("%Y%m%d%H%M%S%f")[:17]
            shot_path = os.path.join(shot_dir, f"front_00_{ts_str}.jpg")
            with open(shot_path, "wb") as f:
                f.write(img_bytes)
            print(f"[camera] 保存: {shot_path}")
            time.sleep(0.1)

        all_files = sorted([f for f in os.listdir(shot_dir) if f.endswith(".jpg")])
        while len(all_files) > 150:
            os.remove(os.path.join(shot_dir, all_files.pop(0)))

        # ===== Gemini用は入室中の3枚を選ぶ =====
        session_images = images[:cooldown_start]
        step = max(1, len(session_images) // 3)
        target_images = session_images[::step][:3]

        from google.genai import types
        client = get_gemini_client()

        prompt = """
これは猫のトイレの画像です。
猫がトイレで何をしているか判定してください。

判定ルール：
- 前足をトイレのふちにかけている、または立った姿勢 → うんち
- 座ってしゃがんでいる姿勢 → おしっこ
- 白黒画像でも姿勢で判断すること（夜間も同じルール）
- 猫がいない、または判断できない → 不明

以下のどれか1つだけ答えてください：
- うんち
- おしっこ
- 不明

理由は不要です。1単語だけ答えてください。
"""

        # ★ 解析に使う画像：先頭1枚＋中間2枚（在席中の姿勢を確認）
        parts = []
        mid = len(images) // 2
        parts_images = [images[0]] + images[mid-1:mid+1]
        for img_bytes in parts_images:
            parts.append(
                types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")
            )
        parts.append(prompt)

        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=parts
        )
        gemini_label = response.text.strip()
        print(f"[Gemini] 判定: {gemini_label}")

        # JSONに追記保存
        events = []
        if os.path.exists(CAMERA_EVENTS_FILE):
            with open(CAMERA_EVENTS_FILE, "r", encoding="utf-8") as f:
                try:
                    events = json.load(f)
                except Exception:
                    events = []

        events.append({
            "timestamp": timestamp,
            "gemini_label": gemini_label,
            "created_at": datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        })

        events = events[-100:]

        os.makedirs(os.path.dirname(CAMERA_EVENTS_FILE), exist_ok=True)
        with open(CAMERA_EVENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False, indent=2)

        print(f"[camera] 保存完了: {CAMERA_EVENTS_FILE}")

    except Exception as e:
        print(f"[camera] エラー: {e}")

    finally:
        _stop_event = None  # ★ 撮影終了後にクリア
        with _shooting_lock:
            _shooting = False


@app.post("/weight")
def receive_weight(data: WeightData, background_tasks: BackgroundTasks):
    global _stop_event

    conn = get_conn()
    conn.execute(
        "INSERT INTO raw_data (timestamp, weight) VALUES (?, ?)",
        (data.timestamp, data.weight)
    )
    conn.commit()
    conn.close()

    baseline = get_baseline()
    if baseline is not None:
        cat_weight_est = data.weight - baseline

        # ★ 退場検知：撮影中に猫が降りたらstop_eventを立てる
        if _stop_event and not _stop_event.is_set():
            if cat_weight_est < EXIT_THRESHOLD:
                print(f"[trigger] 退場検知 推定={cat_weight_est:.1f}g → 撮影停止")
                _stop_event.set()

        # 入場検知：撮影開始
        if CAT_WEIGHT_MIN <= cat_weight_est <= CAT_WEIGHT_MAX:
            avg = get_recent_average()
            if abs(cat_weight_est - avg) < 300:
                with _shooting_lock:
                    already = _shooting
                if not already:
                    background_tasks.add_task(shoot_and_analyze, data.timestamp)
                    print(f"[trigger] 入場検知 推定={cat_weight_est:.1f}g ベース={baseline:.1f}g → 撮影開始")

    return {"status": "ok"}


@app.post("/event")
def create_event(weights: list[float], label: str | None = None):
    from feature import extract_features
    from model import train_model
    features = extract_features(weights)
    conn = get_conn()
    conn.execute("""
    INSERT INTO events (
        start_time, end_time,
        duration, total_diff, max_slope,
        mean_slope, variance, vibration_count,
        label
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        int(time.time()), int(time.time()),
        features["duration"],
        features["total_diff"],
        features["max_slope"],
        features["mean_slope"],
        features["variance"],
        features["vibration_count"],
        label
    ))
    conn.commit()
    conn.close()
    if label:
        train_model()
    return {"features": features}


@app.post("/predict")
def predict_event(weights: list[float]):
    from feature import extract_features
    from model import predict
    features = extract_features(weights)
    result = predict(features)
    return {"prediction": result}
