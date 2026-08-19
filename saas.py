# saas.py -- Signal Aggregation and Alerting System (Section 3.2.5 / 5.6)
#
# Runs server-side only (server = "system operator" per Section 3.2.2). It never
# sees raw text, embeddings, or the raw inference score -- only the daily binary
# flag resolved by the SMPC comparison in compare.mpc (score > threshold).
#
# Sliding window: N=7 consecutive daily flags, window mean = fraction of the
# last N days flagged positive. Alert dispatched once window mean exceeds
# ALPHA (Section 5.6). The MEDIUM/HIGH split within "alert dispatched" is not
# pinned to a specific number in the paper (Section 3.2.5 only says the tier
# determines who is notified) -- HIGH_ALPHA below is a configurable second
# cut point, set conservatively at the midpoint between ALPHA and 1.0.

import json
import os
import time

DATA_DIR = "saas_data"
HISTORY_FILE = f"{DATA_DIR}/history.json"
TRACKING_FILE = f"{DATA_DIR}/longitudinal_tracking.json"
NOTIFICATIONS_LOG = f"{DATA_DIR}/notifications.log"

WINDOW_SIZE = 7     # N consecutive daily inference scores (Section 5.6)
ALPHA = 0.5         # alert threshold on window mean (Section 5.6)
HIGH_ALPHA = 0.75   # MEDIUM -> HIGH escalation threshold (configurable, not fixed in paper)

os.makedirs(DATA_DIR, exist_ok=True)


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    with open(HISTORY_FILE, "r") as f:
        return json.load(f)


def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def append_score(flag):
    # flag is the SMPC-resolved boolean (score > threshold) for today's sample.
    # No raw score, embedding, or text is ever written here.
    history = load_history()
    history.append({"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "flag": int(flag)})
    history = history[-WINDOW_SIZE:]  # keep only the most recent N entries
    save_history(history)
    return history


def compute_window_mean(history):
    if len(history) == 0:
        return None
    flags = [entry["flag"] for entry in history]
    return sum(flags) / len(flags)


def determine_tier(window_mean):
    if window_mean is None or window_mean <= ALPHA:
        return "LOW"
    elif window_mean <= HIGH_ALPHA:
        return "MEDIUM"
    else:
        return "HIGH"


def record_outcome(tier, window_mean):
    if os.path.exists(TRACKING_FILE):
        with open(TRACKING_FILE, "r") as f:
            tracking = json.load(f)
    else:
        tracking = []
    tracking.append({
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tier": tier,
        "window_mean": window_mean
    })
    with open(TRACKING_FILE, "w") as f:
        json.dump(tracking, f, indent=2)


def notify(tier, category="depression"):
    # Notification payload contains only tier, category, and timestamp --
    # never raw text, embeddings, or scores (Section 3.2.5 / 5.6).
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    payload = {"tier": tier, "category": category, "timestamp": timestamp}

    with open(NOTIFICATIONS_LOG, "a") as f:
        f.write(f"{timestamp} | tier={tier} | payload={payload}\n")

    if tier == "LOW":
        print(f"[SAAS] LOW tier - in-app resource shown to user only. payload={payload}")
    elif tier == "MEDIUM":
        print(f"[SAAS] MEDIUM tier - notifying pre-registered trusted contact. payload={payload}")
        # TODO: replace with a real provider call, e.g. Twilio Client().messages.create(...)
    elif tier == "HIGH":
        print(f"[SAAS] HIGH tier - notifying trusted contact and mental health professional. payload={payload}")
        # TODO: replace with real provider calls for both recipients

    return payload


def run_saas(flag, category="depression"):
    history = append_score(flag)
    window_mean = compute_window_mean(history)
    tier = determine_tier(window_mean)
    notify(tier, category=category)
    record_outcome(tier, window_mean)
    print(f"[SAAS] window={len(history)}/{WINDOW_SIZE} days, window_mean={window_mean}, tier={tier}")
    return tier, window_mean
