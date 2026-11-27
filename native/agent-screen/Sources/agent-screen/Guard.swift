import AgentScreenCore
import AppKit
import ApplicationServices

/// Keeps the user's own windows off agent displays, event-driven.
///
/// - The frontmost app is the user's, except an app an agent claimed that came forward without
///   his input or while an agent was acting on it (`Frontmost.isHis`). When it comes forward,
///   opens a window, or a window of it changes focus or stops moving on an agent display, that
///   window moves to the screen he is on.
/// - When displays change (lid closed, monitor unplugged, wake, the agent display's host
///   restarting), macOS moves windows from a lost screen to a remaining one. Afterwards every
///   window on an agent display that no agent owns moves to his screen; when the Agents display
///   came back under a new id (its host restarted), claimed windows macOS left on his screens go
///   back to it.
/// - Agent displays sit diagonally off the bottom-right corner of his screens, touching them at
///   one point only, so his mouse cannot cross over.
/// - Agents own exactly the windows claimed by agent-display `launch`/`move`, and those of the
///   browsers the browser harness drives (`HarnessBrowsers`), whose placement is the harness's.
///   Its sweeps, which run only while agent windows are open, send its windows home and move
///   windows holding only his tabs to his screen; after a display change the companion touches
///   `~/.agent-display/displays-changed`, and a helper that watches it then runs that stray
///   check even with no agent windows open.
/// - When every agent session that started an app has ended, the app is quit.
enum WindowGuard {
  private static var observer: AXObserver?
  private static var observedPid: pid_t = 0
  private static var pendingMoves: [(window: AXUIElement, work: DispatchWorkItem)] = []
  /// Windows the companion just put back on the agents' screen, which the guard must not take
  /// for his while he is still in their app.
  private static var returning: [CGWindowID: Date] = [:]
  /// When an agent last reported acting on each app (harness activity datagrams).
  private static var agentActedAt: [pid_t: Date] = [:]

  static func agentActed(on pid: pid_t) {
    let now = Date()
    agentActedAt = agentActedAt.filter { now.timeIntervalSince($0.value) < Frontmost.agentWindow }
    agentActedAt[pid] = now
  }

  static func start() {
    NSWorkspace.shared.notificationCenter.addObserver(
      forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main
    ) { _ in frontmostChanged() }
    CGDisplayRegisterReconfigurationCallback({ _, flags, _ in
      WindowGuard.displaysChanging(flags)
    }, nil)
    keepAgentDisplaysCornered()
    settledAgents = agentsDisplay()
    frontmostChanged()
    let sweep = Timer(timeInterval: 30, repeats: true) { _ in quitAppsOfEndedAgents() }
    sweep.tolerance = 5
    RunLoop.main.add(sweep, forMode: .common)
  }

  static func ownedByAgents(_ app: NSRunningApplication, window: CGWindowID?) -> Bool {
    HarnessBrowsers.places(bundleID: app.bundleIdentifier) || Claims.owns(app.processIdentifier, window: window)
  }

  /// The companion is putting `id` back on the agents' screen.
  static func returningWindow(_ id: CGWindowID) {
    let now = Date()
    returning = returning.filter { now.timeIntervalSince($0.value) < 3 }
    returning[id] = now
  }

  private static func isReturning(_ id: CGWindowID?) -> Bool {
    guard let id, let at = returning[id] else { return false }
    return Date().timeIntervalSince(at) < 3
  }

  /// Moves every window of the user's apps off agent displays. Returns how many moved.
  @discardableResult
  static func bringAllBack() -> Int {
    var moved = 0
    for w in windowsOnAgentDisplays() {
      guard let app = NSRunningApplication(processIdentifier: w.pid), !ownedByAgents(app, window: w.id),
            let ax = axWindow(pid: w.pid, id: w.id) else { continue }
      moveToUser(ax, app: app)
      moved += 1
    }
    return moved
  }

  /// An agent that joins a claim after this read changes it, so `Claims.end` refuses and the
  /// app stays; one that comes after the mark finds it quitting and starts a fresh copy.
  private static func quitAppsOfEndedAgents() {
    for step in ClaimsFile.sweep(Claims.all(), ownerAlive: { kill($0, 0) == 0 }) {
      switch step {
      case .release(let c):
        if Claims.end(c, quitting: false) {
          log("left \(NSRunningApplication(processIdentifier: c.pid)?.localizedName ?? "pid \(c.pid)") open: it did not quit")
        }
      case .quit(let c):
        quit(c)
      }
    }
  }

