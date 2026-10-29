# Wake benchmark checkpoint, 2026-10-29

Refs #43. This records a reproducible failure baseline, not a detector fix.

## Installed source and private data

Huawei P30 runs APK 49 (`0.1.49-wake-bias`), built from source
`2c4d33eb87d6ec0d4bb14c86bf7d59746bc867f6`. That source is already included
in published main through `d329410644b56a3509af1595bc7fcc15af4b01f6`.

The owner-authorized recordings remain in a private local benchmark outside Git.
All 28 audio SHA-256 values were verified. There are 6 owner-labelled intentional
activations, 9 false activations and 13 unlabelled recordings. Review markers
alone are not false-activation labels. Raw audio, transcripts, timestamps and
installation credentials are deliberately excluded from this checkpoint.

## Complete installed-detector replay

The 15 labelled examples were run through the installed APK detector on the
phone in two bounded batches, without microphone capture, playback, provider
requests or HA commands.

| Ground truth | Examples | Accepted | Rejected |
| --- | ---: | ---: | ---: |
| Intentional activation | 6 | 6 | 0 |
| False activation | 9 | 4 | 5 |

Both latest owner-reported false activations were reproduced. All four false
acceptances required appended silence in this replay. One intentional example
also required appended silence, so removing finalization at silence is not
a demonstrated solution.

The private complete report is named `20261029-combined-installed49.json`.
The preceding single-batch report timed out and must not be counted as a
complete run.

## Limits and next experiment

These are selected eight-second wake clips, not continuous negative-room audio.
The replay starts each clip with fresh VAD state and may append two seconds of
silence. Some preceding decoder context is absent. The counts are not a measured
false-alarm rate per hour and do not establish real-room acceptance.

Increasing unknown-word prior previously lost a positive example. Independent
full-vocabulary confirmation rejected most positives. Next evaluate alternate
decoder hypotheses and score separation against the unchanged labelled set,
then hold out future room recordings before changing live behavior. Do not
choose arbitrary lexical exclusions from the recorded conversation.

## How to test

With the private benchmark and installed-detector replay helper, verify every
audio hash, run all 15 labelled examples and require a complete report. Compare
with 6/6 intentional and 4/9 false accepted. Preserve unlabelled examples without
assigning automatic ground truth. No audio belongs in the public repository.
