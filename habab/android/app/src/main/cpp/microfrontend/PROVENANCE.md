# Native microWakeWord frontend provenance

The files under `tensorflow/lite/experimental/microfrontend/lib/` are the
TensorFlow Lite Micro audio frontend from `pymicro-features` 2.0.2, distributed
under Apache License 2.0. Source archive SHA-256:

`0d0bed7843ec78b6ced82d1a2dcddeb4fe5df61b3af80a281d0868c8e279c727`

The source archive is published at
<https://pypi.org/project/pymicro-features/2.0.2/>. `kiss_fft_int16.cc` embeds
the package's KissFFT sources in a fixed-point namespace; KissFFT is licensed
under the BSD 3-Clause license included beside those sources.

The frontend configuration and output multiplier in `MicroFrontend_jni.cpp`
match `pymicro_features==2.0.2` `src/micro_features.cpp`: 30 ms analysis
window, 10 ms step, 40 channels, 125–7500 Hz, noise reduction and PCAN enabled,
log scale shift 6, and output multiplier 0.0390625. Only the native feature
extractor is included; no TensorFlow Lite Micro interpreter is vendored.
