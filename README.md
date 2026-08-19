# SRISAI-Secure-Reconfigurable-Inference-and-Signal-Aggregation-with-Alerting-and-Intervention
SRISAI is the first system to combine privacy-preserving on-device depression detection with integrated tiered alerting and longitudinal tracking. 
# SRISAI — Encrypted Inference + SMPC Comparison + SAAS Demo

This is a minimal two-party demonstration of the SRISAI pipeline:

1. **Client** runs BERT-Tiny locally (plaintext) to embed a piece of text, then
   runs the classification head as **homomorphic (CKKS) inference** using
   encrypted weights it received (or already has cached) from the server.
2. Client blinds the encrypted result and sends it to the **server**, who
   decrypts the blinded value (never the real score).
3. Client and server jointly run an **MP-SPDZ garbled-circuit comparison**
   (`compare.mpc`) to resolve "is the score above the threshold?" into a
   single boolean flag, without the server ever seeing the true score.
4. The server feeds that daily flag into the **SAAS** (Signal Aggregation and
   Alerting System) — a 7-day sliding window that decides a LOW / MEDIUM /
   HIGH tier and logs a notification.

Everything runs locally on `127.0.0.1` across two terminals on the same
machine.

---

## 1. Prerequisites

- **OS**: Linux (MP-SPDZ builds most easily here; the code was tested on
  Ubuntu-family distros)
- **Python**: 3.10+
- **Build tools**: `git`, `make`, `g++`, `libsodium`, `libssl-dev`, `libgmp-dev`
  (needed to compile MP-SPDZ)

Install the Python dependencies:

```bash
pip install torch transformers tenseal
```

> `tenseal` provides CKKS homomorphic encryption. `transformers` + `torch`
> are used only for the plaintext BERT-Tiny embedding step.

---

## 2. Get and build MP-SPDZ

The `yao-party.x` binary (Yao's garbled circuits) and the `compile.py`
compiler used by this project come from MP-SPDZ.

```bash
git clone https://github.com/data61/MP-SPDZ.git
cd MP-SPDZ
make -j4 yao-party.x
```

This can take a while the first time. Once it finishes, you should have an
executable at `MP-SPDZ/yao-party.x`.

---

## 3. Lay out the project

Put all six files from this repo directly **inside your MP-SPDZ directory**
(the scripts assume `yao-party.x` and the `Programs/` folder are siblings of
`compare.mpc`, via `MP_SPDZ_DIR = "."` in `client.py` / `server.py`):

```
MP-SPDZ/
├── yao-party.x          <- built in step 2
├── compare.mpc           <- from this repo
├── client.py              <- from this repo
├── server.py              <- from this repo
├── network.py             <- from this repo
├── saas.py                <- from this repo
└── plaintext_weights.pkl  <- from this repo
```

(If you'd rather keep this repo separate, just set `MP_SPDZ_DIR` in both
`client.py` and `server.py` to the absolute path of your MP-SPDZ checkout,
and copy `compare.mpc` into `MP-SPDZ/Programs/Source/`.)

---

## 4. Compile the comparison circuit

From inside the MP-SPDZ directory:

```bash
cp compare.mpc Programs/Source/
./compile.py -Y compare
```

The `-Y` flag compiles for Yao's garbled circuits (matches `yao-party.x` used
in `client.py` / `server.py`). This produces the bytecode MP-SPDZ needs; you
only have to do it once (recompile only if you edit `compare.mpc`).

---

## 5. Run the demo

Open **two terminals**, both `cd`'d into the MP-SPDZ directory from step 3.

**Terminal 1 — start the server first:**

```bash
python3 server.py
```

You should see it load/encrypt the weights (or reuse a cache), then:

```
[Server] Listening for clients on port 5001...
```

**Terminal 2 — run the client:**

```bash
python3 client.py
```

The client will:
- connect to the server and either download the encrypted weights or reuse
  its local cache,
- run BERT-Tiny locally on the hardcoded `INPUT_TEXT` in `client.py`,
- run the encrypted forward pass,
- blind the result and send it to the server,
- trigger the MP-SPDZ garbled-circuit comparison alongside the server.

Back in **Terminal 1**, after the comparison finishes, the server will print
the resolved flag and the SAAS's decision for that "day":

```
[Server] SMPC resolved flag (score > threshold) = True
[SAAS] window=1/7 days, window_mean=1.0, tier=HIGH
[Server] SAAS assigned tier for this sliding window: HIGH
```

The server keeps running afterward — you can run `client.py` again (edit
`INPUT_TEXT` first to simulate a different day) to feed more days into the
sliding window and watch the tier evolve.

---

## 6. What gets created on disk

| Path | Created by | Purpose |
|---|---|---|
| `server_cache/` | server | Persisted CKKS contexts + encrypted weights, so the server doesn't re-encrypt on restart |
| `client_cache/` | client | Cached public context + encrypted weights, so the client doesn't re-download them every connection |
| `saas_data/history.json` | server (`saas.py`) | Rolling window of the last 7 daily flags |
| `saas_data/longitudinal_tracking.json` | server (`saas.py`) | Full history of tier decisions over time |
| `saas_data/notifications.log` | server (`saas.py`) | Simulated notification payloads (tier, category, timestamp only — never raw text/score) |
| `Player-Data/` | client + server | MP-SPDZ input shares for the garbled circuit |

Delete `server_cache/` and `client_cache/` if you change
`plaintext_weights.pkl` and want to force a fresh download on the client
(the version hash will already force this automatically, but a clean wipe is
useful for testing).

---

## 7. Notes and known limitations

- **Single-machine demo.** Both `HOST` values are `127.0.0.1`. To run
  client and server on separate machines, change `HOST` in both files to the
  server's real IP/hostname and open port `5001` (and MP-SPDZ's port
  `6010`) between them.
- **Single simulated user.** `saas_data/` is not keyed by user ID — it
  represents one person's history. For multiple users, namespace the
  `saas_data` files (or the whole cache directories) by a client/user ID.
- **`compare.mpc` reveals more than the flag.** The circuit currently does
  `print_ln` on `A`, `t`, `r`, and the result `b` — all in plaintext. Only
  `b` (the comparison result) is meant to be public; `A`, `t`, and `r`
  leaking means the blinding is defeated at the MP-SPDZ layer even though
  CKKS hid the score beforehand. If this matters for your threat model,
  remove the `A`/`t`/`r` reveal lines and keep only the result line.
- **HIGH-tier threshold is a placeholder.** The paper only specifies the
  overall alert threshold (α = 0.5 window mean); the MEDIUM→HIGH escalation
  point (`HIGH_ALPHA = 0.75` in `saas.py`) is not pinned down in the paper
  and is left as an easily-adjustable constant.
- **No real notification provider.** `saas.py`'s `notify()` prints and logs
  the payload instead of calling Twilio/Firebase — swap in a real client
  where the `TODO` comments are.

---

## 8. File overview

| File | Role |
|---|---|
| `client.py` | Data owner: BERT-Tiny embedding, encrypted inference, blinding, MP-SPDZ party 1 |
| `server.py` | Model owner / system operator: weight encryption + caching, decrypting the blinded value, MP-SPDZ party 0, hands off to SAAS |
| `network.py` | Small length-prefixed socket send/recv helpers shared by client and server |
| `saas.py` | Signal Aggregation and Alerting System: sliding window, tiering, notification, longitudinal tracking |
| `compare.mpc` | MP-SPDZ program: secure comparison `(A − r) > t` |
| `plaintext_weights.pkl` | Trained classifier weights (128→32→1) the server encrypts under CKKS |