  private static func quit(_ c: Claim) {
    guard c.launched, let app = NSRunningApplication(processIdentifier: c.pid) else {
      Claims.end(c, quitting: false)
      return
    }
    Battery.resume(c.pid)
    if axWindows(c.pid).contains(where: { windowID($0).map(c.windows.contains) != true }) {
      if Claims.end(c, quitting: false) {
        log("left \(app.localizedName ?? "pid \(c.pid)") open: the user has his own windows in it")
      }
      return
    }
    guard Claims.end(c, quitting: true) else { return }
    app.terminate()
    log("quit \(app.localizedName ?? "pid \(c.pid)"): the agents that opened it have finished")
  }

  // MARK: frontmost app

  private static func frontmostChanged() {
    guard let app = NSWorkspace.shared.frontmostApplication else { return }
    observe(app.processIdentifier, attempt: 0)
    rescueFocused()
  }

  /// A launching app often refuses notifications until it finishes starting, so retry.
  private static func observe(_ pid: pid_t, attempt: Int) {
    guard pid != observedPid, NSWorkspace.shared.frontmostApplication?.processIdentifier == pid else { return }
    if let old = observer {
      CFRunLoopRemoveSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(old), .defaultMode)
    }
    observer = nil
    observedPid = 0
    pendingMoves.forEach { $0.work.cancel() }
    pendingMoves = []
    var obs: AXObserver?
    let callback: AXObserverCallback = { _, element, name, _ in WindowGuard.windowEvent(name as String, element) }
    let app = axApp(pid)
    let names = [kAXFocusedWindowChangedNotification, kAXMainWindowChangedNotification,
                 kAXWindowCreatedNotification, kAXWindowMovedNotification]
    guard AXObserverCreate(pid, callback, &obs) == .success, let obs,
          names.allSatisfy({ AXObserverAddNotification(obs, app, $0 as CFString, nil) == .success }) else {
      if attempt < 20 {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { observe(pid, attempt: attempt + 1) }
      }
      return
    }
    CFRunLoopAddSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(obs), .defaultMode)
    observer = obs
    observedPid = pid
    rescueFocused()
  }

  private static func windowEvent(_ name: String, _ element: AXUIElement) {
    switch name {
    case kAXWindowCreatedNotification:
      rescue(element)
    case kAXWindowMovedNotification:
      // A drag fires moves continuously; act once this window has been still for a moment,
      // so it is never yanked out from under the pointer.
      pendingMoves.removeAll { pending in
        guard CFEqual(pending.window, element) else { return false }
        pending.work.cancel()
        return true
      }
      let work = DispatchWorkItem {
        pendingMoves.removeAll { CFEqual($0.window, element) }
        rescue(element)
      }
      pendingMoves.append((element, work))
      DispatchQueue.main.asyncAfter(deadline: .now() + 0.4, execute: work)
    default:
      rescueFocused()
    }
  }

  private static func rescueFocused() {
    guard observedPid != 0 else { return }
    let ax = axApp(observedPid)
    guard let window = axElement(ax, kAXFocusedWindowAttribute) ?? axElement(ax, kAXMainWindowAttribute) else { return }
    rescue(window)
  }

  /// Moves a window of the frontmost app off an agent display.
  private static func rescue(_ window: AXUIElement) {
    guard let app = NSWorkspace.shared.frontmostApplication, app.processIdentifier == observedPid else { return }
    var pid: pid_t = 0
    let id = windowID(window)
    guard AXUIElementGetPid(window, &pid) == .success, pid == observedPid,
          (axString(window, kAXSubroleAttribute) ?? kAXStandardWindowSubrole) == kAXStandardWindowSubrole,
          !ownedByAgents(app, window: id), !isReturning(id), inFrontForHim(app) else { return }
    moveToUser(window, app: app)
  }

  private static func inFrontForHim(_ app: NSRunningApplication) -> Bool {
    let pid = app.processIdentifier
    return Frontmost.isHis(appClaimed: Claims.all().contains { $0.pid == pid },
                           sincePerson: secondsSincePersonInput(),
                           sinceAgentActed: agentActedAt[pid].map { Date().timeIntervalSince($0) })
  }

  // MARK: display changes

  /// The Agents display's id as the last display change found it. A restarted agent-display
  /// host comes back under a new id, after macOS moved the agents' windows to his screens.
  private static var settledAgents: CGDirectDisplayID?

  private static func displaysChanging(_ flags: CGDisplayChangeSummaryFlags) {
    guard !flags.contains(.beginConfigurationFlag) else { return }
    let agents = agentsDisplay()
    let hostRestarted = agents != nil && agents != settledAgents
    if agents != nil { settledAgents = agents }
    // macOS relocates windows shortly after the change completes.
    for delay in [1.0, 3.0] {
      DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
        keepAgentDisplaysCornered()
        settleRelocated(returningClaimed: hostRestarted)
        // The Safari helper watches this file and then moves his Safari windows off the
        // Agents display, which the companion leaves to it.
        if delay == 3.0 {
          try? Data().write(to: AgentDisplay.stateDir.appendingPathComponent("displays-changed"))
        }
      }
    }
  }

  /// Moves windows no agent owns off agent displays. After a host restart, also returns claimed
  /// windows macOS left on his screens; at other times a claimed window there is one he moved.
  private static func settleRelocated(returningClaimed: Bool) {
    for w in windowsOnAgentDisplays() {
      guard let app = NSRunningApplication(processIdentifier: w.pid), !ownedByAgents(app, window: w.id) else { continue }
      Battery.resume(w.pid)
      guard let ax = axWindow(pid: w.pid, id: w.id) else { continue }
      moveToUser(ax, app: app)
    }
    guard returningClaimed, let display = agentsDisplay() else { return }
    let bounds = CGDisplayBounds(display)
    var claimed: [CGWindowID: pid_t] = [:]
    for c in Claims.all() { for id in c.windows { claimed[id] = c.pid } }
    for w in serverWindows([.optionAll, .excludeDesktopElements])
    where w.layer == 0 && claimed[w.id] == w.pid && !Grabs.book.contains(w.id) && agentDisplay(containing: w.frame) == nil {
      Battery.resume(w.pid)
      guard let ax = axWindow(pid: w.pid, id: w.id) else { continue }
      returningWindow(w.id)
      setFrame(ax, CGRect(x: bounds.minX + 20, y: bounds.minY + 40,
                          width: min(w.frame.width, bounds.width - 40), height: min(w.frame.height, bounds.height - 60)))
      log("returned a \(w.owner) window to the agents' screen")
    }
  }

  /// Puts agent displays past the bottom-right corner of the user's screens, side by side, when
  /// one shares a border with his screens. macOS may snap the display to a different corner of
  /// his arrangement; any corner-only spot is accepted, and a spot macOS keeps refusing is tried
  /// once per arrangement, so display changes never feed back into each other.
  private static var lastCornerAttempt: [CGRect] = []

  private static func keepAgentDisplaysCornered() {
    let agents = agentDisplays()
    let real = activeDisplays().filter { !agents.contains($0) }.map(CGDisplayBounds)
    let agentBounds = agents.map(CGDisplayBounds)
    guard agentBounds.contains(where: { a in real.contains { Geometry.sharesEdge(a, $0) } }) else { return }
    guard real != lastCornerAttempt else {
      log("the agent display touches your screens and macOS will not move it to a corner")
      return
    }
    lastCornerAttempt = real
    guard !agents.isEmpty,
          let origins = Geometry.cornerOrigins(real: real, agentWidths: agentBounds.map(\.width)) else { return }
    var config: CGDisplayConfigRef?
    var changed = false
    guard CGBeginDisplayConfiguration(&config) == .success else { return }
    for (id, origin) in zip(agents, origins) where CGDisplayBounds(id).origin != origin {
      CGConfigureDisplayOrigin(config, id, Int32(origin.x), Int32(origin.y))
      changed = true
    }
    if changed {
      CGCompleteDisplayConfiguration(config, .forSession)
      log("moved the agent display to the corner of your screens")
    } else {
      CGCancelDisplayConfiguration(config)
    }
  }

  // MARK: moving

  /// Moves a window from an agent display to the user's screen, keeping its place there.
  /// Answers its frame before the move, or nil when it was not on an agent display.
  @discardableResult
  static func moveToUser(_ window: AXUIElement, app: NSRunningApplication) -> CGRect? {
    guard let frame = axFrame(window), let agent = agentDisplay(containing: frame),
          let target = userDisplay() else { return nil }
    setFrame(window, Geometry.userFrame(window: frame, agentBounds: CGDisplayBounds(agent), area: usableArea(target)))
    log("moved a \(app.localizedName ?? "pid \(app.processIdentifier)") window off the agent display to display \(target)")
    return frame
  }
}
