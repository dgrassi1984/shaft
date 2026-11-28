import Foundation

/// An in-memory app API for a single demo run. Only the reserved demo origin is answered.
public final class DemoServer: URLProtocol, @unchecked Sendable {
    static let baseURL = URL(string: "https://demo.shaft.invalid")!
    private static let runHeader = "X-SHAFT-Demo-Run"
    private static let registryLock = NSLock()
    nonisolated(unsafe) private static var runs: [String: DemoState] = [:]
    private let deliveryLock = NSRecursiveLock()
    private var stopped = false
    private var deliveryTask: Task<Void, Never>?

    static func start() -> (id: String, session: URLSession) {
        let id = UUID().uuidString
        registryLock.withLock { runs[id] = DemoState() }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [DemoServer.self, DemoNetworkBlock.self]
        configuration.httpAdditionalHeaders = [runHeader: id]
        return (id, URLSession(configuration: configuration))
    }

    static func finish(_ id: String) {
        _ = registryLock.withLock { runs.removeValue(forKey: id) }
    }

    public override class func canInit(with request: URLRequest) -> Bool {
        request.url?.scheme == "https" && request.url?.host == "demo.shaft.invalid"
            && (request.url?.port == nil || request.url?.port == 443)
    }

    public override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    public override func startLoading() {
        guard let url = request.url,
              let id = request.value(forHTTPHeaderField: Self.runHeader),
              let state = Self.registryLock.withLock({ Self.runs[id] }) else {
            deliver(.unavailable, url: request.url ?? Self.baseURL)
            return
        }
        let body = request.httpBody ?? request.httpBodyStream.map(Self.read) ?? Data()
        let request = self.request
        // Callbacks and cancellation share deliveryLock.
        nonisolated(unsafe) let server = self
        let delivery = Task {
            do {
                let query = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
                if url.path.split(separator: "/").count == 5,
                   url.path.hasPrefix("/app/v1/chat/"), url.path.hasSuffix("/stream"),
                   request.httpMethod == "GET" {
                    guard ["tundra", "wisp", "ada"].contains(String(url.path.split(separator: "/")[3])) else {
                        server.deliver(.unavailable, url: url)
                        return
                    }
                    let stream = state.stream(url: url)
                    guard let stream else {
                        server.deliver(.error(409, code: "not_replying", message: "No reply is running."), url: url)
                        return
                    }
                    let after = Int(query.first { $0.name == "after" }?.value ?? "0") ?? 0
                    let sameReply = query.first { $0.name == "reply" }?.value == stream.reply
                    guard !sameReply || after < stream.events.count else {
                        server.deliver(.error(409, code: "not_replying", message: "No new reply events."), url: url)
                        return
                    }
                    server.emit(stream.status, url: url, type: "text/event-stream")
                    try await Task.sleep(for: .seconds(max(0, stream.ready.timeIntervalSinceNow)))
                    state.completeReplies()
                    for (index, event) in stream.events.enumerated() {
                        if !sameReply || index + 1 > after { server.emit(event) }
                    }
                    server.end()
                } else {
                    // Pace long polls without blocking URLSession's protocol queue.
                    if query.contains(where: { $0.name == "since" }) {
                        try await Task.sleep(for: .milliseconds(300))
                    }
                    let response = state.handle(request, body: body)
                    try Task.checkCancellation()
                    server.deliver(response, url: url)
                }
            } catch { /* stopLoading cancels a pending long poll or stream. */ }
        }
        deliveryLock.withLock {
            if stopped { delivery.cancel() } else { deliveryTask = delivery }
        }
    }

    public override func stopLoading() {
        deliveryLock.withLock {
            stopped = true
            deliveryTask?.cancel()
        }
    }

    private func deliver(_ response: DemoResponse, url: URL) {
        deliveryLock.withLock {
            guard !stopped else { return }
            emit(response.status, url: url, type: response.type)
            emit(response.body)
            end()
        }
    }

