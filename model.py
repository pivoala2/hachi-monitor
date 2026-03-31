import joblib
import sqlite3
import pandas as pd
from sklearn.ensemble import RandomForestClassifier

MODEL_PATH = "model.pkl"

def train_model():

    conn = sqlite3.connect("cat.db")
    df = pd.read_sql("SELECT * FROM events WHERE label IS NOT NULL", conn)

    if len(df) < 10:
        return "Not enough data"

    X = df[[
        "duration",
        "total_diff",
        "max_slope",
        "mean_slope",
        "variance",
        "vibration_count"
    ]]
    y = df["label"]

    model = RandomForestClassifier()
    model.fit(X, y)

    joblib.dump(model, MODEL_PATH)
    return "Model trained"


def predict(feature_dict):

    model = joblib.load(MODEL_PATH)

    X = [[
        feature_dict["duration"],
        feature_dict["total_diff"],
        feature_dict["max_slope"],
        feature_dict["mean_slope"],
        feature_dict["variance"],
        feature_dict["vibration_count"]
    ]]

    return model.predict(X)[0]

