import AgentScreenCore
import AppKit
import ApplicationServices

/// Windows the user grabbed from the peek: each comes to his screen and goes back to exactly
/// where it was when he sends it back or leaves it unused (`GrabBook` says when).
enum Grabs {
  static var book = GrabBook()
  private static var timer: Timer?
  /// Called on the main queue after a grab or a return.
  static var onChange: () -> Void = {}

  /// Grabs the frontmost window on an agent display at `point` (global, top-left origin).
  @discardableResult
  static func grab(at point: CGPoint) -> Bool {
    guard let w = windowsOnAgentDisplays(onScreenOnly: true).first(where: { $0.frame.contains(point) }) else { return false }
    return grab(w.id, pid: w.pid)
  }

  @discardableResult
  static func grab(_ id: CGWindowID, pid: pid_t) -> Bool {
    Battery.resume(pid)
    guard let app = NSRunningApplication(processIdentifier: pid), let ax = axWindow(pid: pid, id: id),
          let home = WindowGuard.moveToUser(ax, app: app) else { return false }
    book.grab(windowID: id, pid: pid, home: home, safari: app.bundleIdentifier == "com.apple.Safari", at: Date())
    AXUIElementPerformAction(ax, kAXRaiseAction as CFString)
    app.activate()
    schedule()
    onChange()
    return true
  }

  static func sendBack(_ id: CGWindowID) {
    guard let g = book.release(id) else { return }
    if let ax = axWindow(pid: g.pid, id: g.windowID) {
      WindowGuard.returningWindow(g.windowID)
      setFrame(ax, g.home)
      log("sent a \(NSRunningApplication(processIdentifier: g.pid)?.localizedName ?? "pid \(g.pid)") window back to the agents' screen")
    }
    schedule()
    onChange()
  }

  static func sendAllBack() {
    for g in book.grabs { sendBack(g.windowID) }
  }

  /// Checks grabs every few seconds while there are any: notes which he is using, forgets
  /// closed ones, and sends back those left unused.
  private static func schedule() {
    guard !book.grabs.isEmpty else {
      timer?.invalidate()
      timer = nil
      return
    }
    guard timer == nil else { return }
    let t = Timer(timeInterval: 5, repeats: true) { _ in check() }
    t.tolerance = 1
    RunLoop.main.add(t, forMode: .common)
    timer = t
  }

  private static func check() {
    let now = Date()
    var frames: [CGWindowID: CGRect] = [:]
    for w in serverWindows([.optionAll]) where book.contains(w.id) { frames[w.id] = w.frame }
    let before = book.grabs.count
    book.keep(alive: Set(frames.keys))
    let cursor = CGEvent(source: nil)?.location ?? .zero
    let front = NSWorkspace.shared.frontmostApplication?.processIdentifier
    for g in book.grabs {
      let focused = g.pid == front
        && axElement(axApp(g.pid), kAXFocusedWindowAttribute).flatMap(windowID) == g.windowID
      if focused || frames[g.windowID]?.contains(cursor) == true { book.used(g.windowID, at: now) }
    }
    for g in book.due(at: now) { sendBack(g.windowID) }
    if book.grabs.count != before { onChange() }
    schedule()
  }
}
