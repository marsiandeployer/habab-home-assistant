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
