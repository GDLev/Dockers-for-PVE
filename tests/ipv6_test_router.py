#!/usr/bin/env python3
"""Small RA/DHCPv6 server for the isolated integration-test namespace."""
import ipaddress
import socket
import struct
import sys
import threading
import time


def option(code, value):
    return struct.pack('!HH', code, len(value)) + value


def options(packet):
    result = {}
    while len(packet) >= 4:
        code, length = struct.unpack('!HH', packet[:4])
        if len(packet) < length + 4:
            raise ValueError('Truncated DHCPv6 test request')
        result[code] = packet[4:4 + length]
        packet = packet[4 + length:]
    return result


def main():
    interface, mode = sys.argv[1:3]
    index = socket.if_nametoindex(interface)
    prefix = ipaddress.IPv6Address('fd42:bd20:2::').packed
    def advertisements():
        packet = b'\x86\x00\x00\x00' + bytes([64, 0x80 if mode == 'dhcp' else 0]) + struct.pack('!H', 1800) + bytes(8)
        packet += bytes([3, 4, 64, 0x80 if mode == 'dhcp' else 0xc0]) + struct.pack('!II', 600, 300) + bytes(4) + prefix
        with socket.socket(socket.AF_INET6, socket.SOCK_RAW, socket.IPPROTO_ICMPV6) as sender:
            sender.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_IF, index)
            sender.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 255)
            while True:
                try:
                    sender.sendto(packet, ('ff02::1', 0, 0, index))
                except OSError:
                    pass
                time.sleep(.3)
    threading.Thread(target=advertisements, daemon=True).start()
    if mode == 'auto':
        while True:
            time.sleep(1)
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b'\0')
        server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP, socket.inet_pton(socket.AF_INET6, 'ff02::1:2') + struct.pack('I', index))
        server.bind(('::', 547))
        while True:
            packet, peer = server.recvfrom(4096)
            if len(packet) < 4:
                continue
            values = options(packet[4:])
            message = 2 if packet[0] == 1 and 14 not in values else 7
            reply = bytes([message]) + packet[1:4]
            reply += option(2, struct.pack('!HH', 3, 1) + bytes.fromhex('0266778899aa'))
            if 1 in values:
                reply += option(1, values[1])
            if 14 in values and packet[0] == 1:
                reply += option(14, b'')
            if 3 in values:
                address = ipaddress.IPv6Address('fd42:bd20:2::123').packed
                ia = values[3][:4] + struct.pack('!II', 60, 120)
                ia += option(5, address + struct.pack('!II', 300, 600))
                reply += option(3, ia)
            reply += option(13, b'\x00\x00')
            server.sendto(reply, peer)


if __name__ == '__main__':
    main()
