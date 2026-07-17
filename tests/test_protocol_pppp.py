import socket

from socket import AF_INET
from types import SimpleNamespace

import pytest

from libflagship.cyclic import CyclicU16
from libflagship.pppp import (
    Aabb,
    Duid,
    FileTransfer,
    Host,
    Message,
    P2PCmdType,
    PktDevLgnCrc,
    PktDrw,
    PktDrwAck,
    PktLanSearch,
    PktSessionReady,
    Type,
    Version,
    Xzyh,
)
from libflagship.ppppapi import AnkerPPPPBaseApi, Channel, FileUploadInfo, PPPP_LAN_PORT, PPPP_SOCKET_RCVBUF, PPPP_SOCKET_SNDBUF


def _host(addr="192.168.1.25", port=32108):
    return Host(afam=AF_INET, port=port, addr=addr)


def _duid():
    return Duid(prefix="ABCDEF1", serial=123456, check="CHK01")


def test_host_and_duid_round_trip():
    host = _host()
    duid = _duid()

    parsed_host, rest = Host.parse(host.pack())
    parsed_duid, rest_duid = Duid.parse(duid.pack())

    assert rest == b""
    assert rest_duid == b""
    assert parsed_host.addr == "192.168.1.25"
    assert parsed_host.port == 32108
    assert str(parsed_duid) == "ABCDEF1-123456-CHK01"


def test_xzyh_round_trip():
    pkt = Xzyh(
        cmd=P2PCmdType.P2P_JSON_CMD,
        len=4,
        unk0=1,
        unk1=2,
        chan=3,
        sign_code=4,
        unk3=5,
        dev_type=6,
        data=b"ping",
    )

    parsed, rest = Xzyh.parse(pkt.pack())

    assert rest == b""
    assert parsed.cmd == P2PCmdType.P2P_JSON_CMD
    assert parsed.chan == 3
    assert parsed.data == b"ping"


def test_aabb_crc_round_trip():
    header = Aabb(frametype=FileTransfer.DATA, sn=7, pos=1024, len=4)
    packet = header.pack_with_crc(b"data")

    parsed_header, parsed_data, rest = Aabb.parse_with_crc(packet)

    assert rest == b""
    assert parsed_header.frametype == FileTransfer.DATA
    assert parsed_header.sn == 7
    assert parsed_header.pos == 1024
    assert parsed_data == b"data"


def test_message_parse_round_trip_for_basic_packets():
    lan = PktLanSearch()
    drw = PktDrw(chan=1, index=42, data=b"chunk")
    ack = PktDrwAck(chan=1, count=2, acks=[1, 2])

    parsed_lan, lan_rest = Message.parse(lan.pack())
    parsed_drw, drw_rest = Message.parse(drw.pack())
    parsed_ack, ack_rest = Message.parse(ack.pack())

    assert isinstance(parsed_lan, PktLanSearch)
    assert lan_rest == b""
    assert isinstance(parsed_drw, PktDrw)
    assert drw_rest == b""
    assert parsed_drw.index == 42
    assert parsed_drw.data == b"chunk"
    assert isinstance(parsed_ack, PktDrwAck)
    assert ack_rest == b""
    assert parsed_ack.acks == [1, 2]


def test_encrypted_pppp_packets_round_trip():
    dev_login = PktDevLgnCrc(
        duid=_duid(),
        nat_type=1,
        version=Version(major=1, minor=2, patch=3),
        host=_host(),
    )
    session_ready = PktSessionReady(
        duid=_duid(),
        handle=12,
        max_handles=16,
        active_handles=3,
        startup_ticks=99,
        b1=1,
        b2=2,
        b3=3,
        b4=4,
        addr_local=_host("192.168.1.10", 32108),
        addr_wan=_host("10.0.0.25", 32100),
        addr_relay=_host("8.8.8.8", 32100),
    )

    parsed_login, login_rest = Message.parse(dev_login.pack())
    parsed_ready, ready_rest = Message.parse(session_ready.pack())

    assert isinstance(parsed_login, PktDevLgnCrc)
    assert login_rest == b""
    assert parsed_login.version.patch == 3
    assert parsed_login.host.addr == "192.168.1.25"
    assert isinstance(parsed_ready, PktSessionReady)
    assert ready_rest == b""
    assert parsed_ready.handle == 12
    assert parsed_ready.addr_relay.addr == "8.8.8.8"


