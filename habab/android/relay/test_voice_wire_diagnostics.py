"""Numeric diagnostics must preserve privacy, frame boundaries and bounded output."""
import json
import unittest
from unittest.mock import patch
from openai_gateway import Gateway, trace_gemini_wire

class VoiceWireDiagnosticsTests(unittest.TestCase):
    def test_coalesced_vad_and_interruption_are_observed_without_payloads(self):
        frame = {'voiceActivity': {'type': 'ACTIVITY_START', 'audioOffset': 'private-offset'},
                 'serverContent': {'interrupted': True, 'inputTranscription': {'text': 'private-transcript'},
                                   'modelTurn': {'parts': [{'inlineData': {'data': 'private-pcm'}}]}}}
        with patch('builtins.print') as output:
            trace_gemini_wire(json.dumps(frame), {'session': 'synthetic-trace'})
        line = output.call_args.args[0]
        record = json.loads(line.removeprefix('HababVoiceWire '))
        self.assertEqual(record['events'], ['vad_start', 'interrupted'])
        self.assertEqual(record['audio_chunks'], 1)
        self.assertNotIn('private-', line)

    def test_binary_json_frames_are_observed_without_payloads(self):
        with patch('builtins.print') as output:
            trace_gemini_wire(b'{"setupComplete":{},"ignored":"private-token"}',
                              {'session': 'synthetic-trace'})
        self.assertIn('setup', output.call_args.args[0])
        self.assertNotIn('private-token', output.call_args.args[0])

    def test_audio_counts_reset_only_at_turn_complete_and_no_per_chunk_logging(self):
        state = {'session': 'synthetic-trace'}
        audio = {'serverContent': {'modelTurn': {'parts': [{'thought': True, 'inlineData': {}}]}}}
        with patch('builtins.print') as output:
            trace_gemini_wire(json.dumps(audio), state)
            output.assert_not_called()
            trace_gemini_wire(json.dumps({'serverContent': {'generationComplete': True}}), state)
            self.assertEqual(state['audio_chunks'], 1)
            trace_gemini_wire(json.dumps({'serverContent': {'turnComplete': True}}), state)
            record = json.loads(output.call_args.args[0].removeprefix('HababVoiceWire '))
            self.assertEqual(record['thought_audio_chunks'], 1)
            self.assertNotIn('audio_chunks', state)

    def test_output_is_bounded_and_malformed_input_does_not_raise(self):
        state = {'session': 'synthetic-trace'}
        with patch('builtins.print') as output:
            for _ in range(160): trace_gemini_wire('{"setupComplete":{}}', state)
            self.assertEqual(output.call_count, 128)
            for raw in ('broken', 'null', '[]', '{"serverContent":{"modelTurn":{"parts":42}}}'):
                trace_gemini_wire(raw, state)
        self.assertFalse(Gateway('a'*64).voice_diagnostics)
