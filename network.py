import socket
import struct
import pickle

def send_msg(sock, msg_bytes):
    msg_len = struct.pack('>I', len(msg_bytes))
    sock.sendall(msg_len + msg_bytes)

def recv_msg(sock):
    raw_msglen = recvall(sock, 4)
    if not raw_msglen:
        return None
    msglen = struct.unpack('>I', raw_msglen)[0]
    return recvall(sock, msglen)

def recvall(sock, n):
    data = bytearray()
    while len(data) < n:
        packet = sock.recv(n - len(data))
        if not packet:
            return None
        data.extend(packet)
    return bytes(data)

def send_obj(sock, obj):
    send_msg(sock, pickle.dumps(obj))

def recv_obj(sock):
    data = recv_msg(sock)
    return pickle.loads(data) if data else None
