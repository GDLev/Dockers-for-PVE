"""Real IPv6 TCP/UDP clients reach the adapter's IPv4 upstream."""
import socket
import threading
import unittest
import ipv6_proxy


class ProxyTests(unittest.TestCase):
    def test_both_transport_protocols(self):
        for protocol in ('tcp', 'udp'):
            with self.subTest(protocol=protocol):
                kind = socket.SOCK_STREAM if protocol == 'tcp' else socket.SOCK_DGRAM
                upstream = socket.socket(socket.AF_INET, kind)
                upstream.bind(('127.0.0.1', 0)); upstream.settimeout(3)
                if protocol == 'tcp':
                    upstream.listen()
                def respond():
                    if protocol == 'tcp':
                        with upstream.accept()[0] as stream:
                            stream.sendall(stream.recv(100).upper())
                    else:
                        data, peer = upstream.recvfrom(100); upstream.sendto(data.upper(), peer)
                thread = threading.Thread(target=respond, daemon=True); thread.start()
                listener = ipv6_proxy.Listener({'address': '::1', 'port': 0, 'protocol': protocol,
                    'target': '127.0.0.1', 'internal': upstream.getsockname()[1]})
                try:
                    with socket.socket(socket.AF_INET6, kind) as client:
                        client.settimeout(3); client.connect(listener.socket.getsockname())
                        client.sendall(b'endpoint'); self.assertEqual(client.recv(100), b'ENDPOINT')
                    thread.join(3); self.assertFalse(thread.is_alive())
                finally:
                    listener.close(); upstream.close()
