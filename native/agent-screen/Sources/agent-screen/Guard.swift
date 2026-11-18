import AgentScreenCore
import AppKit
import ApplicationServices

/// Keeps the user's own windows off agent displays, event-driven.
///
/// - The frontmost app is the user's. When it comes forward, opens a window, or a window of it
///   changes focus or stops moving on an agent display, that window moves to the screen he is on.
/// - When displays change (lid closed, monitor unplugged), macOS moves windows from the lost
///   screen to a remaining one, which can be an agent display. Windows never seen on an agent
///   display before the change and on one after are moved back.
/// - Agent displays sit diagonally off the bottom-right corner of his screens, touching them at
///   one point only, so his mouse cannot cross over.
/// - Agents' windows stay: Safari's (the Safari helper keeps its agent windows there) and apps
///   claimed by agent-display `launch`/`move`.
/// - When every agent session that started an app has ended, the app is quit.
enum WindowGuard {
  private static let agentOwnedBundles: Set<String> = ["com.apple.Safari"]
  private static var observer: AXObserver?
  private static var observedPid: pid_t = 0
  /// Windows seen on an agent display while displays were steady: agents' windows.
  private static var knownOnAgents: Set<CGWindowID> = []
  private static var pendingMoves: [(window: AXUIElement, work: DispatchWorkItem)] = []
  /// Windows the companion just put back on the agents' screen, which the guard must not take
  /// for his while he is still in their app.
  private static var returning: [CGWindowID: Date] = [:]

  static func start() {
    NSWorkspace.shared.notificationCenter.addObserver(
      forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main
    ) { _ in frontmostChanged() }
    CGDisplayRegisterReconfigurationCallback({ _, flags, _ in
      WindowGuard.displaysChanging(flags)
    }, nil)
    noteAgentWindows()
    keepAgentDisplaysCornered()
    frontmostChanged()
    let sweep = Timer(timeInterval: 30, repeats: true) { _ in
      noteAgentWindows()
      quitAppsOfEndedAgents()
    }
    sweep.tolerance = 5
    RunLoop.main.add(sweep, forMode: .common)
  }

  static func ownedByAgents(_ app: NSRunningApplication, window: CGWindowID?) -> Bool {
    agentOwnedBundles.contains(app.bundleIdentifier ?? "") || Claims.owns(app.processIdentifier, window: window)
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

  private static func quitAppsOfEndedAgents() {
    for c in Claims.all() where !c.owners.isEmpty && c.owners.allSatisfy({ kill($0, 0) != 0 }) {
      Claims.remove(c.pid)
      guard c.launched, let app = NSRunningApplication(processIdentifier: c.pid) else { continue }
      Battery.resume(c.pid)
      if axWindows(c.pid).contains(where: { windowID($0).map(c.windows.contains) != true }) {
        log("left \(app.localizedName ?? "pid \(c.pid)") open: the user has his own windows in it")
        continue
      }
      app.terminate()
      log("quit \(app.localizedName ?? "pid \(c.pid)"): the agents that opened it have finished")
    }
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
          !ownedByAgents(app, window: id), !isReturning(id) else { return }
    moveToUser(window, app: app)
  }

  // MARK: display changes

  private static func displaysChanging(_ flags: CGDisplayChangeSummaryFlags) {
    if flags.contains(.beginConfigurationFlag) {
      noteAgentWindows()
      return
    }
    // macOS relocates windows shortly after the change completes.
    for delay in [1.0, 3.0] {
      DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
        keepAgentDisplaysCornered()
        rescueRelocated()
      }
    }
  }

  /// Adds windows now on agent displays and forgets windows that no longer exist. The begin
  /// notification can arrive after a change already applied, so a window once seen there keeps
  /// counting as the agents'.
  private static func noteAgentWindows() {
    let alive = Set(serverWindows([.optionAll]).map(\.id))
    knownOnAgents = knownOnAgents.intersection(alive).union(windowsOnAgentDisplays().map(\.id))
  }

  private static func rescueRelocated() {
    for w in windowsOnAgentDisplays() where !knownOnAgents.contains(w.id) && !Claims.owns(w.pid, window: w.id) {
      guard let app = NSRunningApplication(processIdentifier: w.pid), let ax = axWindow(pid: w.pid, id: w.id) else { continue }
      moveToUser(ax, app: app)
    }
    noteAgentWindows()
  }

  /// Puts agent displays past the bottom-right corner of the user's screens, side by side.
  private static func keepAgentDisplaysCornered() {
    let agents = agentDisplays()
    let real = activeDisplays().filter { !agents.contains($0) }.map(CGDisplayBounds)
    guard !agents.isEmpty,
          let origins = Geometry.cornerOrigins(real: real, agentWidths: agents.map { CGDisplayBounds($0).width }) else { return }
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
