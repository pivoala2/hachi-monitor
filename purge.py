import sqlite3
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
import os
import struct

DB_PATH = "/data/cat.db"
SUMMARY_FILE = "/app/shared_summary/summary.txt"

# --------------------------
# Utility
# --------------------------
def safe_float(v):
    if v is None:
        return 0.0
    if isinstance(v, bytes):
        try:
            return struct.unpack('<d', v)[0] if len(v) == 8 else 0.0
        except:
            return 0.0
    try:
        return float(v)
    except:
        return 0.0

def get_db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

# --------------------------
# Load Data
# --------------------------
def load_all_data(hours=None):
    """
    hours=None  : 全期間ロード（rebuild用）
    hours=N     : 直近N時間のみロード（通常運用用）
    """
    if hours is not None:
        cutoff_ts = datetime.now().timestamp() - (hours * 3600)
        sql = "SELECT timestamp, weight FROM raw_data WHERE timestamp >= ? ORDER BY timestamp"
        params = (cutoff_ts,)
    else:
        sql = "SELECT timestamp, weight FROM raw_data ORDER BY timestamp"
        params = ()

    with get_db_conn() as conn:
        df_raw = pd.read_sql(sql, conn, params=params)
        # pd.to_numeric はベクトル演算なので apply(safe_float) より高速
        df_raw['timestamp'] = pd.to_numeric(df_raw['timestamp'], errors='coerce').fillna(0.0)
        df_raw['weight']    = pd.to_numeric(df_raw['weight'],    errors='coerce').fillna(0.0)

    return df_raw

# --------------------------
# Weight Calculation
# --------------------------
def calculate_event_weights(df, start_ts, end_ts):
    unit_factor = 1000.0 if start_ts > 1e11 else 1.0

    # ==============================
    # tare計算（中央値自動検出方式）
    # ゼロ点が何gかを決め打ちせず直前データの中央値から自動検出する
    # ゼロ点が-550g・0g・1915gどれであっても正しく動作する
    # ==============================
    before_all = df[df["timestamp"] < start_ts]
    before_recent = before_all.tail(20)

    if not before_recent.empty:
        # 中央値を基準に±100g以内のデータのみをtareとして使う
        # 入室過渡値（急激に大きな値）を自動除外できる
        median_w = before_recent["weight"].median()
        stable = before_recent[before_recent["weight"].between(median_w - 100, median_w + 100)]
        base_in = stable["weight"].median() if not stable.empty else median_w
    else:
        base_in = 0.0

    print(f"  [tare] base_in={base_in:.1f}g (median方式)")

    # 猫が乗っている間の全データ
    event_data = df[(df["timestamp"] >= start_ts) & (df["timestamp"] <= end_ts)]
    if event_data.empty:
        return 0.0, 0.0

    # 体重 = quantile(0.9) - tare
    cat_w = round(event_data["weight"].quantile(0.9) - base_in, 1)

    # 排泄量計算（退室直後10秒以内・砂箱重量範囲に限定）
    after = df[
        (df["timestamp"] > end_ts)
        & (df["timestamp"] <= end_ts + (10 * unit_factor))
        & (df["weight"] > 500)
        & (df["weight"] < 1200)
    ]
    if len(after) >= 2:
        base_out = after["weight"].median()
        waste_w = round(base_out - base_in, 1)
    else:
        waste_w = 0.0

    return cat_w, waste_w

