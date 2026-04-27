from flask import Flask, render_template_string, request, redirect
from datetime import datetime, timezone, timedelta
import sqlite3
import os
import time
import sys

# purge.py と同じディレクトリをパスに追加して直接importする
sys.path.insert(0, "/app")
from purge import apply_camera_events, auto_tagging_all, write_summary_file

DB_PATH = "/data/cat.db"
SUMMARY_PATH = "/app/shared_summary/summary.txt"

app = Flask(__name__)

# --- HTML テンプレート（ボタンを追加） ---
HTML = """
<style>
    .long-stay { color: red; font-weight: bold; }
    .heavy-waste { color: blue; font-weight: bold; }
    table { border-collapse: collapse; width: 100%; }
    th, td { border: 1px solid #ccc; padding: 8px; text-align: left; }
    tr:nth-child(even) { background-color: #f9f9f9; }
    .update-header {
        background: #f0f0f0;
        padding: 15px;
        border-radius: 5px;
        margin-bottom: 15px;
        border-left: 5px solid #2196F3;
        display: flex;
        justify-content: space-between;
        align-items: center;
    }
    .btn-purge {
        background-color: #2196F3;
        color: white;
        padding: 10px 20px;
        text-decoration: none;
        border-radius: 4px;
        border: none;
        cursor: pointer;
        font-weight: bold;
    }
    .btn-purge:hover { background-color: #0b7dda; }
</style>
<h2>ラベル編集（直近50件）</h2>

<div class="update-header">
    <div>
        <strong>📊 データ最終更新:</strong> <span style="color: #d32f2f;">{{ updated_at }}</span>
        {% if elapsed_time %}
        　<span style="color: #555; font-size: 0.9em;">（再計算: {{ elapsed_time }}秒）</span>
        {% endif %}
    </div>
    <!-- Purge実行ボタンのフォーム -->
    <form action="/run_purge" method="post" style="margin: 0;">
        <button type="submit" class="btn-purge" onclick="return confirm('最新データで再計算(Purge)を開始しますか？');">
            🔄 最新データで再計算
        </button>
    </form>
</div>

<table>
<tr style="background-color: #eee;">
    <th>利用日時</th>
    <th>滞在時間</th>
    <th>現在のラベル</th>
    <th>猫体重</th>
    <th>排泄量</th>
    <th>📷 AI判定</th>
    <th>変更/削除</th>
</tr>
{% for row in rows %}
<tr>
<form method="post">
    <td>{{row.display_time}}</td>
    <td class="{{ 'long-stay' if row.is_long else '' }}">{{row.duration_str}}</td>
    <td style="font-weight: bold;">{{row.label}}</td>
    <td>{{row.cat_w}}g</td>
    <td class="{{ 'heavy-waste' if row.is_heavy else '' }}">{{row.waste_w}}g</td>
    <td style="color: #7b1fa2; font-weight: bold;">{{row.camera_label}}</td>
    <td>
        <input type="hidden" name="start_ts" value="{{row.start_ts}}">
        <select name="label">
            <option value="keep">--- 選択 ---</option>
            <option value="うんち(poop)">うんち</option>
            <option value="おしっこ(pee)">おしっこ</option>
            <option value="入室のみ">入室のみ</option>
            <option value="DELETE" style="color: red;">❌ 削除する</option>
        </select>
        <input type="submit" value="適用">
    </td>
</form>
</tr>
{% endfor %}
</table>
"""

# (get_last_update, get_labels 関数は変更なしのため中略)
def get_last_update():
    try:
        if not os.path.exists(SUMMARY_PATH): return "File not found"
        with open(SUMMARY_PATH, "r", encoding="utf-8") as f:
            first_line = f.readline()
            return first_line.split("at")[-1].strip() if "at" in first_line else first_line[:30]
    except: return "Error"

def get_labels():

# 変更後
    conn = sqlite3.connect(DB_PATH, timeout=30); conn.execute("PRAGMA journal_mode=WAL;"); conn.execute("PRAGMA busy_timeout=5000;"); conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT start_ts, end_ts, label, cat_w, waste_w, camera_label FROM labels ORDER BY start_ts DESC LIMIT 50")
    rows = cur.fetchall(); conn.close()
    jst = timezone(timedelta(hours=9))
    result = []
    for r in rows:
        ts_s, ts_e = float(r['start_ts']), float(r['end_ts'])
        unit = 1000.0 if ts_s > 1e11 else 1.0
        dur_sec = (ts_e - ts_s) / unit
        dt = datetime.fromtimestamp(ts_s / unit, tz=timezone.utc).astimezone(jst)
        result.append({
            "start_ts": ts_s, "display_time": dt.strftime("%Y/%m/%d %H:%M:%S"),
            "duration_str": f"{int(dur_sec//60)}分{int(dur_sec%60)}秒" if dur_sec>=60 else f"{int(dur_sec)}秒",
            "is_long": dur_sec > 100, "is_heavy": float(r['waste_w']) >= 40,
            "label": r['label'] if r['label'] else "(未設定)", "cat_w": r['cat_w'], "waste_w": r['waste_w'],
            "camera_label": r['camera_label'] if r['camera_label'] else "-"
        })
    return result

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        start_ts = float(request.form["start_ts"])
        selected_label = request.form["label"]
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        if selected_label == "DELETE":
            conn.execute("DELETE FROM labels WHERE start_ts=?", (start_ts,))
        elif selected_label != "keep":
            conn.execute("UPDATE labels SET label=?, manually_edited=1 WHERE start_ts=?", (selected_label, start_ts))
        conn.commit(); conn.close()
        # ★追加：summary.txtを再生成
        try:
            import sys
            sys.path.insert(0, "/app")
            from purge import write_summary_file
            write_summary_file()
        except Exception as e:
            print(f"write_summary_file error: {e}")
        return redirect("/")
    return render_template_string(HTML, rows=get_labels(), updated_at=get_last_update())

# --- Purge実行用の新しいエンドポイント ---
@app.route("/run_purge", methods=["POST"])
def run_purge():
    elapsed_time = None
    try:
        t0 = time.time()
        apply_camera_events()
        auto_tagging_all()
        write_summary_file()
        elapsed_time = round(time.time() - t0, 1)
        print(f"Purge完了: {elapsed_time}秒")
    except Exception as e:
        print(f"Purge Error: {e}")

    return render_template_string(HTML, rows=get_labels(), updated_at=get_last_update(), elapsed_time=elapsed_time)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5056)
