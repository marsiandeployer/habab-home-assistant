import unittest

from znp_usb import (
    Endpoint,
    Interface,
    CdcAcmPair,
    ZnpStreamDecoder,
    encode_znp_frame,
    find_cdc_acm_pair,
)


class CdcAcmSelectionTests(unittest.TestCase):
    def test_selects_control_and_bulk_data_interfaces(self):
        interfaces = (
            Interface(0, 0x02, 0x02, 0x01, (Endpoint(0x83, 0x03),), 1),
            Interface(1, 0x0A, 0, 0, (Endpoint(0x82, 0x02), Endpoint(0x02, 0x02))),
        )
        self.assertEqual(find_cdc_acm_pair(interfaces), CdcAcmPair(0, 1, 0x82, 0x02))

    def test_rejects_generic_non_cdc_serial_interfaces(self):
        interfaces = (
            Interface(0, 0xFF, 0, 0, (Endpoint(0x81, 0x02), Endpoint(0x01, 0x02))),
        )
        self.assertIsNone(find_cdc_acm_pair(interfaces))

    def test_rejects_data_interface_without_bulk_in_out_pair(self):
        interfaces = (
            Interface(0, 0x02, 0x02, 0, (), 1),
            Interface(1, 0x0A, 0, 0, (Endpoint(0x82, 0x02),)),
        )
        self.assertIsNone(find_cdc_acm_pair(interfaces))


class ZnpCodecTests(unittest.TestCase):
    def test_encode_matches_ti_format_and_incremental_decode(self):
        frame = encode_znp_frame(0x21, 0x02, b"\x01\x02")
        self.assertEqual(frame, b"\xFE\x02\x21\x02\x01\x02\x22")
        decoder = ZnpStreamDecoder()
        self.assertEqual(decoder.feed(frame[:3]), [])
        self.assertEqual(decoder.feed(frame[3:]), [frame])

    def test_discards_bad_fcs_and_recovers_next_frame(self):
        bad = bytearray(encode_znp_frame(0x21, 0x02, b"x"))
        bad[-1] ^= 1
        good = encode_znp_frame(0x22, 0x03)
        decoder = ZnpStreamDecoder()
        self.assertEqual(decoder.feed(bytes(bad) + good), [good])

    def test_rejects_oversized_payload(self):
        with self.assertRaises(ValueError):
            encode_znp_frame(0, 0, bytes(251))


if __name__ == "__main__":
    unittest.main()
