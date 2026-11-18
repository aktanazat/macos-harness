import AgentScreenCore
import AppKit
import Darwin

/// What agents are doing, from the datagrams macos-harness sends while the companion listens:
/// each agent's arrow and the trail of its actions.
enum Activity {
  struct Pointer {
    var from: CGPoint
    var to: CGPoint
    var movedAt: Date
    var duration: TimeInterval
    var clickedAt: Date?
    var lastEvent: Date

    func position(at now: Date) -> CGPoint {
      guard duration > 0 else { return to }
      let p = min(1, max(0, now.timeIntervalSince(movedAt) / duration))
      let eased = 1 - pow(1 - p, 3)
      return CGPoint(x: from.x + (to.x - from.x) * eased, y: from.y + (to.y - from.y) * eased)
    }
  }

  /// An arrow left still this long fades from the peek, as the live arrow hides.
  static let pointerLinger: TimeInterval = 4

  private(set) static var trail = Trail()
  private(set) static var pointers: [String: Pointer] = [:]
  /// Agent names by agent session (harness) pid, learned from `--agent-id`.
  private static var namesByHost: [pid_t: String] = [:]
  private static var senders: [pid_t: (name: String, host: pid_t?)] = [:]
  private static var source: DispatchSourceRead?
  /// Called on the main queue after anything changes.
  static var onChange: () -> Void = {}

  static var socketPath: String {
    let base = ProcessInfo.processInfo.environment["MACOS_HARNESS_HOME"].map { ($0 as NSString).expandingTildeInPath }
      ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/macos-harness").path
    return (base as NSString).appendingPathComponent("activity.sock")
  }

  static func start() {
    let path = socketPath
    try? FileManager.default.createDirectory(atPath: (path as NSString).deletingLastPathComponent,
                                             withIntermediateDirectories: true)
    unlink(path)
    let fd = socket(AF_UNIX, SOCK_DGRAM, 0)
    guard fd >= 0 else { return log("activity socket: \(String(cString: strerror(errno)))") }
    var addr = sockaddr_un()
    addr.sun_family = sa_family_t(AF_UNIX)
    let bytes = Array(path.utf8)
    guard bytes.count < MemoryLayout.size(ofValue: addr.sun_path) else {
      close(fd)
      return log("activity socket path too long: \(path)")
    }
    withUnsafeMutableBytes(of: &addr.sun_path) { buf in
      buf.copyBytes(from: bytes)
      buf[bytes.count] = 0
    }
    let old = umask(0o077)
    let bound = withUnsafePointer(to: &addr) {
      $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
    }
    umask(old)
    guard bound == 0 else {
      close(fd)
      return log("activity socket bind: \(String(cString: strerror(errno)))")
    }
    var size: Int32 = 256 * 1024
    setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &size, socklen_t(MemoryLayout<Int32>.size))
    _ = fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK)
    let src = DispatchSource.makeReadSource(fileDescriptor: fd, queue: .main)
    src.setEventHandler { drain(fd) }
    src.setCancelHandler {
      close(fd)
      unlink(path)
    }
    src.resume()
    source = src
  }

  /// Removes the socket now: the process exits right after, before a cancel handler would run.
  static func stop() {
    source?.cancel()
    unlink(socketPath)
  }

  private static func drain(_ fd: Int32) {
    var buffer = [UInt8](repeating: 0, count: 8192)
    var changed = false
    while true {
      let n = recv(fd, &buffer, buffer.count, 0)
      guard n > 0 else { break }
      if let e = ActivityEvent.decode(Data(buffer[0..<n])) {
        take(e)
        changed = true
      }
    }
    if changed { onChange() }
  }

  private static func take(_ e: ActivityEvent) {
    let name = agentName(sender: e.pid, given: e.agent)
    let at = Date(timeIntervalSince1970: e.t)
    switch e.kind {
    case "pointer":
      let now = Date()
      if e.event == "hide" {
        pointers[name] = nil
        return
      }
      guard let x = e.x, let y = e.y else { return }
      let to = CGPoint(x: x, y: y)
      var p = pointers[name] ?? Pointer(from: to, to: to, movedAt: now, duration: 0, lastEvent: now)
      if e.event == "click" {
        p.clickedAt = now
      } else {
        p.from = p.position(at: now)
        p.to = to
        p.movedAt = now
        p.duration = e.event == "move" ? min(e.duration ?? 0, 1) : 0
      }
      p.lastEvent = now
      pointers[name] = p
    case "action":
      guard let op = e.op, !TrailWording.quiet.contains(op) else { return }
      if let app = e.appPid { Battery.agentActed(on: app) }
      let appName = e.appPid.flatMap { NSRunningApplication(processIdentifier: $0)?.localizedName } ?? e.app
      let title = e.windowID.flatMap(windowTitle)
      let text = TrailWording.phrase(op: op, target: e.target, app: appName, window: title, key: e.key,
                                     failed: e.outcome == "failed")
      trail.add(TrailEntry(time: at, agent: name, senderPid: e.pid, appPid: e.appPid,
                           fromReceipt: e.source == "receipt", text: text, count: 1),
                receiptDuration: e.durationS ?? 0)
    default:
      return
    }
  }

  /// Drops arrows that have been still past `pointerLinger`; true while one is still moving.
  static func prunePointers(at now: Date) -> Bool {
    pointers = pointers.filter { now.timeIntervalSince($0.value.lastEvent) < pointerLinger }
    return pointers.values.contains { now.timeIntervalSince($0.movedAt) < $0.duration + 0.15 }
      || pointers.values.contains { $0.clickedAt.map { now.timeIntervalSince($0) < 0.2 } ?? false }
  }

  private static func agentName(sender: pid_t, given: String?) -> String {
    let host = senders[sender]?.host ?? agentHost(of: sender)?.pid
    if let given, !given.isEmpty {
      if let host { namesByHost[host] = given }
      senders[sender] = (given, host)
      return given
    }
    if let known = senders[sender] { return known.name }
    let name = host.map(sessionName) ?? processFacts(sender)?.name ?? "pid \(sender)"
    senders[sender] = (name, host)
    if senders.count > 256 { senders = senders.filter { kill($0.key, 0) == 0 } }
    return name
  }

  /// The name of an agent session, by its harness pid: the `--agent-id` its tools used, else
  /// the harness and the folder it works in.
  static func sessionName(_ host: pid_t) -> String {
    if let given = namesByHost[host] { return given }
    let facts = processFacts(host)
    return Labels.session(host: facts?.name ?? "agent", pid: host, cwd: processCwd(host))
  }
}
