import SHAFTKit
import SwiftUI
import WebKit

#if os(macOS)
import AppKit
#else
import UIKit
#endif

@MainActor
struct ArtifactWebView {
    let page: ArtifactPage
    let rules: WKContentRuleList

    static func compileRules() async throws -> WKContentRuleList? {
        try await WKContentRuleListStore.default().compileContentRuleList(
            forIdentifier: "shaft-artifact-block-loads",
            encodedContentRuleList: #"[{"trigger":{"url-filter":".*"},"action":{"type":"block"}}]"#
        )
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    private func makeWebView(_ coordinator: Coordinator) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true
        configuration.userContentController.add(rules)
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = coordinator
        view.uiDelegate = coordinator
        view.allowsLinkPreview = false
        view.underPageBackgroundColor = .clear
        #if os(iOS)
        view.isOpaque = false
        view.backgroundColor = .clear
        #endif
        return view
    }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        private var loadedVersion: String?
        private var awaitingInitialLoad = false

        func load(_ page: ArtifactPage, in view: WKWebView) {
            guard loadedVersion != page.version else { return }
            loadedVersion = page.version
            awaitingInitialLoad = true
            view.loadHTMLString(page.html, baseURL: nil)
        }

        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void) {
            let url = action.request.url
            if awaitingInitialLoad, action.navigationType == .other, action.targetFrame?.isMainFrame == true,
               url?.absoluteString == "about:blank" {
                awaitingInitialLoad = false
                decisionHandler(.allow)
                return
            }
            decisionHandler(.cancel)
            if action.navigationType == .linkActivated, let url,
               url.scheme == "https" || url.scheme == "http" {
                #if os(macOS)
                NSWorkspace.shared.open(url)
                #else
                UIApplication.shared.open(url)
                #endif
            }
        }

        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            nil
        }
    }
}

#if os(macOS)
extension ArtifactWebView: NSViewRepresentable {
    func makeNSView(context: Context) -> WKWebView { makeWebView(context.coordinator) }

    func updateNSView(_ view: WKWebView, context: Context) {
        context.coordinator.load(page, in: view)
    }
}
#else
extension ArtifactWebView: UIViewRepresentable {
    func makeUIView(context: Context) -> WKWebView { makeWebView(context.coordinator) }

    func updateUIView(_ view: WKWebView, context: Context) {
        context.coordinator.load(page, in: view)
    }
}
#endif
