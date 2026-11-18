import AgentScreenCore
import AppKit
import ApplicationServices

/// Keeps windows an agent-owned app opens later (a second document, a dialog-like window) on
/// the agent display, where macOS would otherwise put them on the user's screen. A window that
/// opens while the user is in the app is his.
enum AgentApps {
  private static var observers: [pid_t: AXObserver] = [:]
  private static var watch: DispatchSourceFileSystemObject?

  static func start() {
    let fd = open(AgentDisplay.stateDir.path, O_EVTONLY)
    if fd >= 0 {
      let src = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: .write, queue: .main)
      src.setEventHandler { sync() }
      src.setCancelHandler { close(fd) }
      src.resume()
      watch = src
    }
    sync()
  }

  /// Matches the observed apps to the claims file.
  private static func sync() {
    let owned = Set(Claims.all().filter(\.wholeApp).map(\.pid))
    for (pid, obs) in observers where !owned.contains(pid) {
      CFRunLoopRemoveSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(obs), .defaultMode)
      observers[pid] = nil
    }
    for pid in owned where observers[pid] == nil {
      var obs: AXObserver?
      guard AXObserverCreate(pid, { _, element, _, _ in AgentApps.windowCreated(element) }, &obs) == .success, let obs,
            AXObserverAddNotification(obs, axApp(pid), kAXWindowCreatedNotification as CFString, nil) == .success else { continue }
      CFRunLoopAddSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(obs), .defaultMode)
      observers[pid] = obs
    }
  }

  private static func windowCreated(_ window: AXUIElement) {
    var pid: pid_t = 0
    guard AXUIElementGetPid(window, &pid) == .success,
          NSWorkspace.shared.frontmostApplication?.processIdentifier != pid,
          let id = windowID(window) else { return }
    guard let display = agentDisplays().first, let frame = axFrame(window) else { return }
    if agentDisplay(containing: frame) == nil {
      let bounds = CGDisplayBounds(display)
      setFrame(window, CGRect(x: bounds.minX + 20, y: bounds.minY + 40,
                              width: min(frame.width, bounds.width - 40), height: min(frame.height, bounds.height - 60)))
    }
    Claims.add(pid, windows: [id], wholeApp: true, launched: false)
  }
}
