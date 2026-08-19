import socket
import tenseal as ts
import pickle
import subprocess
import os
import hashlib
import re
from network import send_msg, recv_msg, send_obj
from saas import run_saas

HOST = '0.0.0.0'
CLIENT_PORT = 5001
MP_SPDZ_DIR = "."
SERVER_CACHE_DIR = "server_cache"

def compute_weights_hash():
    with open("plaintext_weights.pkl", "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]

def load_and_encrypt():
    print("[Server] Loading Plaintext Weights...")
    with open("plaintext_weights.pkl", "rb") as f:
        weights_dict = pickle.load(f)

    poly_mod_degree = 16384
    coeff_mod_bit_sizes = [60, 40, 40, 40, 40, 40, 40, 60]
    secret_context = ts.context(ts.SCHEME_TYPE.CKKS, poly_mod_degree, -1, coeff_mod_bit_sizes)
    secret_context.global_scale = 2**40
    secret_context.generate_relin_keys()
    secret_context.generate_galois_keys()

    public_context = secret_context.copy()
    public_context.make_context_public()

    encrypted_state_dict = {}
    encrypted_state_dict['w1'] = [ts.ckks_vector(secret_context, row).serialize() for row in weights_dict['w1']]
    encrypted_state_dict['b1'] = [ts.ckks_vector(secret_context, [b]).serialize() for b in weights_dict['b1']]
    encrypted_state_dict['w2'] = [ts.ckks_vector(secret_context, [w]).serialize() for w in weights_dict['w2']]
    encrypted_state_dict['b2'] = ts.ckks_vector(secret_context, weights_dict['b2']).serialize()

    return secret_context, public_context, encrypted_state_dict

def get_encryption_material():
    # Encrypting the weights is the one-time server-side cost (see project notes,
    # ~11.6s). Persisting it to disk means restarting the server doesn't redo that
    # work, and gives clients a stable "version" they can check against so they
    # don't have to re-download the encrypted weights on every connection.
    os.makedirs(SERVER_CACHE_DIR, exist_ok=True)
    weights_hash = compute_weights_hash()
    version_path = f"{SERVER_CACHE_DIR}/version.txt"
    secret_ctx_path = f"{SERVER_CACHE_DIR}/secret_context.bin"
    public_ctx_path = f"{SERVER_CACHE_DIR}/public_context.bin"
    enc_weights_path = f"{SERVER_CACHE_DIR}/enc_weights.pkl"

    cached_version = None
    if os.path.exists(version_path):
        with open(version_path, "r") as f:
            cached_version = f.read().strip()

    if cached_version == weights_hash and os.path.exists(secret_ctx_path):
        print(f"[Server] Reusing cached encryption material (version {weights_hash})")
        with open(secret_ctx_path, "rb") as f:
            secret_context = ts.context_from(f.read())
        with open(public_ctx_path, "rb") as f:
            public_context = ts.context_from(f.read())
        with open(enc_weights_path, "rb") as f:
            enc_weights = pickle.load(f)
        return weights_hash, secret_context, public_context, enc_weights

    print(f"[Server] No valid cache found, encrypting weights (version {weights_hash})")
    secret_context, public_context, enc_weights = load_and_encrypt()

    with open(secret_ctx_path, "wb") as f:
        f.write(secret_context.serialize(save_secret_key=True))
    with open(public_ctx_path, "wb") as f:
        f.write(public_context.serialize())
    with open(enc_weights_path, "wb") as f:
        pickle.dump(enc_weights, f)
    with open(version_path, "w") as f:
        f.write(weights_hash)

    return weights_hash, secret_context, public_context, enc_weights

def parse_gc_result(gc_stdout):
    # compare.mpc prints: "Result of (A - r) > t is: 1" (or 0)
    match = re.search(r"Result of \(A - r\) > t is:\s*(\d+)", gc_stdout)
    if not match:
        return None
    return int(match.group(1)) == 1

def run_server():
    weights_hash, secret_context, public_context, enc_weights = get_encryption_material()

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((HOST, CLIENT_PORT))
    server_sock.listen(1)

    print(f"\n[Server] Listening for clients on port {CLIENT_PORT}...")

    while True:
        conn, addr = server_sock.accept()
        print(f"\n[Server] Client connected from {addr}")

        # Version handshake: only send the (large) encrypted weights if the
        # client doesn't already have this exact version cached locally.
        send_msg(conn, weights_hash.encode())
        client_status = recv_msg(conn).decode()

        if client_status == "SEND":
            print("[Server] Client cache miss/stale - sending encrypted weights...")
            send_msg(conn, public_context.serialize())
            send_obj(conn, enc_weights)
        else:
            print("[Server] Client already has matching cached weights - skipping transfer.")

        enc_A_bytes = recv_msg(conn)
        if not enc_A_bytes:
            conn.close()
            continue

        enc_A = ts.ckks_vector_from(secret_context, enc_A_bytes)
        A = enc_A.decrypt()[0]
        print(f"[Server] Decrypted blinded value A = {A:.4f}")

        # Scale to Integer (Multiply by 100,000)
        t = 0.5
        A_int = int(A * 100000)
        t_int = int(t * 100000)
        print(f"[Server] Scaled blinded value A = {A_int}")
        print(f"[Server] Scaled Threshold t = {t_int}")

        os.makedirs(f"{MP_SPDZ_DIR}/Player-Data", exist_ok=True)
        with open(f"{MP_SPDZ_DIR}/Player-Data/Input-P0-0", "w") as f:
            f.write(f"{A_int} {t_int}\n")

        print(f"[Server] Triggering Garbled Circuit (Party 0)...")
        print(f"[Server] --- MP-SPDZ OUTPUT START ---")
        cmd = ["./yao-party.x", "-p", "0", "-pn", "6010","-h","10.110.30.98", "compare"]
        gc_result = subprocess.run(cmd, cwd=MP_SPDZ_DIR, capture_output=True, text=True)
        print(gc_result.stdout)
        if gc_result.stderr:
            print(gc_result.stderr)
        print(f"[Server] --- MP-SPDZ OUTPUT END ---\n")

        # --- SMPC output -> SAAS handoff ---
        flag = parse_gc_result(gc_result.stdout)
        if flag is None:
            print("[Server] Could not parse SMPC result from MP-SPDZ output, skipping SAAS update.")
            conn.close()
            continue

        print(f"[Server] SMPC resolved flag (score > threshold) = {flag}")

        # The server only ever sees this single boolean flag, never the raw
        # score, embedding, or text (Section 3.2.4 / 6.1).
        tier, window_mean = run_saas(flag)
        print(f"[Server] SAAS assigned tier for this sliding window: {tier}")

        conn.close()

if __name__ == "__main__":
    run_server()