    private func emit(_ status: Int, url: URL, type: String) {
        deliveryLock.withLock {
            guard !stopped else { return }
            let http = HTTPURLResponse(url: url, statusCode: status, httpVersion: "HTTP/1.1",
                                       headerFields: ["Content-Type": type])!
            client?.urlProtocol(self, didReceive: http, cacheStoragePolicy: .notAllowed)
        }
    }

    private func emit(_ data: Data) {
        deliveryLock.withLock {
            if !stopped { client?.urlProtocol(self, didLoad: data) }
        }
    }

    private func end() {
        deliveryLock.withLock {
            if !stopped { client?.urlProtocolDidFinishLoading(self) }
        }
    }

    private static func read(_ stream: InputStream) -> Data {
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 65536)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count <= 0 { break }
            data.append(buffer, count: count)
        }
        return data
    }
}

/// Prevents a demo session's requests to any other origin from reaching the network.
private final class DemoNetworkBlock: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool { !DemoServer.canInit(with: request) }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() { client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet)) }
    override func stopLoading() {}
}

private struct DemoResponse {
    var status = 200
    var type = "application/json; charset=utf-8"
    var body: Data

    static func json(_ value: Any) -> Self {
        // Every value is built here from JSON primitives, never from external data.
        Self(body: try! JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]))
    }

    static func error(_ status: Int, code: String = "demo_unavailable", message: String) -> Self {
        var response = json(["error": ["code": code, "message": message]])
        response.status = status
        return response
    }

    static var unavailable: Self { error(404, message: "Not available in the demo.") }
}

/// Mutable wire data belongs to one run; URLProtocol requests serialize access through this lock.
private final class DemoState: @unchecked Sendable {
    private let lock = NSRecursiveLock()
    private let now = Date()
    private let names = ["tundra", "wisp", "ada"]
    private var feeds: [String: [String: Any]] = [:]
    private var order: [String: [String]] = [:]
    private var unread: Set<String> = ["wisp", "ada"]
    private var sends: [String: DemoResponse] = [:]
    private var pending: [String: (reply: String, ready: Date)] = [:]
    private var voices: [String: [String: Any]] = [:]
    private var available: [String: Any] = [:]
    private var revision = 1

    init() {
        for name in names {
            let ids = name == "ada" ? ["ada-ideas", "ada-notes"] : ["\(name)-chat"]
            order[name] = ids
            for (index, cid) in ids.enumerated() {
                let title = name == "ada" ? ["Weekly review", "Field notes"][index]
                    : name == "tundra" ? "Build speed" : "Weekly digest"
                feeds[cid] = DemoData.feed(name, cid: cid, title: title, at: now)
            }
            var from = DemoData.feed(name, cid: "from", title: "From \(name)", at: now)
            from["chat"] = NSNull()
            // From holds what an instance sent on its own (its question), not the chat's turns.
            from["items"] = (from["items"] as? [[String: Any]] ?? []).filter { $0["type"] as? String == "sent" }
            if name == "wisp" {
                from["items"] = [DemoData.digest(at: now.addingTimeInterval(-6 * 86_400))]
                    + (from["items"] as? [[String: Any]] ?? [])
            }
            if name == "ada" {
                from["items"] = [DemoData.assistant("This week's field notes are in Documents, and the weekly review has its own thread.", k: 0, at: now.addingTimeInterval(-180))]
            }
            from["can_clear"] = (from["items"] as? [Any])?.isEmpty == false
            feeds["\(name)-from"] = from
        }
    }

    private func key(_ name: String, _ cid: String?) -> String {
        cid == "from" ? "\(name)-from" : cid ?? order[name]!.first!
    }

    private func chat(_ cid: String) -> [String: Any] { feeds[cid]?["chat"] as? [String: Any] ?? [:] }

