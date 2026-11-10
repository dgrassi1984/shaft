The iPhone and iPad app: `SHAFTApp.swift` (the entry point and app delegate: push token,
notification taps, no banner for the chat on screen, `shaft://chat/<instance>`) and `IOSRoot.swift` (a phone shows the
chat full screen with a chat switcher; an iPad one split view at every window size, the instances in the sidebar and
the documents as an inspector: open documents put the sidebar away under 1000 pt and become a sheet under 700 pt, and
neither a resize nor a turn loses what is open). The views themselves are in `../Shared/`.