# --------------------------
# Auto Tagging（上書き禁止版）
# --------------------------
def auto_tagging_all(rebuild=False):
    # rebuild時は全期間、通常は直近36時間のみロード
    # （36時間 = 今日分のイベント + ベースライン計算用の前日バッファを含む）
    df = load_all_data(hours=None if rebuild else 36)
    if df.empty:
        print("No data")
        return 0

    unit = "s" if df["timestamp"].max() < 1e11 else "ms"
    df["dt"] = pd.to_datetime(df["timestamp"], unit=unit)
    df = df.sort_values("timestamp")

    df["is_on"] = df["weight"] > 3200
    df["diff"] = df["weight"].diff().fillna(0)
    df["event_id"] = (df["is_on"] != df["is_on"].shift()).cumsum()

    # ==============================
    # イベント結合（60秒以内の再入室を同一セッションとして扱う）
    # においかぎ・一時退室による分断を吸収する
    # ==============================
    MERGE_GAP_SEC = 60

    raw_events = []
    for eid, group in df[df["is_on"]].groupby("event_id"):
        raw_events.append({
            "start_ts": group["timestamp"].iloc[0],
            "end_ts":   group["timestamp"].iloc[-1],
            "count":    len(group),
            "diff_max": group["diff"].max(),
        })

    merged_events = []
    for ev in raw_events:
        if merged_events:
            gap = ev["start_ts"] - merged_events[-1]["end_ts"]
            if 0 <= gap <= MERGE_GAP_SEC:
                merged_events[-1]["end_ts"]   = ev["end_ts"]
                merged_events[-1]["count"]   += ev["count"]
                merged_events[-1]["diff_max"] = max(merged_events[-1]["diff_max"], ev["diff_max"])
                print(f"  [merge] イベント結合: gap={gap:.0f}s")
                continue
        merged_events.append(dict(ev))

    print(f"イベント数: raw={len(raw_events)} → merged={len(merged_events)}")

    # 今日0時のタイムスタンプ（秒）
    today_start_ts = datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()

    new_tags = []

    with get_db_conn() as conn:

        if rebuild:
            print("⚠ labels全削除")
            conn.execute("DELETE FROM labels")

        # 過去10回平均をループ外で1回だけ取得
        avg_rows = conn.execute(
            "SELECT cat_w FROM labels WHERE cat_w > 100 ORDER BY start_ts DESC LIMIT 10"
        ).fetchall()
        if len(avg_rows) >= 3:
            past_weights = [float(r[0]) for r in avg_rows]
            past_avg = sum(past_weights) / len(past_weights)
        else:
            past_avg = None

        for ev in merged_events:
            start_ts = ev["start_ts"]
            end_ts   = ev["end_ts"]

            # rebuild でない場合は今日0時以降のイベントのみ処理
            if not rebuild and start_ts < today_start_ts:
                continue

            if ev["count"] < 3:
                continue

            if ev["diff_max"] < 500:
                continue

            duration = end_ts - start_ts
            if duration < 3 or duration > 600:
                continue

            cat_weight, waste = calculate_event_weights(df, start_ts, end_ts)

            if 3500 <= cat_weight <= 5500:

                # ① 過去10回平均との差チェック（ループ外で取得済み）
                cat_weight_valid = True
                if past_avg is not None:
                    if abs(cat_weight - past_avg) >= 500:
                        print("⚠ 猫体重が過去平均から500g以上ズレ → N/A")
                        cat_weight_valid = False

                # ② フォールバック判定（wasteが異常値の場合）
                used_fallback = False

                if waste <= 5 or waste > 200 or not cat_weight_valid:
                    print("⚠ 排泄量フォールバック（直前イベント差分使用）")
                    if len(new_tags) > 0:
                        prev_cat_w = new_tags[-1][3]
                        fallback = round(prev_cat_w - cat_weight, 1)
                        if fallback < 0:
                            waste = 0.0
                        elif fallback > 200:
                            waste = 0.0
                        else:
                            waste = fallback
                            used_fallback = True
                    else:
                        waste = 0.0

                # ③ N/A処理
                if not cat_weight_valid:
                    cat_weight_to_save = 0.0
                    cam = conn.execute(
                        "SELECT camera_label FROM labels WHERE ABS(start_ts - ?) < 10 LIMIT 1",
                        (float(start_ts),)
                    ).fetchone()
                    print(f"DEBUG N/A: start_ts={start_ts}, cam={cam}")
                    cam_label = cam[0] if cam and cam[0] else None
                    if cam_label == "おしっこ":
                        label = "おしっこ(pee)"
                    elif cam_label == "うんち":
                        label = "うんち(poop)"
                    else:
                        label = "N/A"
                else:
                    cat_weight_to_save = round(cat_weight, 1)
                    if waste >= 40 or duration > 100:
                        label = "うんち(poop)"
                    else:
                        label = "おしっこ(pee)"

                if used_fallback:
                    label = label + " ※体重差分"

                new_tags.append(
                    (start_ts, end_ts, label, cat_weight_to_save, waste)
                )

        # INSERT処理
        existing = set(
            r[0] for r in conn.execute("SELECT start_ts FROM labels")
        )

        for s, e, l, cw, ww in new_tags:
            if s in existing:
                continue
            conn.execute(
                "INSERT INTO labels VALUES (?,?,?,?,?,?,?,?)",
                (float(s), float(e), l, float(cw), float(ww), None, None, 0)
            )

        conn.commit()

    print(f"Tagged {len(new_tags)} events")
    return len(new_tags)

