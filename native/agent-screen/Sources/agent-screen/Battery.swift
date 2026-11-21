import AgentScreenCore
import AppKit
import IOKit.ps

/// Goes easy on the battery: on battery power or a serious thermal state, the peek refreshes
/// less often and agent-launched apps left idle in the background are paused (SIGSTOP). They
/// resume on power, when he brings one forward or grabs its window, and when an agent acts on
/// one: agent-display and macos-harness touch `acting/<pid>`, then send SIGCONT, before acting
/// on a pid. A pause re-reads that mark after SIGSTOP and undoes itself when an agent touched it
/// since the idle check, so no agent's SIGCONT lands before a pause that then sticks; a mark
/// newer than a pause means an agent woke the app.
enum Battery {
  private(set) static var saving = false
  /// Apps this companion paused, and when.
  private static var paused: [pid_t: Date] = [:]
  private static var activity = WindowActivity()
  private static var sweepTimer: Timer?
  /// Called on the main queue when `saving` changes.
  static var onChange: () -> Void = {}

  /// Pids this companion paused, kept on disk so a restarted companion resumes them.
  private static var pausedFile: URL { AgentDisplay.stateDir.appendingPathComponent("paused") }
  private static var actingDir: URL { AgentDisplay.stateDir.appendingPathComponent("acting", isDirectory: true) }

  /// Resumes the apps a previous companion paused and left paused (it crashed or was killed),
  /// and forgets the wake marks of processes that ended.
  static func resumeLeftovers() {
    let left = ((try? String(contentsOf: pausedFile, encoding: .utf8)) ?? "").split(whereSeparator: \.isNewline)
    for pid in left.compactMap({ pid_t($0) }) { kill(pid, SIGCONT) }
    persist()
    try? FileManager.default.createDirectory(at: actingDir, withIntermediateDirectories: true)
    for name in (try? FileManager.default.contentsOfDirectory(atPath: actingDir.path)) ?? [] {
      if let pid = pid_t(name), kill(pid, 0) == 0 { continue }
      try? FileManager.default.removeItem(at: actingDir.appendingPathComponent(name))
    }
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
    guard let at = paused.removeValue(forKey: pid) else { return }
    kill(pid, SIGCONT)
    activity.touch(pid, at: Date())
    persist()
    guard !wokenByAgent(pid, since: at) else { return }
    log("resumed \(NSRunningApplication(processIdentifier: pid)?.localizedName ?? "pid \(pid)")")
  }

  static func resumeAll() {
    for pid in paused.keys { kill(pid, SIGCONT) }
    paused = [:]
    persist()
  }

  private static func sweep() {
    let now = Date()
    for (pid, at) in paused where wokenByAgent(pid, since: at) {
      // The agent that marked it may have died before its SIGCONT.
      kill(pid, SIGCONT)
      paused[pid] = nil
      activity.touch(pid, at: now)
    }
    let claims = Claims.all().filter(\.launched)
    let launched = claims.map(\.pid)
    let signatures = windowSignatures(Set(launched))
    for pid in launched { activity.observe(pid, signature: signatures[pid] ?? 0, at: now) }
    activity.keep(Set(launched))
    var acting: [pid_t: Date] = [:]
    for pid in launched { acting[pid] = lastActing(pid) }
    let front = NSWorkspace.shared.frontmostApplication?.processIdentifier
    let grabbed = Set(Grabs.book.grabs.map(\.pid))
    let apps = claims.filter { paused[$0.pid] == nil }.map { c in
      Power.App(pid: c.pid, launched: true, frontmost: c.pid == front, grabbed: grabbed.contains(c.pid),
                hasUserWindow: hasUserWindow(c),
                lastActivity: max(activity.last(c.pid) ?? now, acting[c.pid] ?? .distantPast))
    }
    for pid in Power.toPause(apps, now: now) where kill(pid, SIGSTOP) == 0 {
      guard lastActing(pid) == acting[pid] else {
        kill(pid, SIGCONT)
        continue
      }
      paused[pid] = now
      log("paused \(NSRunningApplication(processIdentifier: pid)?.localizedName ?? "pid \(pid)"): idle 10 min on battery")
    }
    persist()
  }

  /// When an agent last marked `pid` as about to act on it.
  private static func lastActing(_ pid: pid_t) -> Date? {
    let path = actingDir.appendingPathComponent(String(pid)).path
    return (try? FileManager.default.attributesOfItem(atPath: path))?[.modificationDate] as? Date
  }

  private static func wokenByAgent(_ pid: pid_t, since pause: Date) -> Bool {
    lastActing(pid).map { $0 > pause } ?? false
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
    let text = paused.keys.map(String.init).joined(separator: "\n")
    try? text.write(to: pausedFile, atomically: true, encoding: .utf8)
  }

  private static func onBattery() -> Bool {
    guard let info = IOPSCopyPowerSourcesInfo()?.takeRetainedValue(),
          let type = IOPSGetProvidingPowerSourceType(info)?.takeUnretainedValue() else { return false }
    return (type as String) == kIOPSBatteryPowerValue
  }
}
