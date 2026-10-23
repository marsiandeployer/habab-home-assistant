# Components used by Habab Home Android

Habab Home Android uses Home Assistant Core and the downstream Habab components
from this repository. Home Assistant's Apache-2.0 notices remain in the server
image. This is an independent Android server package, not an official Home
Assistant Android application.

The APK packages native tools from Termux. Their source projects and package
recipes are available from:

- https://github.com/termux/proot
- https://github.com/termux/termux-packages/tree/master/packages/proot
- https://github.com/termux/termux-packages/tree/master/packages/libandroid-shmem
- https://github.com/termux/termux-packages/tree/master/packages/libtalloc

Package versions, original download locations and SHA-256 digests are recorded
in native-packages.json. The original copyright/license texts are copied into
app/src/main/assets/native-licenses. prepare_native.py describes the ELF path
changes applied to those binaries. Keep corresponding upstream sources and
these modifications available alongside a distributed release.

The Android application also uses Apache Commons Compress, mwiede/JSch and
net.i2p.crypto/EdDSA. Their notices remain part of their upstream distributions.
OpenCode is included in the separately downloaded runtime with the pinned
ARM64 version and integrity record from distribution/opencode-release.json.

The voice preview includes Vosk Android 0.3.75 (Apache-2.0), JNA 5.18.1
(Apache-2.0 option of its dual license), and OkHttp 4.12.0 (Apache-2.0), with
their transitive dependencies and upstream notices. The offline Russian model
vosk-model-small-ru-0.22 is distributed under Apache-2.0; voice-model.json
records its exact source, size and checksum. Source projects:

- https://github.com/alphacep/vosk-api/tree/v0.3.75
- https://github.com/java-native-access/jna/tree/5.18.1
- https://github.com/square/okhttp/tree/parent-4.12.0
- https://alphacephei.com/vosk/models