def write_summary_file():
    try:
        print("DEBUG: write_summary_file 開始")
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        # summary.txt は graph_worker が30日分を使うので30日分だけ取得
        cutoff_ts = datetime.now().timestamp() - (30 * 24 * 3600)
        rows = conn.execute(
            "SELECT start_ts, end_ts, label, cat_w, waste_w FROM labels "
            "WHERE start_ts >= ? ORDER BY start_ts",
            (cutoff_ts,)
        ).fetchall()
        conn.close()

        if not rows:
            print("labels テーブルにデータがありません")
            return

        output_time = datetime.now().strftime('%Y/%m/%d %H:%M:%S')
        os.makedirs(os.path.dirname(SUMMARY_FILE), exist_ok=True)

        with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
            f.write(f"# Generated at {output_time}\n\n")
            for r in rows:
                if r['label'] == 'N/A':
                    continue
                ts = float(r['start_ts'])
                unit = 1000.0 if ts > 1e11 else 1.0
                event_time = datetime.fromtimestamp(ts / unit).strftime('%Y/%m/%d %H:%M:%S')
                line = (
                    f"[{event_time}] "
                    f"判定: [{r['label']}] 猫体重: {r['cat_w']}g 排泄量: {r['waste_w']}g\n"
                )
                f.write(line)
            f.flush()
            os.fsync(f.fileno())

        print(f"✅ {len(rows)} 件のラベル情報を {SUMMARY_FILE} に書き込み、クローズしました")
    except Exception as e:
        print(f"❌ write_summary_file でエラーが発生しました: {e}")

# --------------------------
# Camera Events → labels 反映
# --------------------------
def apply_camera_events():
    import json
    CAMERA_EVENTS_FILE = "/app/shared_summary/camera_events.json"
    if not os.path.exists(CAMERA_EVENTS_FILE):
        print("camera_events.json が見つかりません")
        return
    with open(CAMERA_EVENTS_FILE, "r", encoding="utf-8") as f:
        events = json.load(f)
    if not events:
        print("camera_events.json が空です")
        return
    updated = 0
    with get_db_conn() as conn:
        for ev in events:
            ts = float(ev["timestamp"])
            label = ev.get("gemini_label", "不明")
            row = conn.execute("""
                SELECT start_ts FROM labels
                WHERE ABS(start_ts - ?) <= 60
                ORDER BY ABS(start_ts - ?) ASC
                LIMIT 1
            """, (ts, ts)).fetchone()
            if row:
                conn.execute("""
                    UPDATE labels
                    SET camera_label = ?,
                        label = CASE
                            WHEN manually_edited = 1 THEN label
                            WHEN ? = 'おしっこ' THEN 'おしっこ(pee)'
                            WHEN ? = 'うんち'   THEN 'うんち(poop)'
                            ELSE label
                        END
                    WHERE start_ts = ?
                """, (label, label, label, row[0]))
                updated += 1
                print(f"  UPDATE start_ts={row[0]} → camera_label={label}")
            else:
                print(f"  対応するlabelsレコードなし: timestamp={ts}")
        conn.commit()
    print(f"✅ camera_events 反映完了: {updated}件")

# --------------------------
# Entry Point
# --------------------------
if __name__ == "__main__":
    import time
    REBUILD_FLAG = False

    t0 = time.time()
    apply_camera_events()
    print(f"[時間] apply_camera_events: {time.time()-t0:.2f}s")

    t1 = time.time()
    auto_tagging_all(rebuild=REBUILD_FLAG)
    print(f"[時間] auto_tagging_all: {time.time()-t1:.2f}s")

    t2 = time.time()
    write_summary_file()
    print(f"[時間] write_summary_file: {time.time()-t2:.2f}s")

    print(f"[時間] 合計: {time.time()-t0:.2f}s")
