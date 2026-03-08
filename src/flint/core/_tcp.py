from typing import Callable


def _read_tcp_output(sock, on_data: Callable[[bytes], None], on_disconnect: Callable[[], None]) -> None:
    """Read from TCP socket and deliver raw bytes via callbacks."""
    while True:
        try:
            data = sock.recv(4096)
            if not data:
                break
            on_data(data)
        except OSError:
            break
    on_disconnect()
