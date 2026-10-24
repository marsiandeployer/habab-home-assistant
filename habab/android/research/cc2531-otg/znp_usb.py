"""Small offline model of CDC-ACM interface selection and TI ZNP framing.

This is a reference/test module only. It does not open USB devices or send radio
commands; Android UsbManager remains the intended owner of a physical device.
"""

from __future__ import annotations

from dataclasses import dataclass


USB_CLASS_COMM = 0x02
USB_CDC_SUBCLASS_ACM = 0x02
USB_CLASS_DATA = 0x0A
USB_ENDPOINT_XFER_BULK = 0x02
ZNP_SOF = 0xFE
ZNP_MAX_PAYLOAD = 250


@dataclass(frozen=True)
class Endpoint:
    address: int
    transfer_type: int

    @property
    def is_in(self) -> bool:
        return bool(self.address & 0x80)


@dataclass(frozen=True)
class Interface:
    number: int
    interface_class: int
    subclass: int
    protocol: int
    endpoints: tuple[Endpoint, ...]
    union_data_interface: int | None = None


@dataclass(frozen=True)
class CdcAcmPair:
    control_interface: int
    data_interface: int
    bulk_in: int
    bulk_out: int


def find_cdc_acm_pair(interfaces: tuple[Interface, ...]) -> CdcAcmPair | None:
    """Find CDC-ACM control + bulk-data interfaces; reject generic serial lookalikes.

    Android must separately check USB permission, claim both interfaces, issue
    CDC line/control requests, and handle detach. No VID/PID is assumed here.
    """
    controls = [
        i for i in interfaces
        if i.interface_class == USB_CLASS_COMM
        and i.subclass == USB_CDC_SUBCLASS_ACM
        and i.union_data_interface is not None
    ]
    data_interfaces = [i for i in interfaces if i.interface_class == USB_CLASS_DATA]
    for control in controls:
        for data in data_interfaces:
            bulk = [e for e in data.endpoints if e.transfer_type == USB_ENDPOINT_XFER_BULK]
            ins = [e for e in bulk if e.is_in]
            outs = [e for e in bulk if not e.is_in]
            if data.number == control.union_data_interface and len(ins) == 1 and len(outs) == 1:
                return CdcAcmPair(control.number, data.number, ins[0].address, outs[0].address)
    return None


def encode_znp_frame(cmd0: int, cmd1: int, payload: bytes = b"") -> bytes:
    """Encode one ZNP MT serial frame (SOF, LEN, CMD0, CMD1, DATA, XOR FCS)."""
    if not 0 <= cmd0 <= 0xFF or not 0 <= cmd1 <= 0xFF:
        raise ValueError("command bytes must be in range 0..255")
    if len(payload) > ZNP_MAX_PAYLOAD:
        raise ValueError("ZNP payload exceeds 250 bytes")
    body = bytes((len(payload), cmd0, cmd1)) + payload
    fcs = 0
    for value in body:
        fcs ^= value
    return bytes((ZNP_SOF,)) + body + bytes((fcs,))


class ZnpStreamDecoder:
    """Incremental decoder for arbitrary USB bulk-IN chunk boundaries."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buffer.extend(chunk)
        frames: list[bytes] = []
        while True:
            try:
                start = self._buffer.index(ZNP_SOF)
            except ValueError:
                self._buffer.clear()
                break
            if start:
                del self._buffer[:start]
            if len(self._buffer) < 5:
                break
            length = self._buffer[1]
            if length > ZNP_MAX_PAYLOAD:
                del self._buffer[0]
                continue
            frame_length = length + 5
            if len(self._buffer) < frame_length:
                break
            candidate = bytes(self._buffer[:frame_length])
            fcs = 0
            for value in candidate[1:-1]:
                fcs ^= value
            if fcs != candidate[-1]:
                del self._buffer[0]
                continue
            frames.append(candidate)
            del self._buffer[:frame_length]
        return frames
