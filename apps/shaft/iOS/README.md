The iPhone and iPad app: `SHAFTApp.swift` (the entry point and app delegate: push token,
notification taps, no banner for the chat on screen, `shaft://chat/<instance>`) and `IOSRoot.swift` (a phone shows the
chat full screen with a chat switcher; an iPad one split view at every window size, the instances in the sidebar and
the documents as an inspector: open documents put the sidebar away under 1000 pt and become a sheet under 700 pt, and
neither a resize nor a turn loses what is open). The views themselves are in `../Shared/`.

## Share from another app

`../ShareExtension/` adds SHAFT to the system Share sheet. The operator picks an instance, adds an optional line, and
sends shared text, URLs, or files to its current conversation. The extension remembers its last destination and reads
the app's pairing from the shared Keychain. Retry keeps the same message id and destination after an uncertain send.
Unsupported items show a message; an empty share cannot send.

To try it in the simulator, build and install SHAFT-iOS with its embedded ShareExtension-iOS target. Pair SHAFT first,
using the fixture server described in [the app guide](../README.md#try-it-against-a-fixture-server). Open Safari and
share a page, or share a picture from Photos or a PDF from Files. Choose SHAFT under More if it is not visible.
Check the destination chat in SHAFT. Share again to check the remembered instance. Stop the fixture server before
sending to check the error, restart it, and tap Retry to check that one message arrives. Cancel should return to the
host app without sending. A Debug extension permits local HTTP for the fixture server; Release keeps ATS unchanged.
