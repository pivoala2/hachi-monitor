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

# 撮影中フラグ（多重起動防止）
_shooting = False
_shooting_lock = threading.Lock()


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
            return 4300.0  # デフォルト
        weights = [float(r[0]) for r in rows]
        return sum(weights) / len(weights)
    except Exception as e:
        print(f"[avg] エラー: {e}")
        return 4300.0


def shoot_and_analyze(timestamp: int):
    """カメラ撮影してGeminiで解析、結果をJSONに保存"""
    global _shooting

    with _shooting_lock:
        if _shooting:
            print("[camera] 既に撮影中のためスキップ")
            return
        _shooting = True

    try:
        from camera import capture_session
        import io as _io

        stop_event = threading.Event()

        # 退室検知スレッド：重量が下がったらstop_eventをセット
        def watch_exit():
            time.sleep(3)  # 入室直後の安定待ち
            consecutive_low = 0
            while not stop_event.is_set():
                try:
                    conn = get_conn()
                    row = conn.execute(
                        "SELECT weight FROM raw_data ORDER BY timestamp DESC LIMIT 1"
                    ).fetchone()
                    conn.close()
                    if row:
                        w = float(row[0])
                        baseline = get_baseline()
                        if baseline is not None:
                            cat_est = w - baseline
                            if cat_est < 500:
                                consecutive_low += 1
                                print(f"[exit] 退室候補 {consecutive_low}回目 (cat_est={cat_est:.1f}g)")
                            else:
                                consecutive_low = 0
                        if consecutive_low >= 2:
                            print("[exit] 退室検知 → 撮影停止")
                            stop_event.set()
                            break
                except Exception as e:
                    print(f"[exit] エラー: {e}")
                time.sleep(2)

        # 最大60秒で強制停止
        def stop_after():
            time.sleep(60)
            if not stop_event.is_set():
                print("[exit] タイムアウト → 撮影停止")
                stop_event.set()

        threading.Thread(target=watch_exit, daemon=True).start()
        threading.Thread(target=stop_after, daemon=True).start()
        images, cooldown_start = capture_session(stop_event)

        if not images:
            print("[camera] 画像取得なし")
            return

        print(f"[camera] {len(images)}枚取得 → Gemini解析開始")

        # ===== 画像保存（全枚数）=====
        shot_dir = "/app/shared_summary/camera_shots"
        os.makedirs(shot_dir, exist_ok=True)

        for img_bytes in images:
            ts_str = datetime.now().strftime("%Y%m%d%H%M%S")
            shot_path = os.path.join(shot_dir, f"front_00_{ts_str}.jpg")
            with open(shot_path, "wb") as f:
                f.write(img_bytes)
            print(f"[camera] 保存: {shot_path}")
            time.sleep(1)  # 同秒防止の保険

        # 古いファイルを削除（150ファイル超えたら古いものから削除）
        all_files = sorted([f for f in os.listdir(shot_dir) if f.endswith(".jpg")])
        while len(all_files) > 150:
            os.remove(os.path.join(shot_dir, all_files.pop(0)))

        from google.genai import types
        client = get_gemini_client()

        # Geminiで解析
        # 退室直前2枚（行動中）+ 退室後1枚（砂の状態）を使用
        prompt = """ これは猫のトイレ利用シーンの時系列画像です。
ケージの中に猫のトイレがあり、猫が何をしているか答えて。
画像に人間が映っていても無視してください。猫の姿勢だけを見て判定してください。
以下のルールで判定してください：
- 夜や早朝はカラーではなく、白黒の画像になります。白黒の画像だからと言ってうんちの判定にしないこと。
- 1. ブラウン色のトイレの淵（縁）に前足をかけている姿勢が見られる場合は、うんち
- 2. おしりから黒いものが出ている場合は、うんち
- 3. トイレに黒いものがある場合は、うんち
- 1～3ではなく、砂の上にすわっている → おしっこ
- 判断できない → 不明

以下のどれか1単語だけ答えてください：
- うんち
- おしっこ
- 不明"""
        parts = []

        # 退室直前3枚 + クールダウン1枚目
        gemini_images = images[max(0, cooldown_start-3):cooldown_start] + [images[cooldown_start]]
        if not gemini_images:
            gemini_images = images[-3:]
        for img_bytes in gemini_images:
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

        # 直近100件だけ保持
        events = events[-100:]

        os.makedirs(os.path.dirname(CAMERA_EVENTS_FILE), exist_ok=True)
        with open(CAMERA_EVENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False, indent=2)

        print(f"[camera] 保存完了: {CAMERA_EVENTS_FILE}")

    except Exception as e:
        print(f"[camera] エラー: {e}")

    finally:
        with _shooting_lock:
            _shooting = False

# ① ベースラインを直近30件ではなく「500g未満のデータ」直近10件に変える
# → 猫乗車中のデータに汚染されない

def get_baseline():
    try:
        conn = get_conn()
        rows = conn.execute(
            "SELECT weight FROM raw_data ORDER BY timestamp DESC LIMIT 20"
        ).fetchall()
        conn.close()
        if len(rows) < 3:
            return None
        weights = sorted([float(r[0]) for r in rows])
        median_w = weights[len(weights) // 2]
        # 中央値±200g以内の安定データのみでbaselineを計算
        # 異常値（クールダウン中の-600gなど）を自動除外
        stable = [w for w in weights if median_w - 200 <= w <= median_w + 200]
        return sum(stable) / len(stable) if stable else median_w
    except Exception as e:
        print(f"[baseline] エラー: {e}")
        return None

# ② capture_session の最初の1枚を即撮影に変える

@app.post("/weight")
def receive_weight(data: WeightData, background_tasks: BackgroundTasks):
    conn = get_conn()
    conn.execute(
        "INSERT INTO raw_data (timestamp, weight) VALUES (?, ?)",
        (data.timestamp, data.weight)
    )
    conn.commit()
    conn.close()

    # ベースラインからの差分で猫体重を推定
    baseline = get_baseline()
    if baseline is not None:
        cat_weight_est = data.weight - baseline
        if CAT_WEIGHT_MIN <= cat_weight_est <= CAT_WEIGHT_MAX:
            avg = get_recent_average()
            if abs(cat_weight_est - avg) < 300:
                with _shooting_lock:
                    already = _shooting
                if not already:
                    background_tasks.add_task(shoot_and_analyze, data.timestamp)
                    print(f"[trigger] 推定体重={cat_weight_est:.1f}g ベース={baseline:.1f}g → 撮影開始")

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