def test_file_upload_info_sanitizes_filename_and_generates_md5():
    info = FileUploadInfo.from_data(
        b"gcode-data",
        "../../dangerous file?.gcode",
        user_name="alice",
        user_id="user-1",
        machine_id="machine-1",
    )

    assert info.name == "dangerous_file_.gcode"
    assert info.size == 10
    assert info.md5 == "8a53afd35aeb10af9bbadd86cc711138"
    assert bytes(info).endswith(b"\x00")


def test_channel_write_poll_and_acknowledge():
    chan = Channel(index=2, max_in_flight=2)

    start, done = chan.write(b"a" * 1500, block=False)
    packets = chan.poll()

    assert start == CyclicU16(0)
    assert done == CyclicU16(2)
    assert len(packets) == 2
    assert packets[0].chan == 2
    assert len(packets[0].data) == 1024
    assert len(packets[1].data) == 476

    chan.rx_ack({CyclicU16(0), CyclicU16(1)})

    assert chan.tx_ack == CyclicU16(2)
    assert chan.txqueue == []


def test_channel_write_raises_timeout_instead_of_blocking_forever_when_never_acked():
    # Regression: Channel.write(block=True) with timeout=None waits on a bare
    # Event.wait() with no deadline (see the `else: self.wait()` branch), so a
    # silently-dropped outgoing packet blocks forever. Passing an explicit
    # timeout must take the bounded-deadline branch instead and raise.
    chan = Channel(index=0)

    with pytest.raises(TimeoutError, match="PPPP DRW ACK"):
        chan.write(b"payload", block=True, timeout=0.2)


def test_channel_can_skip_stale_receive_gap_for_realtime_streams():
    chan = Channel(index=1)

    chan.rx_drw(CyclicU16(1), b"middle")
    chan.rx_drw(CyclicU16(2), b"end")

    assert chan.peek(1, timeout=0.0) is None
    assert chan.skip_rx_gap(max_queued=2) is True
    assert chan.read(9, timeout=0.0) == b"middleend"
    assert chan.rx_ctr == CyclicU16(3)


def test_pppp_open_configures_udp_socket_buffers(monkeypatch):
    created = []

    class FakeSocket:
        def __init__(self):
            self.calls = []
            self.binds = []

        def setsockopt(self, level, optname, value):
            self.calls.append((level, optname, value))

        def bind(self, addr):
            self.binds.append(addr)

    monkeypatch.setattr(
        "libflagship.ppppapi.socket.socket",
        lambda family, kind: created.append(FakeSocket()) or created[-1],
    )

    lan_api = AnkerPPPPBaseApi.open(duid=None, host="127.0.0.1", port=32108)
    broadcast_api = AnkerPPPPBaseApi.open_broadcast()

    assert lan_api.addr == ("127.0.0.1", 32108)
    assert broadcast_api.addr == ("255.255.255.255", 32108)
    assert (socket.SOL_SOCKET, socket.SO_RCVBUF, PPPP_SOCKET_RCVBUF) in created[0].calls
    assert (socket.SOL_SOCKET, socket.SO_SNDBUF, PPPP_SOCKET_SNDBUF) in created[0].calls
    assert (socket.SOL_SOCKET, socket.SO_BROADCAST, 1) in created[1].calls
    assert created[0].binds == []
    assert created[1].binds == [('', PPPP_LAN_PORT)]


def test_pppp_remote_close_log_is_rate_limited(monkeypatch):
    api = AnkerPPPPBaseApi(sock=SimpleNamespace(), duid=_duid(), addr=("127.0.0.1", 32108))
    warnings = []

    monkeypatch.setattr(
        "libflagship.ppppapi.log.warning",
        lambda message, *args: warnings.append(message % args if args else message),
    )
    times = iter([0.0, 1.0, 11.0])
    monkeypatch.setattr("libflagship.ppppapi.time.monotonic", lambda: next(times))

    for _ in range(3):
        with pytest.raises(ConnectionResetError):
            api.process(SimpleNamespace(type=Type.CLOSE))

    assert len(warnings) == 2
    assert warnings[0].startswith("PPPP: received CLOSE from remote peer")
    assert warnings[1].endswith("(seen 3 times)")
