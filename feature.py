import numpy as np

def extract_features(weights):

    duration = len(weights)
    total_diff = weights[-1] - weights[0]
    diffs = np.diff(weights)

    max_slope = np.max(diffs) if len(diffs) else 0
    mean_slope = np.mean(diffs) if len(diffs) else 0
    variance = np.var(weights)
    vibration_count = np.sum(np.abs(diffs) > 5)

    return {
        "duration": duration,
        "total_diff": total_diff,
        "max_slope": float(max_slope),
        "mean_slope": float(mean_slope),
        "variance": float(variance),
        "vibration_count": int(vibration_count)
    }

