The iPhone and iPad app (PLAN-native-apps.md M4): `SHAFTApp.swift` (the entry point and app delegate: push token,
notification taps, no banner for the chat on screen, `shaft://chat/<instance>`) and `IOSRoot.swift` (a phone shows the
chat full screen with a chat switcher; an iPad a split view with the instances in the sidebar and the documents as an
inspector). The views themselves are in `../Shared/`.
