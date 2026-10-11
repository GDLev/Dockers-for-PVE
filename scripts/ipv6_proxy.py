#!/usr/bin/env python3
"""IPv6 published-port adapter for the isolated IPv4 rootless runtime."""
import ipaddress
import json
from pathlib import Path
import select
import socket
import threading
import time

CONFIG = Path('/var/lib/betterdocker/ipv6-proxy.json')
SESSIONS = threading.BoundedSemaphore(256)


class Listener:
    def __init__(self, mapping):
        self.mapping = mapping
        self.stop = threading.Event()
        kind = socket.SOCK_STREAM if mapping['protocol'] == 'tcp' else socket.SOCK_DGRAM
        self.socket = socket.socket(socket.AF_INET6, kind)
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((mapping['address'], mapping['port']))
        self.socket.settimeout(.5)
        if kind == socket.SOCK_STREAM:
            self.socket.listen(128)
        self.peers = {}
        self.lock = threading.Lock()
        threading.Thread(target=self.serve, daemon=True).start()

    def close(self):
        self.stop.set(); self.socket.close()
        with self.lock:
            for channel, _ in self.peers.values():
                channel.close()
            self.peers.clear()

    def serve(self):
        while not self.stop.is_set():
            try:
                if self.mapping['protocol'] == 'tcp':
                    client, _ = self.socket.accept()
                    if SESSIONS.acquire(blocking=False):
                        threading.Thread(target=self.tcp, args=(client,), daemon=True).start()
                    else:
                        client.close()
                else:
                    data, peer = self.socket.recvfrom(65535)
                    with self.lock:
                        entry = self.peers.get(peer)
                        if not entry:
                            if not SESSIONS.acquire(blocking=False):
                                continue
                            channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                            channel.settimeout(.5)
                            try:
                                channel.connect((self.mapping['target'], self.mapping['internal']))
                            except OSError:
                                channel.close()
                                SESSIONS.release()
                                raise
                            self.peers[peer] = (channel, time.monotonic())
                            threading.Thread(target=self.udp, args=(peer, channel), daemon=True).start()
                        else:
                            channel = entry[0]
                            self.peers[peer] = (channel, time.monotonic())
                        channel.send(data)
            except socket.timeout:
                continue
            except OSError:
                if self.stop.is_set():
                    return
                time.sleep(.1)

    def tcp(self, client):
        upstream = None
        try:
            client.settimeout(10)
            upstream = socket.create_connection((self.mapping['target'], self.mapping['internal']), timeout=10)
            peers = {client: upstream, upstream: client}
            idle = time.monotonic()
            while not self.stop.is_set() and time.monotonic() - idle < 300:
                ready, _, _ = select.select(list(peers), [], [], .5)
                for channel in ready:
                    data = channel.recv(65536)
                    if not data:
                        return
                    peers[channel].sendall(data); idle = time.monotonic()
        except OSError:
            pass
        finally:
            client.close()
            if upstream:
                upstream.close()
            SESSIONS.release()

    def udp(self, peer, channel):
        try:
            while not self.stop.is_set():
                with self.lock:
                    entry = self.peers.get(peer)
                if not entry or time.monotonic() - entry[1] > 30:
                    break
                try:
                    data = channel.recv(65535); self.socket.sendto(data, peer)
                except socket.timeout:
                    pass
        except OSError:
            pass
        finally:
            with self.lock:
                self.peers.pop(peer, None)
            channel.close(); SESSIONS.release()


def main():
    listeners = {}
    while True:
        # A missing or corrupt configuration closes all endpoints.
        try:
            mappings = json.loads(CONFIG.read_text())
            requested = {}
            for mapping in mappings:
                if ipaddress.ip_address(mapping['address']).version != 6 or ipaddress.ip_address(mapping['target']) not in ipaddress.IPv4Network('10.240.0.0/16') or mapping['protocol'] not in ('tcp', 'udp'):
                    raise ValueError('Invalid proxy mapping')
                if not 1 <= mapping['port'] <= 65535 or not 1 <= mapping['internal'] <= 65535:
                    raise ValueError('Invalid proxy port')
                requested[json.dumps(mapping, sort_keys=True)] = mapping
        except (OSError, ValueError, KeyError, TypeError):
            requested = {}
        for key in list(listeners):
            if key not in requested:
                listeners.pop(key).close()
        for key, mapping in requested.items():
            if key not in listeners:
                try:
                    listeners[key] = Listener(mapping)
                except OSError:
                    # A temporarily missing IPv6 endpoint is retried after refresh.
                    pass
        time.sleep(.5)


if __name__ == '__main__':
    main()
