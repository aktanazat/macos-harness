import AgentScreenCore
import AppKit
import IOKit.ps

/// Goes easy on the battery: on battery power or a serious thermal state, the peek refreshes
/// less often and agent-launched apps left idle in the background are paused (SIGSTOP). They
/// resume on power, when he brings one forward or grabs its window, and when an agent acts on
/// one (agent-display and macos-harness also send SIGCONT before acting on a pid).
enum Battery {
  private(set) static var saving = false
  private static var paused: Set<pid_t> = []
  private static var activity = WindowActivity()
  private static var sweepTimer: Timer?
  /// Called on the main queue when `saving` changes.
  static var onChange: () -> Void = {}

  /// Pids this companion paused, kept on disk so a restarted companion resumes them.
  private static var pausedFile: URL { AgentDisplay.stateDir.appendingPathComponent("paused") }

  /// Resumes the apps a previous companion paused and left paused (it crashed or was killed).
  static func resumeLeftovers() {
    let left = ((try? String(contentsOf: pausedFile, encoding: .utf8)) ?? "").split(whereSeparator: \.isNewline)
    for pid in left.compactMap({ pid_t($0) }) { kill(pid, SIGCONT) }
    persist()
  }

  static func start() {
    if let loop = IOPSNotificationCreateRunLoopSource({ _ in Battery.update() }, nil)?.takeRetainedValue() {
      CFRunLoopAddSource(CFRunLoopGetMain(), loop, .defaultMode)
    }
    NotificationCenter.default.addObserver(forName: ProcessInfo.thermalStateDidChangeNotification, object: nil,
                                           queue: .main) { _ in update() }
    NSWorkspace.shared.notificationCenter.addObserver(
      forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main
    ) { note in
      if let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication {
        resume(app.processIdentifier)
      }
    }
    update()
  }

  static func update() {
    let now = Power.saving(onBattery: onBattery(), thermal: ProcessInfo.processInfo.thermalState)
    guard now != saving else { return }
    saving = now
    log(saving ? "saving power: slower peek, idle agent apps pause" : "on power: agent apps resume")
    sweepTimer?.invalidate()
    sweepTimer = nil
    if saving {
      let t = Timer(timeInterval: 60, repeats: true) { _ in sweep() }
      t.tolerance = 10
      RunLoop.main.add(t, forMode: .common)
      sweepTimer = t
      sweep()
    } else {
      resumeAll()
    }
    onChange()
  }

  static func agentActed(on pid: pid_t) {
    activity.touch(pid, at: Date())
    resume(pid)
  }

  static func resume(_ pid: pid_t) {
    guard paused.remove(pid) != nil else { return }
    kill(pid, SIGCONT)
    activity.touch(pid, at: Date())
    persist()
    log("resumed \(NSRunningApplication(processIdentifier: pid)?.localizedName ?? "pid \(pid)")")
  }

  static func resumeAll() {
    for pid in paused { kill(pid, SIGCONT) }
    paused = []
    persist()
  }

  private static func sweep() {
    let now = Date()
    let claims = Claims.all().filter(\.launched)
    let launched = claims.map(\.pid)
    let signatures = windowSignatures(Set(launched))
    for pid in launched { activity.observe(pid, signature: signatures[pid] ?? 0, at: now) }
    activity.keep(Set(launched))
    let front = NSWorkspace.shared.frontmostApplication?.processIdentifier
    let grabbed = Set(Grabs.book.grabs.map(\.pid))
    let apps = claims.filter { !paused.contains($0.pid) }.map { c in
      Power.App(pid: c.pid, launched: true, frontmost: c.pid == front, grabbed: grabbed.contains(c.pid),
                hasUserWindow: hasUserWindow(c), lastActivity: activity.last(c.pid) ?? now)
    }
    for pid in Power.toPause(apps, now: now) where kill(pid, SIGSTOP) == 0 {
      paused.insert(pid)
      log("paused \(NSRunningApplication(processIdentifier: pid)?.localizedName ?? "pid \(pid)"): idle 10 min on battery")
    }
    persist()
  }

  /// Whether the app has a window off the agent displays that the agents never claimed, as the
  /// quit path judges his windows.
  private static func hasUserWindow(_ claim: Claim) -> Bool {
    axWindows(claim.pid).contains { w in
      if let id = windowID(w), claim.windows.contains(id) { return false }
      return axFrame(w).map { agentDisplay(containing: $0) == nil } ?? true
    }
  }

  /// A digest of each app's windows: ids, frames and titles.
  private static func windowSignatures(_ pids: Set<pid_t>) -> [pid_t: Int] {
    guard !pids.isEmpty else { return [:] }
    var hashers: [pid_t: Hasher] = [:]
    for w in serverWindows([.optionAll, .excludeDesktopElements]) where pids.contains(w.pid) {
      var h = hashers[w.pid] ?? Hasher()
      h.combine(w.id)
      h.combine(w.frame.origin.x)
      h.combine(w.frame.origin.y)
      h.combine(w.frame.width)
      h.combine(w.frame.height)
      h.combine(w.title)
      h.combine(w.onScreen)
      hashers[w.pid] = h
    }
    return hashers.mapValues { $0.finalize() }
  }

  private static func persist() {
    let text = paused.map(String.init).joined(separator: "\n")
    try? text.write(to: pausedFile, atomically: true, encoding: .utf8)
  }

  private static func onBattery() -> Bool {
    guard let info = IOPSCopyPowerSourcesInfo()?.takeRetainedValue(),
          let type = IOPSGetProvidingPowerSourceType(info)?.takeUnretainedValue() else { return false }
    return (type as String) == kIOPSBatteryPowerValue
  }
}
