"""Test-only network guard inherited by every Python subprocess."""
import sys


def block_network(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.sendto'}:
        raise RuntimeError('EDITPPT_OFFLINE_NETWORK_BLOCKED: ' + event)


sys.addaudithook(block_network)
