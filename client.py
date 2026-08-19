import socket
import torch
import tenseal as ts
import time
import random
import subprocess
import os
import re
import pickle
from transformers import BertTokenizer, BertModel
from network import recv_msg, recv_obj, send_msg

os.environ["HF_HUB_ENABLE_HF_XET"] = "0"

HOST = '10.110.30.98'
SERVER_PORT = 5001
MP_SPDZ_DIR = "."
CLIENT_CACHE_DIR = "client_cache"

INPUT_TEXT = "I am very happy"
SIGMOID_POLY = [0.500781, 0.14670403, 0.001198, -0.001006]

def load_cached_weights(server_hash):
    version_path = f"{CLIENT_CACHE_DIR}/version.txt"
    ctx_path = f"{CLIENT_CACHE_DIR}/public_context.bin"
    weights_path = f"{CLIENT_CACHE_DIR}/enc_weights.pkl"

    if not os.path.exists(version_path):
        return None

    with open(version_path, "r") as f:
        cached_hash = f.read().strip()

    if cached_hash != server_hash:
        return None
    if not (os.path.exists(ctx_path) and os.path.exists(weights_path)):
        return None

    with open(ctx_path, "rb") as f:
        public_ctx = ts.context_from(f.read())
    with open(weights_path, "rb") as f:
        enc_weights = pickle.load(f)

    return public_ctx, enc_weights

def save_cached_weights(server_hash, public_ctx_bytes, enc_weights):
    os.makedirs(CLIENT_CACHE_DIR, exist_ok=True)
    with open(f"{CLIENT_CACHE_DIR}/public_context.bin", "wb") as f:
        f.write(public_ctx_bytes)
    with open(f"{CLIENT_CACHE_DIR}/enc_weights.pkl", "wb") as f:
        pickle.dump(enc_weights, f)
    with open(f"{CLIENT_CACHE_DIR}/version.txt", "w") as f:
        f.write(server_hash)

def run_client():
    client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    print(f"[Client] Connecting to server at {HOST}:{SERVER_PORT}...")
    client_sock.connect((HOST, SERVER_PORT))

    server_hash = recv_msg(client_sock).decode()
    print(f"[Client] Server weight version: {server_hash}")

    cached = load_cached_weights(server_hash)
    if cached is not None:
        print("[Client] Matching cached weights found locally - skipping download.")
        send_msg(client_sock, b"CACHED")
        public_ctx, enc_weights = cached
    else:
        print("[Client] No matching local cache - requesting encrypted weights from server.")
        send_msg(client_sock, b"SEND")
        public_ctx_bytes = recv_msg(client_sock)
        public_ctx = ts.context_from(public_ctx_bytes)
        enc_weights = recv_obj(client_sock)
        save_cached_weights(server_hash, public_ctx_bytes, enc_weights)

    enc_w1 = [ts.ckks_vector_from(public_ctx, row) for row in enc_weights['w1']]
    enc_b1 = [ts.ckks_vector_from(public_ctx, b) for b in enc_weights['b1']]
    enc_w2 = [ts.ckks_vector_from(public_ctx, w) for w in enc_weights['w2']]
    enc_b2 = ts.ckks_vector_from(public_ctx, enc_weights['b2'])

    print("[Client] Running Local BERT Embedding...")
    tokenizer = BertTokenizer.from_pretrained("google/bert_uncased_L-2_H-128_A-2")
    from transformers import BertConfig, BertModel

    # 1. Base the config on your local file but manually force the tiny dimensions
    config = BertConfig.from_pretrained(".", local_files_only=True)
    config.hidden_size = 128
    config.num_hidden_layers = 2
    config.num_attention_heads = 2
    config.intermediate_size = 512

    # 2. Load the model with this explicit layout override
    bert = BertModel.from_pretrained(".", config=config, local_files_only=True).eval()

    with torch.no_grad():
        inputs = tokenizer(INPUT_TEXT, return_tensors="pt", padding=True, truncation=True, max_length=128)
        cls_embedding = bert(**inputs).last_hidden_state[:, 0, :]
    inp_flat = cls_embedding.cpu().squeeze().tolist()

    print("[Client] Running Encrypted FHE Inference...")
    out = []
    for w_row, b in zip(enc_w1, enc_b1):
        node_val = w_row.dot(inp_flat) + b
        node_val = node_val.polyval(SIGMOID_POLY)
        out.append(node_val)

    out1 = enc_w2[0] * out[0]
    for i in range(1, 32):
        out1 += (enc_w2[i] * out[i])
    out1 += enc_b2
    out1 = out1.polyval(SIGMOID_POLY)

    # Strictly positive blinding factor to avoid binary two's-complement bugs
    r = random.uniform(10.0, 20.0)
    blinded_out = out1 + r
    print(f"[Client] Random r : {r}")

    print("[Client] Sending blinded output to Server...")
    send_msg(client_sock, blinded_out.serialize())
    client_sock.close()

    # Scale to Integer (Multiply by 100,000)
    r_int = int(r * 100000)
    print(f"[Client] Scaled random value r = {r_int}")
    os.makedirs(f"{MP_SPDZ_DIR}/Player-Data", exist_ok=True)
    with open(f"{MP_SPDZ_DIR}/Player-Data/Input-P1-0", "w") as f:
        f.write(f"{r_int}\n")

    # Give the Server 3 seconds to decrypt and boot up the GC engine
    time.sleep(3)

    print(f"[Client] Triggering Garbled Circuit (Party 1)...")
    print(f"[Client] --- MP-SPDZ OUTPUT START ---")
    cmd = ["./yao-party.x","-p", "1", "-pn", "6010", "-h", HOST, "compare"]
    gc_result = subprocess.run(cmd, cwd=MP_SPDZ_DIR, capture_output=True, text=True)
    print(gc_result.stdout)
    if gc_result.stderr:
        print(gc_result.stderr)
    print(f"[Client] --- MP-SPDZ OUTPUT END ---")

    # Same public result the server sees (compare.mpc reveals it to both
    # parties) -- used only for optional local/in-app display. Aggregation,
    # tiering, and notification (SAAS) run server-side; see saas.py.
    match = re.search(r"Result of \(A - r\) > t is:\s*(\d+)", gc_result.stdout)
    if match:
        flag = int(match.group(1)) == 1
        print(f"[Client] Locally resolved flag = {flag}")

if __name__ == "__main__":
    run_client()