    private func touch(_ cid: String) {
        revision += 1
        feeds[cid]?["version"] = String(revision)
        feeds[cid]?["last"] = DemoData.stamp(Date())
    }

    func completeReplies() {
        lock.withLock {
            for (cid, reply) in pending where reply.ready <= Date() {
                var items = feeds[cid]?["items"] as? [[String: Any]] ?? []
                items.append(DemoData.assistant(DemoData.reply, k: items.count, at: reply.ready))
                feeds[cid]?["items"] = items
                feeds[cid]?["reply"] = NSNull()
                feeds[cid]?["live"] = ""
                feeds[cid]?["can_clear"] = true
                if var info = feeds[cid]?["chat"] as? [String: Any] {
                    info["state"] = "idle"
                    feeds[cid]?["chat"] = info
                }
                pending.removeValue(forKey: cid)
                touch(cid)
            }
        }
    }

    struct Stream {
        let status = 200
        let reply: String
        let ready: Date
        let events: [Data]
    }

    func stream(url: URL) -> Stream? {
        lock.withLock {
            let parts = url.path.split(separator: "/").map(String.init)
            guard parts.count == 5, parts[2] == "chat", names.contains(parts[3]) else { return nil }
            let cid = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "cid" }?.value
            guard let running = pending[key(parts[3], cid)] else { return nil }
            let events: [[String: Any]] = [
                ["seq": 1, "type": "status", "reply": running.reply, "phase": "model", "detail": "Sample reply"],
                ["seq": 2, "type": "draft", "reply": running.reply, "text": DemoData.reply],
                ["seq": 3, "type": "step_end", "reply": running.reply, "answer": true],
                ["seq": 4, "type": "answer", "reply": running.reply, "text": DemoData.reply],
                ["seq": 5, "type": "end", "reply": running.reply, "failure": NSNull()]]
            return Stream(reply: running.reply, ready: running.ready, events: events.map {
                let json = DemoResponse.json($0).body
                return Data("id: \($0["seq"]!)\ndata: \(String(decoding: json, as: UTF8.self))\n\n".utf8)
            })
        }
    }

    func handle(_ request: URLRequest, body: Data) -> DemoResponse {
        lock.withLock {
            completeReplies()
            guard let url = request.url, url.path.hasPrefix("/app/v1/") else { return .unavailable }
            let parts = url.path.dropFirst("/app/v1/".count).split(separator: "/").map(String.init)
            let method = request.httpMethod ?? "GET"
            let query = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
            func value(_ name: String) -> String? { query.first { $0.name == name }?.value }
            let json = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any] ?? [:]
            // Always the api this app needs, so a later app never finds its own demo "too old".
            if parts == ["version"], method == "GET" {
                return .json(["server": "demo", "api": max(1, ServerCompatibility.minimumServerAPI)])
            }
            if parts == ["instances"], method == "GET" {
                return .json(["instances": names.map { name -> [String: Any] in
                    ["name": name, "state": "idle", "since": "", "model": "Demo", "usd_today": 0,
                     "line": ["tundra": "Build time: three experiments", "wisp": "How should I send the digest?",
                              "ada": "The field notes are ready in Documents."][name]!,
                     "last": DemoData.stamp(now.addingTimeInterval(-180)), "replying": false,
                     "unread": unread.contains(name), "unread_main": name == "wisp" && unread.contains(name),
                     "unread_threads": unread.contains(name) ? 1 : 0, "threads": order[name]!.count,
                     "threads_rev": String(revision), "next_cycle": "for \(name)'s next cycle", "voice_mode": "on"]
                }, "paused": [], "badge": unread.count, "instances_rev": revision])
            }
            if parts == ["push", "status"], method == "GET" {
                return .json(["on": false, "reason": "Not used in the demo"])
            }
            if parts == ["voices", "report"], method == "POST" {
                if let platform = json["platform"] as? String { available[platform] = json["voices"] ?? [] }
                return .json(["available": available])
            }
            guard parts.count >= 2, names.contains(parts[1]) else { return .unavailable }
            let name = parts[1]
            let action = parts.count == 3 ? parts[2] : ""
            let cid = key(name, (json["cid"] as? String) ?? value("cid"))
            switch (method, parts[0]) {
            case ("GET", "threads") where parts.count == 2:
                return .json(["threads": order[name]!.map { id -> [String: Any] in
                    let info = chat(id)
                    return ["id": id, "title": info["title"] ?? "New thread", "created": info["created"]!,
                            "last": feeds[id]!["last"]!, "line": "Explore a sample conversation", "state": info["state"]!,
                            "unread": unread.contains(name), "waiting": NSNull()]
                }, "from": ["line": name == "wisp" ? "A question for you" : "", "last": DemoData.stamp(now),
                             "unread": name == "wisp" && unread.contains(name)], "threads_rev": String(revision)])
            case ("GET", "chats") where parts.count == 2:
                return .json(["chats": order[name]!.map { id in
                    ["id": id, "title": chat(id)["title"]!, "created": chat(id)["created"]!,
                     "ended": DemoData.stamp(now), "excerpt": chat(id)["title"]!, "messages": (feeds[id]!["items"] as? [Any])!.count] as [String: Any]
                }])
            case ("GET", "notes") where parts.count == 2: return .json(["notes": []])
            case ("GET", "chat") where parts.count <= 3,
                 ("GET", "from") where parts.count == 2:
                let id = parts[0] == "from" ? "\(name)-from" : action.isEmpty ? cid : key(name, action)
                guard var feed = feeds[id], feed["instance"] as? String == name else { return .unavailable }
                if let earlier = feed["earlier"] as? Int, earlier > 0 {
                    if value("all") == "1" {
                        feed["earlier"] = 0
                    } else if let items = feed["items"] as? [[String: Any]] {
                        feed["items"] = Array(items.dropFirst(earlier))
                    }
                }
                return .json(feed)
            case ("POST", "chat"):
                guard feeds[cid]?["instance"] as? String == name else { return .unavailable }
                switch action {
                case "send":
                    let fields = Self.fields(body, contentType: request.value(forHTTPHeaderField: "Content-Type") ?? "")
                    let target = key(name, fields["cid"])
                    guard feeds[target]?["instance"] as? String == name, let mid = fields["mid"], let text = fields["text"] else { return .unavailable }
                    if let sent = sends[mid] { return sent }
                    guard pending[target] == nil else { return .error(409, code: "busy", message: "Wait for the sample reply.") }
                    var items = feeds[target]!["items"] as! [[String: Any]]
                    items.append(DemoData.user(text, k: items.count, mid: mid, at: Date(), voice: fields["voice"] == "1"))
                    feeds[target]?["items"] = items
                    let mids = (feeds[target]?["mids"] as? [String] ?? []) + [mid]
                    feeds[target]?["mids"] = mids
                    let started = Date()
                    pending[target] = (mid, started.addingTimeInterval(1.5))
                    feeds[target]?["reply"] = ["started": DemoData.stamp(started), "activity": [], "answer": ""]
                    feeds[target]?["live"] = mid
                    feeds[target]?["can_clear"] = false
                    if var info = feeds[target]?["chat"] as? [String: Any] {
                        info["state"] = "replying"
                        feeds[target]?["chat"] = info
                    }
                    touch(target)
                    let sent = DemoResponse.json(["said": "sent", "chat": fields["cid"] == "from" ? "from" : target])
                    sends[mid] = sent
                    return sent
                case "answer":
                    guard name == "wisp", json["file"] as? String == "demo-question.md",
                          let picked = json["picked"] as? [Int], picked.count <= 1,
                          picked.allSatisfy({ (0..<3).contains($0) }),
                          !picked.isEmpty || !(json["other"] as? String ?? "").isEmpty else { return .unavailable }
                    let answer: [String: Any] = ["picked": picked, "other": json["other"] ?? NSNull(), "ts": DemoData.stamp(Date())]
                    for id in ["wisp-chat", "wisp-from"] {
                        var items = feeds[id]!["items"] as! [[String: Any]]
                        if let index = items.firstIndex(where: { $0["file"] as? String == "demo-question.md" }) {
                            if let existing = items[index]["answer"] as? [String: Any] {
                                return .json(["said": "answered", "answer": existing])
                            }
                            items[index]["answer"] = answer
                            items[index]["open"] = false
                            feeds[id]?["items"] = items
                            touch(id)
                        }
                    }
                    unread.remove(name)
                    return .json(["said": "answered", "answer": answer])
                case "seen":
                    unread.remove(name)
                    return .json(["unread": false, "badge": unread.count])
                case "away": return .json(["ok": true])
                case "new":
                    let id = "demo-\(UUID().uuidString.lowercased())"
                    let title = json["title"] as? String ?? "New thread"
                    var feed = DemoData.feed(name, cid: id, title: title, at: Date())
                    feed["items"] = []
                    feed["mids"] = []
                    feed["can_clear"] = false
                    feeds[id] = feed
                    order[name]?.insert(id, at: 0)
                    revision += 1
                    return .json(["chat": ["id": id, "title": title], "said": "created"])
                case "rename":
                    guard let title = json["title"] as? String, !chat(cid).isEmpty else { return .unavailable }
                    var info = chat(cid)
                    info["title"] = title
                    feeds[cid]?["chat"] = info
                    touch(cid)
                    return .json(["said": "renamed"])
                case "clear":
                    guard feeds[cid]?["can_clear"] as? Bool == true,
                          var items = feeds[cid]?["items"] as? [[String: Any]] else {
                        return .error(409, code: "refused", message: "There is nothing to clear or a reply is running.")
                    }
                    let index = items.count { $0["type"] as? String == "cleared" }
                    let stamp = DemoData.stamp(Date())
                    feeds[cid]?["earlier"] = items.count
                    items.append(["key": "cleared-\(index)", "type": "cleared", "ts": stamp])
                    feeds[cid]?["items"] = items
                    feeds[cid]?["can_clear"] = false
                    if var info = feeds[cid]?["chat"] as? [String: Any] {
                        info["cleared"] = stamp
                        feeds[cid]?["chat"] = info
                    }
                    revision += 1
                    feeds[cid]?["version"] = String(revision)
                    return .json(["said": "cleared"])
                default: return .unavailable
                }
            case ("GET", "docs") where parts.count == 2:
                return .json(["dir": "notes/docs", "max_bytes": 262144, "docs": name == "ada" ?
                    [["path": "field-notes.md", "bytes": DemoData.document.utf8.count, "mtime": DemoData.stamp(now),
                      "sha": DemoData.documentSHA, "too_big": false, "open_comments": 0, "in_chat": true]] : []])
            case ("GET", "docs") where parts.count == 3 && name == "ada" && value("path") == "field-notes.md":
                if action == "raw" {
                    return DemoResponse(type: "text/markdown; charset=utf-8", body: Data(DemoData.document.utf8))
                }
                guard action == "doc" else { return .unavailable }
                return .json(["path": "field-notes.md", "text": DemoData.document, "sha": DemoData.documentSHA,
                              "in_chat": true, "threads": []])
            case ("GET", "artifacts") where parts.count == 2:
                var entry = Self.artifact
                entry["html"] = nil
                entry["mtime"] = DemoData.stamp(now)
                entry["bytes"] = Self.artifactHTML.utf8.count
                return .json(["dir": "notes/artifacts", "artifacts": name == "tundra" ? [entry] : []])
            case ("GET", "artifacts") where parts.count == 3 && action == "page":
                guard name == "tundra", value("slug") == Self.artifact["slug"] as? String else {
                    return .error(404, code: "not_found", message: "Artifact not found.")
                }
                return .json(Self.artifact)
            case ("GET", "plan") where parts.count == 2: return .json(DemoData.plan())
            case ("GET", "cycles"):
                let goals = ["Benchmark three build changes", "Draft the cache change", "Write this week's field notes"]
                if parts.count == 3, let index = Int(action), goals.indices.contains(index) {
                    return .json(DemoData.cycle(action, at: now.addingTimeInterval(Double(index - 3) * 1800), goal: goals[index]))
                }
                guard parts.count == 2 else { return .unavailable }
                return .json(["days": [["date": String(DemoData.stamp(now).prefix(10)), "skipped_lines": 0,
                                       "counts": ["cycles": 3, "yes": 3, "partial": 0, "no": 0, "none": 0, "judged": 3, "for_you": 1],
                                       "rows": goals.enumerated().reversed().map { index, goal in
                    ["id": String(index), "kind": "cycle", "goal": goal, "claim": "yes", "verdict": "yes",
                     "delivered": index == 2, "origin": "operator", "at": DemoData.stamp(now.addingTimeInterval(Double(index - 3) * 1800))] as [String: Any]
                }]], "judge_error": ""])
            case ("GET", "voices") where parts.count == 2,
                 ("POST", "voices") where parts.count == 2:
                if method == "POST", let language = json["language"] as? String {
                    if json["clear"] as? Bool == true { voices[name]?[language] = nil }
                    else if let identifier = json["identifier"] as? String {
                        voices[name, default: [:]][language] = ["identifier": identifier, "name": "Device voice",
                                                               "by": "operator", "ts": DemoData.stamp(Date())]
                    }
                }
                return .json(["voices": voices[name] ?? [:], "proposals": [], "available": available])
            default: return .unavailable
            }
        }
    }

    private static var artifact: [String: Any] {
        ["slug": "build-times", "title": "Build times", "version": "a1b2c3d4e5f60718",
         "problems": [String](), "html": artifactHTML]
    }

    private static let artifactHTML = """
    <!doctype html>
    <html lang="en"><head>
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>:root { color-scheme: light dark; } body { font: 1rem system-ui; } svg { width: 100%; max-width: 24rem; }</style>
    </head><body>
    <div>Uncached</div>
    <svg viewBox="0 0 240 32" role="img" aria-label="Uncached build time, 400 seconds">
      <rect width="240" height="32" rx="4" fill="#7a858f"/>
    </svg>
    <div>Cached</div>
    <svg viewBox="0 0 240 32" role="img" aria-label="Cached build time, 175 seconds">
      <rect width="105" height="32" rx="4" fill="#2b906e"/>
    </svg>
    <p id="result"></p>
    <script>
      window.SHAFT_DATA = {uncached: 400, cached: 175};
      const {uncached, cached} = window.SHAFT_DATA;
      document.getElementById("result").textContent = Math.round(100 * (1 - cached / uncached)) + "% faster with caching";
    </script>
    </body></html>
    """

    private static func fields(_ body: Data, contentType: String) -> [String: String] {
        guard let boundary = contentType.components(separatedBy: "boundary=").last else { return [:] }
        var fields: [String: String] = [:]
        for part in String(decoding: body, as: UTF8.self).components(separatedBy: "--\(boundary)") {
            guard let split = part.range(of: "\r\n\r\n") else { continue }
            let head = String(part[..<split.lowerBound])
            guard !head.contains("filename="), let start = head.range(of: "name=\""),
                  let end = head[start.upperBound...].firstIndex(of: "\"") else { continue }
            var value = String(part[split.upperBound...])
            if value.hasSuffix("\r\n") { value.removeLast("\r\n".count) }
            fields[String(head[start.upperBound..<end])] = value
        }
        return fields
    }
}
